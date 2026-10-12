"""`execution_status_v1` - read-only execution status contract for Mission Control.

Producer side only. Mission Control (Agent B) owns the consumer/frontend; this
module never imports Mission Control and Mission Control never imports this.

Hard rules encoded here:
* UNKNOWN is never rendered as zero: unknown numbers are `null`, unknown state
  is the literal "UNKNOWN".
* Exchange truth is authoritative. When no exchange snapshot is supplied the
  positions/orders/account sections say `source = "UNAVAILABLE"` instead of
  guessing from local files; local files only feed `fills` and `heartbeat`.
* The payload carries the producer timestamp so a consumer can age it.
* Builder is pure (no network). `ExchangeReadOnlyProbe` is the only networked
  helper and issues GET requests only.
"""
from __future__ import annotations

import hashlib
import json
import os
import socket
import time
from pathlib import Path
from typing import Any

from execution.emergency_stop import EmergencyStop

SCHEMA = "execution_status_v1"
SCHEMA_VERSION = 1
DEFAULT_TTL_S = 90
HEARTBEAT_FRESH_S = 180


def _utc(ts: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(ts))


def _num(x: Any) -> float | None:
    try:
        if x is None or x == "":
            return None
        v = float(x)
        return v if v == v else None
    except (TypeError, ValueError):
        return None


def _read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text())
    except Exception:
        return None


def _git_commit(root: Path) -> str | None:
    try:
        head = (root / ".git")
        if head.is_file():  # worktree
            gitdir = Path(head.read_text().split("gitdir:")[1].strip())
        else:
            gitdir = head
        ref = (gitdir / "HEAD").read_text().strip()
        if ref.startswith("ref:"):
            refpath = ref.split(" ", 1)[1]
            for base in (gitdir, gitdir.parent.parent if "worktrees" in str(gitdir) else gitdir):
                p = base / refpath
                if p.exists():
                    return p.read_text().strip()[:12]
            return None
        return ref[:12]
    except Exception:
        return None


# ----------------------------------------------------------------- probe
class ExchangeReadOnlyProbe:
    """GET-only snapshot using the bot's existing client. Never places/cancels/changes anything."""

    def __init__(self, client: Any, product_type: str = "USDT-FUTURES", probe_symbol: str = "BTCUSDT") -> None:
        self.client, self.product_type, self.probe_symbol = client, product_type, probe_symbol

    def snapshot(self) -> dict[str, Any]:
        out: dict[str, Any] = {"ok": True, "errors": [], "t": time.time()}

        def call(label: str, fn):
            try:
                return fn()
            except Exception as exc:  # recorded, never raised
                out["ok"] = False
                out["errors"].append(f"{label}: {type(exc).__name__}: {str(exc)[:160]}")
                return None

        t0 = time.time()
        srv = call("server_time", lambda: self.client._request("GET", "/api/v2/public/time"))
        t1 = time.time()
        if srv:
            st = _num((srv.get("data") or {}).get("serverTime"))
            out["rtt_ms"] = round((t1 - t0) * 1000, 0)
            out["clock_offset_ms"] = None if st is None else round(st - (t0 + t1) / 2 * 1000, 0)
        acc = call("accounts", lambda: self.client.get_accounts(self.product_type))
        if acc is not None:
            out["accounts"] = acc.get("data") or []
        cfg = call("account_cfg", lambda: self.client._request(
            "GET", "/api/v2/mix/account/account",
            params={"symbol": self.probe_symbol, "productType": self.product_type, "marginCoin": "USDT"}, private=True))
        if cfg is not None:
            out["account_cfg"] = cfg.get("data") or {}
        pos = call("positions", lambda: self.client.get_all_positions())
        if pos is not None:
            out["positions"] = pos.get("data") or []
        orders = call("open_orders", lambda: self.client.get_open_orders())
        if orders is not None:
            d = orders.get("data") or {}
            out["open_orders"] = (d.get("entrustedList") if isinstance(d, dict) else d) or []
        plans = call("tpsl_orders", lambda: self.client.get_tpsl_orders())
        if plans is not None:
            try:
                out["tpsl_orders"] = self.client._extract_plan_list(plans)
            except Exception as exc:
                out["errors"].append(f"tpsl_extract: {exc}")
        return out


# --------------------------------------------------------------- builder
def build_execution_status(
    *,
    settings: Any,
    root: str | os.PathLike = ".",
    exchange: dict[str, Any] | None = None,
    now: float | None = None,
    emergency: EmergencyStop | None = None,
    ttl_s: int = DEFAULT_TTL_S,
) -> dict[str, Any]:
    root = Path(root)
    now = time.time() if now is None else now
    emergency = emergency or EmergencyStop(root / "state" / "EMERGENCY_STOP.json")
    es = emergency.status()

    # ---- mode
    exec_enabled = bool(getattr(settings, "execution_enabled", False))
    exec_mode = str(getattr(settings, "execution_mode", "DRY_RUN")).upper()
    fwd_only = bool(getattr(settings, "forward_paper_only", False))
    live = bool(exec_enabled and exec_mode == "LIVE" and not fwd_only)
    mode = {
        "execution_enabled": exec_enabled, "execution_mode": exec_mode, "forward_paper_only": fwd_only,
        "live_trading_enabled": live, "app_env": str(getattr(settings, "app_env", "")),
        "live_trading_default_is_false": True,
    }

    # ---- exchange-derived sections
    ex = exchange or {}
    have_ex = bool(exchange) and ("positions" in ex or "accounts" in ex)
    src = "EXCHANGE_READ_ONLY" if have_ex else "UNAVAILABLE"
    errs = list(ex.get("errors") or [])

    usdt = None
    for row in ex.get("accounts") or []:
        if str(row.get("marginCoin", "")).upper() == "USDT":
            usdt = row
    cfg = ex.get("account_cfg") or {}
    account = {
        "state": "KNOWN" if usdt is not None else "UNKNOWN",
        "source": src,
        "equity_usdt": _num((usdt or {}).get("accountEquity")),
        "available_usdt": _num((usdt or {}).get("available")),
        "unrealized_pnl_usdt": _num((usdt or {}).get("unrealizedPL")),
        "pos_mode": cfg.get("posMode"), "margin_mode": cfg.get("marginMode"),
        "leverage": {k: cfg.get(k) for k in ("crossedMarginLeverage", "isolatedLongLever", "isolatedShortLever") if k in cfg} or None,
    }

    tpsl = ex.get("tpsl_orders")
    positions: list[dict[str, Any]] = []
    for p in ex.get("positions") or []:
        sym = str(p.get("symbol") or "").upper()
        side = str(p.get("holdSide") or "").lower()
        has_pos_sl = bool(_num(p.get("stopLoss"))) or bool(p.get("stopLossId"))
        has_plan_sl = None
        if tpsl is not None:
            has_plan_sl = any(
                str(o.get("symbol") or "").upper() == sym
                and str(o.get("holdSide") or side).lower() == side
                and str(o.get("planType") or "").lower() in {"loss_plan", "pos_loss", "moving_plan", "profit_loss"}
                for o in tpsl
            )
        protected: Any
        if tpsl is None and not has_pos_sl:
            protected = None  # cannot tell -> UNKNOWN, never assumed protected
        else:
            protected = bool(has_pos_sl or has_plan_sl)
        positions.append({
            "symbol": sym, "hold_side": side, "size": _num(p.get("total")), "entry_price": _num(p.get("openPriceAvg")),
            "mark_price": _num(p.get("markPrice")), "unrealized_pnl_usdt": _num(p.get("unrealizedPL")),
            "leverage": _num(p.get("leverage")), "margin_mode": p.get("marginMode"),
            "stop_loss_price": _num(p.get("stopLoss")), "take_profit_price": _num(p.get("takeProfit")),
            "stop_loss_confirmed": protected,
        })
    orders = [{
        "symbol": str(o.get("symbol") or "").upper(), "side": o.get("side"), "trade_side": o.get("tradeSide"),
        "order_type": o.get("orderType"), "size": _num(o.get("size")), "price": _num(o.get("price")),
        "reduce_only": o.get("reduceOnly"), "status": o.get("status") or o.get("state"), "client_oid": o.get("clientOid"),
    } for o in ex.get("open_orders") or []]

    unprotected = [p for p in positions if p["stop_loss_confirmed"] is False]
    unknown_prot = [p for p in positions if p["stop_loss_confirmed"] is None]

    # ---- local-only sections (fills, heartbeat)
    journal = _read_json(root / "state" / "live_trade_journal.json")
    fills: list[dict[str, Any]] = []
    if isinstance(journal, list):
        for r in journal[-10:][::-1]:
            if isinstance(r, dict):
                fills.append({
                    "symbol": r.get("symbol"), "direction": r.get("direction"), "status": r.get("status"),
                    "entry": _num(r.get("entry")), "pnl_usdt": _num(r.get("pnl")), "fees_paid_usdt": _num(r.get("fees_paid")),
                    "opened_at": r.get("opened_at"), "closed_at": r.get("closed_at"), "exchange_order_id": r.get("exchange_order_id"),
                    "source": "LOCAL_JOURNAL_NOT_EXCHANGE_TRUTH",
                })
    hb = _read_json(root / "state" / "runtime_heartbeat.json") or {}
    hb_age = None
    hb_ts = hb.get("timestamp")
    if hb_ts:
        try:
            import datetime as _dt
            hb_age = now - _dt.datetime.fromisoformat(str(hb_ts).replace("Z", "+00:00")).timestamp()
        except Exception:
            hb_age = None
    heartbeat = {
        "state": "MISSING" if not hb else ("FRESH" if hb_age is not None and hb_age <= HEARTBEAT_FRESH_S else "STALE" if hb_age is not None else "UNKNOWN"),
        "last_heartbeat_utc": hb_ts, "age_s": None if hb_age is None else round(hb_age, 1),
        "pid": hb.get("pid"), "stage": hb.get("stage"), "scan_cycles_completed": hb.get("scan_cycles_completed"),
    }

    # ---- connection
    if not have_ex:
        conn_state = "UNKNOWN"
    elif ex.get("ok"):
        conn_state = "OK"
    else:
        conn_state = "DEGRADED"
    connection = {
        "status": conn_state, "exchange": "bitget", "product_type": str(getattr(settings, "bitget_product_type", "")),
        "credentials_present": bool(getattr(getattr(settings, "bitget_api_key", None), "get_secret_value", lambda: "")())
        if hasattr(getattr(settings, "bitget_api_key", None), "get_secret_value") else None,
        "rtt_ms": ex.get("rtt_ms"), "clock_offset_ms": ex.get("clock_offset_ms"), "errors": errs,
        "last_probe_utc": _utc(ex["t"]) if ex.get("t") else None,
    }

    # ---- risk
    reasons: list[str] = []
    if es["active"]:
        reasons.append(es["reason"])
    if not have_ex:
        reasons.append("exchange state UNKNOWN: new orders must be blocked")
    elif not ex.get("ok"):
        reasons.append("exchange probe degraded: new orders must be blocked")
    if unprotected:
        reasons.append("UNPROTECTED POSITION(S): " + ",".join(f"{p['symbol']}:{p['hold_side']}" for p in unprotected))
    if unknown_prot:
        reasons.append("stop-loss UNCONFIRMED for: " + ",".join(f"{p['symbol']}:{p['hold_side']}" for p in unknown_prot))
    if heartbeat["state"] in {"STALE", "MISSING"} and live:
        reasons.append(f"runtime heartbeat {heartbeat['state']}")
    entries_blocked = bool(reasons)
    max_notional = _num(getattr(settings, "execution_max_live_notional_per_trade_usdt", None))
    risk = {
        "state": "BLOCKED" if entries_blocked else ("OK" if have_ex else "UNKNOWN"),
        "entries_blocked": entries_blocked, "reasons": reasons,
        "operator_attention_required": bool(unprotected or unknown_prot),
        "limits": {
            "max_open_positions": getattr(settings, "max_open_positions", None),
            "max_leverage": _num(getattr(settings, "max_leverage", None)),
            "max_daily_loss_pct": _num(getattr(settings, "max_daily_loss_pct", None)),
            "hard_daily_stop_pct": _num(getattr(settings, "hard_daily_stop_pct", None)),
            "max_notional_per_trade_usdt": max_notional,
            "account_risk_per_trade_pct": _num(getattr(settings, "account_risk_per_trade_pct", None)),
        },
    }

    doc = {
        "schema": SCHEMA, "schema_version": SCHEMA_VERSION,
        "producer": {
            "name": "tradingbot_mvp_execution_status", "host": socket.gethostname(),
            "commit": _git_commit(root), "generated_at_utc": _utc(now), "generated_at_ns": int(now * 1e9), "ttl_s": ttl_s,
            "read_only": True,
        },
        "connection": connection, "mode": mode, "account": account,
        "positions": {"source": src, "count": len(positions) if have_ex and "positions" in ex else None, "rows": positions},
        "orders": {"source": src, "count": len(orders) if have_ex and "open_orders" in ex else None, "rows": orders},
        "fills": {"source": "LOCAL_JOURNAL_NOT_EXCHANGE_TRUTH", "rows": fills},
        "risk": risk,
        "emergency_stop": es,
        "heartbeat": heartbeat,
    }
    body = json.dumps(doc, sort_keys=True, separators=(",", ":"))
    doc["content_sha256"] = hashlib.sha256(body.encode()).hexdigest()
    return doc


def write_execution_status(doc: dict[str, Any], out_dir: str | os.PathLike) -> Path:
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    target = out / "execution_status_v1.json"
    tmp = target.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(doc, indent=1, sort_keys=True))
    os.replace(tmp, target)
    return target


JSON_SCHEMA: dict[str, Any] = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "title": "execution_status_v1",
    "type": "object",
    "required": ["schema", "schema_version", "producer", "connection", "mode", "account", "positions", "orders", "fills", "risk", "emergency_stop", "heartbeat", "content_sha256"],
    "properties": {
        "schema": {"const": SCHEMA}, "schema_version": {"const": SCHEMA_VERSION},
        "producer": {"type": "object", "required": ["generated_at_utc", "generated_at_ns", "ttl_s", "read_only"]},
        "connection": {"type": "object", "required": ["status"], "properties": {"status": {"enum": ["OK", "DEGRADED", "UNKNOWN"]}}},
        "mode": {"type": "object", "required": ["execution_enabled", "execution_mode", "live_trading_enabled"]},
        "account": {"type": "object", "required": ["state"], "properties": {"state": {"enum": ["KNOWN", "UNKNOWN"]}}},
        "positions": {"type": "object", "required": ["source", "rows"]},
        "orders": {"type": "object", "required": ["source", "rows"]},
        "fills": {"type": "object", "required": ["source", "rows"]},
        "risk": {"type": "object", "required": ["state", "entries_blocked", "reasons"], "properties": {"state": {"enum": ["OK", "BLOCKED", "UNKNOWN"]}}},
        "emergency_stop": {"type": "object", "required": ["active"]},
        "heartbeat": {"type": "object", "required": ["state"], "properties": {"state": {"enum": ["FRESH", "STALE", "MISSING", "UNKNOWN"]}}},
        "content_sha256": {"type": "string"},
    },
}
