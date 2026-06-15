import json
import logging
from pathlib import Path
from typing import Any

from app.config import Settings
from clients.schemas import ContractSpec


log = logging.getLogger("watchlist")

BASE_PATH = Path(__file__).resolve().parents[1]
REPORTS_PATH = BASE_PATH / "reports" / "backtests"


def _is_symbol_active(spec: ContractSpec) -> bool:
    return spec.status in {"normal", "listed", "trading", "online", ""}


def _cooldown_status(cooldown_manager: Any | None, symbol: str) -> Any | None:
    if cooldown_manager is None:
        return None
    try:
        return cooldown_manager.get(symbol)
    except Exception as exc:
        log.warning("WATCHLIST_COOLDOWN_CHECK_FAILED | %s | error=%s", symbol.upper(), exc)
        return None



def _symbol_expectancy_map() -> dict[str, dict[str, Any]]:
    path = REPORTS_PATH / "latest_summary.json"
    if not path.exists():
        return {}

    try:
        with path.open("r", encoding="utf-8") as handle:
            payload = json.load(handle)
    except Exception as exc:
        log.warning("WATCHLIST_EXPECTANCY_LOAD_FAILED | path=%s | error=%s", path, exc)
        return {}

    by_symbol = payload.get("by_symbol") or {}
    if not isinstance(by_symbol, dict):
        return {}

    return {
        str(symbol).upper(): stats
        for symbol, stats in by_symbol.items()
        if isinstance(stats, dict)
    }


def _expectancy_score(symbol: str, expectancy: dict[str, dict[str, Any]]) -> float:
    stats = expectancy.get(symbol.upper()) or {}
    trades = int(stats.get("trades", 0) or 0)
    exp = float(stats.get("expectancy", 0.0) or 0.0)
    tp1_hit_rate = float(stats.get("tp1_hit_rate", 0.0) or 0.0)
    lossrate = float(stats.get("lossrate", 0.0) or 0.0)

    if trades < 3:
        return 0.0

    score = 0.0
    score += max(-35.0, min(exp * 50.0, 35.0))
    score += min(tp1_hit_rate * 15.0, 15.0)

    if lossrate >= 0.75:
        score -= 25.0
    elif lossrate >= 0.65:
        score -= 12.0

    if exp < -0.25:
        score -= 20.0
    elif exp > 0.10:
        score += 10.0

    return score


def filter_contracts(
    settings: Settings,
    contracts: list[ContractSpec],
    cooldown_manager: Any | None = None,
) -> list[ContractSpec]:
    filtered: list[ContractSpec] = []
    skipped: dict[str, int] = {
        "non_usdt": 0,
        "wrong_quote": 0,
        "inactive": 0,
        "missing_min_trade_num": 0,
        "missing_size_multiplier": 0,
        "cooldown": 0,
        "low_volume": 0,
        "low_move": 0,
    }

    expectancy = _symbol_expectancy_map()

    for spec in contracts:
        symbol = spec.symbol.upper()

        if not symbol.endswith("USDT"):
            skipped["non_usdt"] += 1
            continue

        if spec.quote_coin and spec.quote_coin != settings.bitget_margin_coin.upper():
            skipped["wrong_quote"] += 1
            continue

        if not _is_symbol_active(spec):
            skipped["inactive"] += 1
            continue

        if spec.min_trade_num is None or float(spec.min_trade_num or 0) <= 0:
            skipped["missing_min_trade_num"] += 1
            log.warning(
                "WATCHLIST_SYMBOL_SKIPPED_CONTRACT_META | %s | reason=missing_min_trade_num",
                symbol,
            )
            continue

        if spec.size_multiplier is None or float(spec.size_multiplier or 0) <= 0:
            skipped["missing_size_multiplier"] += 1
            log.warning(
                "WATCHLIST_SYMBOL_SKIPPED_CONTRACT_META | %s | reason=missing_size_multiplier",
                symbol,
            )
            continue

        cooldown = _cooldown_status(cooldown_manager, symbol)
        if cooldown is not None and bool(getattr(cooldown, "active", False)):
            skipped["cooldown"] += 1
            log.warning(
                "WATCHLIST_SYMBOL_SKIPPED_COOLDOWN | %s | reason=%s | remaining_minutes=%s | until=%s",
                symbol,
                getattr(cooldown, "reason", "cooldown"),
                getattr(cooldown, "remaining_minutes", 0),
                getattr(cooldown, "until", ""),
            )
            continue

        vol_ok = spec.volume_24h_usdt is None or spec.volume_24h_usdt >= settings.min_usdt_volume_24h
        move_ok = spec.change_pct_24h is None or abs(spec.change_pct_24h) >= settings.min_change_pct_24h_abs

        if not vol_ok:
            skipped["low_volume"] += 1
            continue

        if not move_ok:
            skipped["low_move"] += 1
            continue

        filtered.append(spec)

    filtered.sort(
        key=lambda x: (
            _expectancy_score(x.symbol, expectancy),
            x.volume_24h_usdt or 0.0,
            abs(x.change_pct_24h or 0.0),
        ),
        reverse=True,
    )
    selected = filtered[: settings.max_symbols]

    log.info(
        "WATCHLIST_AUDIT | input=%s | eligible=%s | selected=%s | skipped=%s | expectancy_loaded=%s | symbols=%s",
        len(contracts),
        len(filtered),
        len(selected),
        skipped,
        bool(expectancy),
        ",".join(spec.symbol for spec in selected),
    )

    return selected


def get_watchlist(
    settings: Settings,
    contracts: list[ContractSpec] | None = None,
    cooldown_manager: Any | None = None,
) -> list[str]:
    if settings.allow_auto_watchlist_refresh and contracts:
        return [c.symbol for c in filter_contracts(settings, contracts, cooldown_manager=cooldown_manager)]

    symbols: list[str] = []
    skipped_cooldown = 0

    for symbol in settings.watchlist_symbols[: settings.max_symbols]:
        symbol_upper = symbol.upper()
        cooldown = _cooldown_status(cooldown_manager, symbol_upper)
        if cooldown is not None and bool(getattr(cooldown, "active", False)):
            skipped_cooldown += 1
            log.warning(
                "WATCHLIST_STATIC_SYMBOL_SKIPPED_COOLDOWN | %s | reason=%s | remaining_minutes=%s | until=%s",
                symbol_upper,
                getattr(cooldown, "reason", "cooldown"),
                getattr(cooldown, "remaining_minutes", 0),
                getattr(cooldown, "until", ""),
            )
            continue
        symbols.append(symbol_upper)

    log.info(
        "WATCHLIST_STATIC_AUDIT | configured=%s | selected=%s | skipped_cooldown=%s | symbols=%s",
        len(settings.watchlist_symbols[: settings.max_symbols]),
        len(symbols),
        skipped_cooldown,
        ",".join(symbols),
    )

    return symbols
