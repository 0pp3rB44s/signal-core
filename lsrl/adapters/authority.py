"""LiveAuthority: what lets a MICRO_LIVE executor exist, what it may do, and the ONE choke point for opening a new position.

Two kinds, and nothing else can be constructed:
  * TRADE        built ONLY from an Identity that came out of `owner_ceremony.Permit.consume` in this process (the owner typed the exact phrase on a real
                 terminal after every gate passed). It carries the owner-approved capital policy and the capacity gate. It is in-memory and single-use per
                 process: a restart, a crash, a launchd respawn or a copied file has no authority and can never reach TRADE.
  * PROTECT_ONLY needs no ceremony because it can only REDUCE exposure: the executor reconciles the exchange, keeps every owned position stopped or flattens
                 it, cancels its own resting orders - and refuses every new entry. This is what a restart must fall back to.

`entries_blocked()` is the only place that decides whether a NEW ladder may start; `capital_veto()` the only place that bounds one. Neither can be
switched off from configuration, environment or a flag.
"""
from __future__ import annotations

import dataclasses
import hashlib
import json
from dataclasses import dataclass
from typing import Any

from lsrl.capital import RISK_FRACTION, CapitalPolicy, CapitalRefused
from lsrl.tools import owner_ceremony

TRADE, PROTECT_ONLY = "TRADE", "PROTECT_ONLY"
EPS = 1e-9


class AuthorityRefused(RuntimeError):
    pass


@dataclass(frozen=True)
class LiveAuthority:
    mode: str
    identity: Any = None
    capital: CapitalPolicy | None = None
    capacity_gate: Any = None
    unbounded: bool = False          # tests only: see `_authority_for_tests`; a source scan forbids any other use

    def __post_init__(self) -> None:
        if self.mode == PROTECT_ONLY:
            if self.identity is not None or self.capital is not None or self.unbounded:
                raise AuthorityRefused("PROTECT_ONLY_CARRIES_NO_TRADING_AUTHORITY")
        elif self.mode == TRADE:
            if self.unbounded:
                return
            if not owner_ceremony.is_consumed(self.identity):
                raise AuthorityRefused("CEREMONY_NOT_CONSUMED")
            if not isinstance(self.capital, CapitalPolicy) or self.capacity_gate is None:
                raise AuthorityRefused("CAPITAL_OR_CAPACITY_GATE_MISSING")
        else:
            raise AuthorityRefused(f"UNKNOWN_MODE:{self.mode!r}")

    @classmethod
    def trade(cls, identity: Any, *, capacity_gate: Any) -> "LiveAuthority":
        try:
            capital = CapitalPolicy(identity.capital_ceiling_usdt, identity.exposure_ceiling_usdt)
        except (CapitalRefused, AttributeError) as exc:
            raise AuthorityRefused(f"CAPITAL_POLICY:{exc}") from exc
        return cls(TRADE, identity, capital, capacity_gate)

    @classmethod
    def protect_only(cls) -> "LiveAuthority":
        return cls(PROTECT_ONLY)

    # ---------------------------------------------------------------- the choke points
    def entries_blocked(self) -> str | None:
        """A reason NO NEW entry may be placed now, or None. Never affects protection."""
        if self.mode != TRADE:
            return "ENTRIES_DISABLED_PROTECT_ONLY"
        if self.capacity_gate is not None:
            reason = self.capacity_gate.blocked_reason()
            if reason:
                return f"CAPACITY_GATE:{reason}"
        return None

    def capital_veto(self, *, planned_loss_usdt: float, total_notional_usdt: float, rung_notionals: list[float],
                     exchange_min_notional: float) -> str | None:
        """A veto for the WHOLE ladder (all or nothing: a thinned ladder is a different strategy), or None. It never computes or raises a size."""
        if self.unbounded:
            return None
        if self.capital is None:
            return "NO_CAPITAL_POLICY"
        ident = dataclasses.replace(self.identity, nav_usdt=self.capital.capital_ceiling_usdt)     # NAV can never exceed the ceiling
        for rung in rung_notionals:
            if not owner_ceremony.within_capital(ident, frozen_risk_usdt=planned_loss_usdt, total_exposure_usdt=total_notional_usdt,
                                                 order_notional=rung, exchange_minimum=exchange_min_notional):
                if planned_loss_usdt > self.capital.max_risk_per_full_trade_usdt * (1 + EPS):
                    return f"RISK_ABOVE_FROZEN_FRACTION:{planned_loss_usdt:.4f}>{self.capital.max_risk_per_full_trade_usdt:.4f}"
                if total_notional_usdt > self.capital.exposure_ceiling_usdt:
                    return f"EXPOSURE_ABOVE_CEILING:{total_notional_usdt:.2f}>{self.capital.exposure_ceiling_usdt:.2f}"
                if rung < exchange_min_notional:
                    return f"RUNG_BELOW_EXCHANGE_MINIMUM:{rung:.4f}<{exchange_min_notional:.4f}"
                return "CAPITAL_GUARD_REFUSED"
        return None


def _authority_for_tests() -> LiveAuthority:
    """Fixtures whose NAV is far above the micro-live band. NEVER used by production code: `tests/test_lsrl_strategy_port_no_bypass.py` scans the
    source tree and fails if this name or `unbounded=True` appears anywhere outside `tests/` and this module."""
    return LiveAuthority(TRADE, unbounded=True)


#: The non-secret effective runtime settings an approval is bound to. Any change after the owner approved (a leverage bump, a different exposure limit, another
#: attestation, the demo-evidence acceptance flag, the armed flags) changes the digest, so the approved identity no longer matches the running configuration.
RUNTIME_CONFIG_FIELDS = (
    "lsrl_mode", "lsrl_symbol", "lsrl_microlive_armed", "execution_enabled", "execution_mode", "forward_paper_only",
    "bitget_product_type", "default_leverage", "max_leverage", "max_total_exposure_pct", "max_cluster_exposure_pct",
    "execution_max_live_notional_per_trade_usdt", "lsrl_min_free_gb", "lsrl_process_down_attestation_sha256",
    "lsrl_accept_demo_process_down_evidence")


def _norm(value: Any) -> Any:
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, (bool, int, float)) or value is None:
        return value
    return repr(value)


def runtime_config_digest(settings: Any, cfg: Any, *, capital_ceiling_usdt: float, exposure_ceiling_usdt: float) -> str:
    doc = {"settings": {name: _norm(getattr(settings, name, None)) for name in RUNTIME_CONFIG_FIELDS},
           "strategy_config_digest": cfg.digest(), "capital_ceiling_usdt": float(capital_ceiling_usdt),
           "exposure_ceiling_usdt": float(exposure_ceiling_usdt)}
    return hashlib.sha256(json.dumps(doc, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def bind_authority_to_runtime(authority: LiveAuthority, *, settings: Any, cfg: Any, git_sha: str, symbol: str) -> None:
    """The runtime being built must be EXACTLY what the owner approved: this release, this frozen config, this symbol, this effective configuration."""
    if authority.mode != TRADE or authority.unbounded:
        return                                              # PROTECT_ONLY has nothing to bind; the test authority has no identity
    ident = authority.identity
    checks = (("sha", ident.sha, git_sha), ("config_hash", ident.config_hash, cfg.digest()), ("symbol", ident.symbol, symbol),
              ("runtime_config_hash", ident.runtime_config_hash,
               runtime_config_digest(settings, cfg, capital_ceiling_usdt=ident.capital_ceiling_usdt,
                                     exposure_ceiling_usdt=ident.exposure_ceiling_usdt)))
    for name, approved, running in checks:
        if approved != running:
            raise AuthorityRefused(f"IDENTITY_MISMATCH:{name}")


def admit_strategy(port: Any, authority: LiveAuthority) -> None:
    """The strategy behind the port must be the one the owner approved in the ceremony: same id, same frozen spec digest, same symbol."""
    if authority.mode != TRADE or authority.unbounded:
        return
    ident = authority.identity
    for name, approved, running in (("strategy", ident.strategy, str(port.strategy_id).upper()), ("spec_digest", ident.config_hash, port.spec_digest),
                                    ("symbol", ident.symbol, str(port.symbol).upper())):
        if approved != running:
            raise AuthorityRefused(f"STRATEGY_NOT_APPROVED:{name}")
