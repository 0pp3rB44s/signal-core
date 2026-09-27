from __future__ import annotations

import json
import stat
from types import SimpleNamespace

import pytest

from execution.entry_outcome import build_close_outcome
from execution.entry_routing import EntryRoutingRecorder
from execution.executor_identity import (
    ExecutionIdentity,
    ExecutionOwnershipError,
    is_legacy_client_oid,
    is_owned_client_oid,
    single_live_executor_lock,
)
from execution.execution_service import ExecutionService
from execution.order_identity import derive_entry_client_oid
from candidate_lifecycle import deterministic_plan_id
from scripts.configure_low_vol_v2_live import LIVE_VALUES, configure


IDENTITY = ExecutionIdentity(
    executor_id="runner01",
    host_id="host-a1",
    pid=123,
    production_sha="a" * 40,
    credential_fingerprint="b" * 16,
    client_id_namespace="cgc-runner01",
)


def test_single_live_executor_lock_fails_closed(tmp_path):
    path = tmp_path / "live.lock"
    with single_live_executor_lock(IDENTITY, str(path)):
        assert json.loads(path.read_text())["executor_id"] == "runner01"
        with pytest.raises(ExecutionOwnershipError, match="another LIVE executor"):
            with single_live_executor_lock(IDENTITY, str(path)):
                pass


def test_executor_namespace_replaces_and_rejects_legacy_bgai():
    candidate_id = "c" * 64
    oid = derive_entry_client_oid(
        plan_id=deterministic_plan_id(candidate_id),
        candidate_id=candidate_id,
        symbol="BTCUSDT",
        direction="LONG",
        strategy="low_vol_reclaim_v2",
        bot_identity=IDENTITY.client_id_namespace,
    )
    assert oid.startswith("cgc-runner01-")
    assert len(IDENTITY.client_id_namespace) <= 16
    assert is_legacy_client_oid("bgai-k-deadbeef")
    assert not is_owned_client_oid("bgai-k-deadbeef", IDENTITY)


def test_foreign_pending_order_blocks_ownership_audit():
    service = ExecutionService.__new__(ExecutionService)
    service.execution_identity = IDENTITY
    service._ownership_audit_done = False
    service._entry_recovery_block_reason = ""
    service.log = SimpleNamespace(critical=lambda *args, **kwargs: None)
    service.client = SimpleNamespace(
        get_pending_orders=lambda limit=100: {
            "data": {"entrustedList": [{"clientOid": "bgai-k-legacy", "orderId": "1"}]}
        },
        get_all_positions=lambda: {"data": []},
        get_tpsl_orders=lambda plan_type=None: {"data": {"entrustedList": []}},
    )
    service._audit_execution_ownership()
    assert service._ownership_audit_done is False
    assert "foreign/legacy" in service._entry_recovery_block_reason


def test_routing_row_carries_identity_and_immutable_geometry():
    recorder = EntryRoutingRecorder(
        lifecycle_id="entry-plan",
        plan_id="plan",
        candidate_id="candidate",
        symbol="BTCUSDT",
        direction="LONG",
        planned_entry=100.0,
        intended_route="maker_only",
        size_requested=1.0,
        strategy_id="low_vol_reclaim_v2",
        execution_identity=IDENTITY.as_dict(),
        original_entry=100.0,
        original_sl=99.0,
        original_tp1=102.0,
        original_tp2=103.0,
        original_rr=2.0,
    )
    row = recorder.to_row()
    assert row["executor_id"] == "runner01"
    assert row["original_sl"] == 99.0
    assert row["original_tp1"] == 102.0


def test_close_outcome_has_fee_split_excursions_and_lineage():
    position = {
        **IDENTITY.as_dict(),
        "position_lifecycle_id": "life-1",
        "strategy": "low_vol_reclaim_v2",
        "plan_id": "plan-1",
        "candidate_id": "candidate-1",
        "symbol": "BTCUSDT",
        "direction": "LONG",
        "planned_avg_entry": 100,
        "exchange_avg_entry": 100,
        "entry_via": "maker",
        "max_favorable_excursion_pct": 0.4,
        "max_adverse_excursion_pct": -0.2,
        "max_mfe_at": "2026-01-01T00:01:00Z",
        "max_mae_at": "2026-01-01T00:02:00Z",
    }
    economics = {
        "gross_pnl": 1.0, "fees": 0.12, "funding": 0.01, "net_pnl": 0.87,
        "open_fee": 0.04, "close_fee": 0.08, "close_time": "now",
    }
    row = build_close_outcome(position, economics, "test")
    assert row["executor_id"] == "runner01"
    assert row["maker_fees"] == 0.04
    assert row["taker_fees"] == 0.08
    assert row["total_fees"] == 0.12
    assert row["mfe_bps"] == 40.0
    assert row["mae_bps"] == -20.0


def test_live_v2_configurator_is_atomic_and_preserves_secrets(tmp_path):
    env_file = tmp_path / ".env.live"
    env_file.write_text(
        "BITGET_API_KEY=do-not-change\nENABLED_STRATEGIES=legacy\n# retained\n",
        encoding="utf-8",
    )
    env_file.chmod(0o600)

    configure(env_file)

    text = env_file.read_text(encoding="utf-8")
    assert "BITGET_API_KEY=do-not-change" in text
    assert "# retained" in text
    for key, value in LIVE_VALUES.items():
        assert text.count(f"{key}={value}") == 1
    assert stat.S_IMODE(env_file.stat().st_mode) == 0o600
