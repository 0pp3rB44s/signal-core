"""Production poller implementing the frozen funding-crowding formula exactly."""
from __future__ import annotations
import math
import statistics
import time
from funding_pilot.core import PilotSignal, verify_spec


def _pct_rank(values, value):
    less = sum(x < value for x in values)
    equal = sum(x == value for x in values)
    return (less + (equal + 1) / 2) / len(values)


def frozen_funding_decision(funding, opens):
    """Shared pure implementation of the frozen research event formula."""
    if len(funding) < 30 or len(opens) != len(funding): return None
    ts, rate = funding[-1]
    pct = _pct_rank([x[1] for x in funding[-90:]], rate)
    rets = [opens[i]/opens[i-3]-1 if i>=3 and opens[i-3]>0 else math.nan for i in range(len(opens))]
    history = [x for x in rets[max(0,len(rets)-31):-1] if math.isfinite(x)]
    if len(history)<20 or not math.isfinite(rets[-1]): return None
    vol=statistics.stdev(history)
    if vol<=0: return None
    extension=rets[-1]/vol
    if not ((pct>=.95 and extension>=1.5) or (pct<=.05 and extension<=-1.5)): return None
    return {"timestamp_ms":ts, "funding_rate":rate, "funding_pct":pct,
            "extension":extension, "side":"LONG" if extension>0 else "SHORT",
            "reference_price":opens[-1]}


class FrozenFundingCrowdingSignalPoller:
    def __init__(self, *, client, ledger, spec_path):
        self.client, self.ledger, self.spec_path = client, ledger, spec_path
        verify_spec(spec_path)

    def _funding(self, symbol):
        payload = self.client._request("GET", "/api/v2/mix/market/history-fund-rate",
            params={"symbol":symbol, "productType":"USDT-FUTURES", "pageSize":"100", "pageNo":"1"}, private=False)
        rows = (payload or {}).get("data") or []
        return sorted([(int(r["fundingTime"]), float(r["fundingRate"])) for r in rows], key=lambda x:x[0])

    def __call__(self):
        verify_spec(self.spec_path)
        now = int(time.time() * 1000)
        contracts = (self.client.get_contracts("USDT-FUTURES") or {}).get("data") or []
        features = []
        series = {}
        for contract in contracts:
            symbol = str(contract.get("symbol") or "").upper()
            if not symbol or str(contract.get("symbolStatus") or contract.get("status") or "normal").lower() != "normal":
                continue
            raw = (self.client.get_candles(symbol=symbol, product_type="USDT-FUTURES", granularity="1H", limit=200) or {}).get("data") or []
            candles = sorted([(int(r[0]), float(r[1]), float(r[4]), float(r[6] if len(r)>6 else r[5])) for r in raw], key=lambda x:x[0])
            completed = [r for r in candles if r[0] + 3_600_000 <= now]
            if len(completed) < 100: continue
            closes = [r[2] for r in completed]
            returns = [closes[i]/closes[i-1]-1 for i in range(1,len(closes)) if closes[i-1]>0]
            book = (self.client.get_orderbook(symbol=symbol, product_type="USDT-FUTURES", limit=50) or {}).get("data") or {}
            bids, asks = book.get("bids") or [], book.get("asks") or []
            if not bids or not asks: continue
            bid, ask = float(bids[0][0]), float(asks[0][0]); mid=(bid+ask)/2
            depth = sum(float(x[0])*float(x[1]) for x in bids+asks)
            if depth < 1000 or mid <= 0: continue
            turnover24 = sum(row[3] for row in completed[-24:])
            features.append((symbol, statistics.pstdev(returns[-72:]), turnover24, (ask-bid)/mid))
            series[symbol] = completed
        if not features: return []
        vols=[x[1] for x in features]; turns=[x[2] for x in features]; spreads=[x[3] for x in features]
        eligible=[x[0] for x in features if _pct_rank(vols,x[1])>=.70 and _pct_rank(turns,x[2])>=.60 and _pct_rank(spreads,x[3])<=.50]
        candidates=[]
        for symbol in eligible:
            funding=self._funding(symbol)
            if len(funding)<30: continue
            ts, rate=funding[-1]
            if ts > now or now-ts > 300_000 or self.ledger.get(f"signal:{symbol}:{ts}") == "SEEN": continue
            # Exact frozen research definition: 24h return across three 8h
            # funding observations; trailing 30-event volatility excludes now.
            opens=[]
            candles=series[symbol]
            for event_ts,_ in funding:
                prior=[c[1] for c in candles if c[0] <= event_ts]
                opens.append(prior[-1] if prior else math.nan)
            decision=frozen_funding_decision(funding, opens)
            if decision is None: continue
            extension=decision["extension"]
            candidates.append((abs(extension), symbol, PilotSignal(
                f"funding:{symbol}:{ts}", ts, symbol, decision["side"], decision["reference_price"],
                {"funding_rate":rate, "funding_pct":decision["funding_pct"], "extension":extension})))
        candidates.sort(key=lambda x:(-x[0],x[1]))
        if not candidates: return []
        signal=candidates[0][2]
        self.ledger.set(f"signal:{signal.symbol}:{signal.timestamp_ms}", "SEEN")
        return [signal]
