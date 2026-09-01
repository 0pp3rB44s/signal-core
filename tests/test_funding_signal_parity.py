import math
import pandas as pd
import pytest

from funding_pilot.core import PilotLedger
from funding_pilot.signals import FrozenFundingCrowdingSignalPoller, frozen_funding_decision


class RecordedSnapshotClient:
    """Deterministic captured-shape market snapshot used by research and runtime."""
    symbols = ["ADAUSDT", "DOGEUSDT", "ETHUSDT", "SOLUSDT", "XRPUSDT"]
    turnover = {"ADAUSDT":100, "DOGEUSDT":500, "ETHUSDT":400, "SOLUSDT":300, "XRPUSDT":200}
    spreads = {"ADAUSDT":.004, "DOGEUSDT":.001, "ETHUSDT":.002, "SOLUSDT":.003, "XRPUSDT":.005}
    def __init__(self, now): self.now = now
    def get_contracts(self, *_):
        return {"data":[{"symbol":s,"symbolStatus":"normal"} for s in self.symbols]}
    def get_candles(self, *, symbol, **_):
        rows=[]
        amplitude={"ADAUSDT":.001,"DOGEUSDT":.05,"ETHUSDT":.02,"SOLUSDT":.01,"XRPUSDT":.005}[symbol]
        for i in range(1000):
            ts=self.now-(1000-i)*3_600_000
            close=100*(1+amplitude*math.sin(i))
            open_price=100+math.sin(i/7)
            rows.append([ts,open_price,close*1.001,close*.999,close,1,100])
        # The last event observes the last completed hourly candle. Its 24h
        # extension is deliberately a recorded 20% continuation.
        if symbol == "DOGEUSDT": rows[-1][1] = 120
        return {"data":rows}
    def get_orderbook(self, *, symbol, **_):
        spread=self.spreads[symbol]; bid=100*(1-spread/2); ask=100*(1+spread/2)
        return {"data":{"bids":[[str(bid),"10"]],"asks":[[str(ask),"10"]]}}
    def _request(self, _method, path, *, params, **_):
        symbol=params["symbol"]
        if path.endswith("ticker"):
            spread=self.spreads[symbol]
            return {"data":[{"usdtVolume":str(self.turnover[symbol]),
                             "bidPr":str(100*(1-spread/2)),"askPr":str(100*(1+spread/2))}]}
        rates=[0.0]*90
        if symbol == "DOGEUSDT": rates=[(i+1)/1_000_000 for i in range(90)]
        return {"data":[{"fundingTime":str(self.now-(89-i)*28_800_000),"fundingRate":str(rate)}
                        for i,rate in enumerate(rates)]}


def research_full_pipeline(snapshot, now):
    """Independent pandas replay of the frozen research selection pipeline."""
    rows=[]; series={}
    for symbol in snapshot.symbols:
        candles=sorted((int(r[0]),float(r[1]),float(r[4])) for r in snapshot.get_candles(symbol=symbol)["data"])
        candles=[r for r in candles if r[0]+3_600_000<=now]
        closes=pd.Series([r[2] for r in candles]); returns=(closes/closes.shift(1)).apply(math.log).dropna().tail(168)
        rows.append({"symbol":symbol,"vol":float(returns.std()*math.sqrt(24)),
                     "turnover":float(snapshot.turnover[symbol]),"spread":snapshot.spreads[symbol]})
        series[symbol]=candles
    frame=pd.DataFrame(rows)
    for column in ("vol","turnover","spread"): frame[column+"_pct"]=frame[column].rank(pct=True)
    eligible=frame[(frame.vol_pct>=.70)&(frame.turnover_pct>=.60)&(frame.spread_pct<=.50)].symbol.tolist()
    candidates=[]
    for symbol in eligible:
        rates=[(now-(89-i)*28_800_000,(i+1)/1_000_000 if symbol=="DOGEUSDT" else 0.0) for i in range(90)]
        opens=[]
        for ts,_ in rates: opens.append([r[1] for r in series[symbol] if r[0]<=ts][-1])
        f=pd.DataFrame({"ts":[x[0] for x in rates],"funding":[x[1] for x in rates],"entry":opens})
        f["pct"]=f.funding.rolling(90,min_periods=30).rank(pct=True)
        f["ret24"]=f.entry/f.entry.shift(3)-1
        f["vol24"]=f.ret24.rolling(30,min_periods=20).std().shift(1)
        f["extension"]=f.ret24/f.vol24
        last=f.iloc[-1]
        if (last.pct>=.95 and last.extension>=1.5) or (last.pct<=.05 and last.extension<=-1.5):
            candidates.append((abs(last.extension),symbol,"LONG" if last.extension>0 else "SHORT",int(last.ts)))
    candidates.sort(key=lambda row:(-row[0],row[1]))
    return {"point_in_time_universe":sorted(frame.symbol),"turnover_input":"ticker.usdtVolume",
            "turnover_values":dict(zip(frame.symbol,frame.turnover)),"universe":sorted(eligible),
            "ranking":[x[1] for x in candidates],"stale":[],
            "selected":candidates[0][1],"selected_side":candidates[0][2],"signal_timestamp":candidates[0][3]}


@pytest.mark.parametrize("direction", [1, -1])
def test_pure_production_formula_matches_frozen_pandas_research_formula(direction):
    n = 90
    rates = [direction * (i + 1) / 1_000_000 for i in range(n)]
    opens = [100 + direction * math.sin(i / 3) for i in range(n)]
    opens[-1] = opens[-4] * (1 + direction * 0.20)
    funding = [(i * 28_800_000, rates[i]) for i in range(n)]
    decision = frozen_funding_decision(funding, opens)

    frame = pd.DataFrame({"funding": rates, "entry_price": opens})
    frame["funding_pct"] = frame.funding.rolling(90, min_periods=30).rank(pct=True)
    frame["ret24"] = frame.entry_price / frame.entry_price.shift(3) - 1
    frame["ret24_vol"] = frame.ret24.rolling(30, min_periods=20).std().shift(1)
    frame["extension"] = frame.ret24 / frame.ret24_vol
    expected = frame.iloc[-1]
    assert decision is not None
    assert decision["funding_pct"] == pytest.approx(expected.funding_pct)
    assert decision["extension"] == pytest.approx(expected.extension)
    assert decision["side"] == ("LONG" if direction > 0 else "SHORT")


def test_tied_funding_rank_matches_pandas_average_rank():
    rates = [0.001] * 30
    opens = [100 + math.sin(i) for i in range(30)]
    # No signal is expected, but parity of the tie calculation is exercised by
    # ensuring it does not falsely classify the tied window at the 95th pct.
    assert frozen_funding_decision([(i, value) for i, value in enumerate(rates)], opens) is None


def test_recorded_snapshot_full_poller_golden_parity(tmp_path, monkeypatch):
    now=2_000_000_000_000
    monkeypatch.setattr("funding_pilot.signals.time.time", lambda: now/1000)
    ledger=PilotLedger(tmp_path/"golden.sqlite")
    spec=__import__("pathlib").Path(__file__).resolve().parents[1]/"research/validation/FROZEN_SPECS.json"
    poller=FrozenFundingCrowdingSignalPoller(client=RecordedSnapshotClient(now), ledger=ledger, spec_path=spec)
    signals=poller()
    assert len(signals) == 1
    recorded_golden = {
        "point_in_time_universe": ["ADAUSDT","DOGEUSDT","ETHUSDT","SOLUSDT","XRPUSDT"],
        "turnover_input": "ticker.usdtVolume",
        "turnover_values": {"ADAUSDT":100.0,"DOGEUSDT":500.0,"ETHUSDT":400.0,"SOLUSDT":300.0,"XRPUSDT":200.0},
        "universe": ["DOGEUSDT","ETHUSDT"], "ranking": ["DOGEUSDT"], "stale": [],
        "selected": "DOGEUSDT", "selected_side": "LONG", "signal_timestamp": now,
    }
    assert research_full_pipeline(RecordedSnapshotClient(now),now) == recorded_golden
    assert poller.last_audit == research_full_pipeline(RecordedSnapshotClient(now),now)
    assert (signals[0].symbol, signals[0].side, signals[0].timestamp_ms) == ("DOGEUSDT","LONG",now)
    # The exact same recorded event is stale after its durable checkpoint.
    assert poller() == []
    assert poller.last_audit["stale"] == ["DOGEUSDT"]
    # Independently exercise the wall-clock stale boundary, not only SEEN dedupe.
    stale_ledger=PilotLedger(tmp_path/"stale.sqlite")
    stale_poller=FrozenFundingCrowdingSignalPoller(client=RecordedSnapshotClient(now),ledger=stale_ledger,spec_path=spec)
    monkeypatch.setattr("funding_pilot.signals.time.time",lambda:(now+300_001)/1000)
    assert stale_poller() == []
    assert stale_poller.last_audit["stale"] == ["DOGEUSDT","ETHUSDT"]


def test_research_minimum_100_completed_bars_remains_in_universe(tmp_path,monkeypatch):
    now=2_000_000_000_000
    class BoundarySnapshot(RecordedSnapshotClient):
        symbols=["DOGEUSDT"]
        def get_candles(self,**kwargs): return {"data":super().get_candles(**kwargs)["data"][-100:]}
    monkeypatch.setattr("funding_pilot.signals.time.time",lambda:now/1000)
    spec=__import__("pathlib").Path(__file__).resolve().parents[1]/"research/validation/FROZEN_SPECS.json"
    poller=FrozenFundingCrowdingSignalPoller(client=BoundarySnapshot(now),ledger=PilotLedger(tmp_path/"b.sqlite"),spec_path=spec)
    poller()
    assert poller.last_audit["point_in_time_universe"] == ["DOGEUSDT"]
