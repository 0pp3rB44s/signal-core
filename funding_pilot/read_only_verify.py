"""Run inside the deployed secure runtime: authenticated reads only, no writes."""
from __future__ import annotations
import tempfile
from pathlib import Path
from app.config import Settings
from clients.bitget_rest import BitgetRestClient
from funding_pilot.bitget_exchange import _rows
from funding_pilot.core import PilotLedger


def main() -> int:
    settings = Settings()  # deployed secure injection mechanism
    client = BitgetRestClient(settings=settings)
    if not client.has_credentials:
        print("BITGET_AUTH_READ_VERIFIED=NO EXTERNAL_RUNTIME_CREDENTIAL_BLOCKER=YES")
        return 2
    with tempfile.TemporaryDirectory() as tmp:
        # Empty isolated ledger deliberately classifies no production lifecycle
        # as pilot-owned; this validates reads without mutating or attributing it.
        ledger = PilotLedger(Path(tmp) / "readonly.sqlite")
        accounts = _rows(client.get_accounts())
        positions = _rows(client.get_all_positions())
        orders = _rows(client.get_pending_orders(limit=100))
        plans = []
        for plan_type in ("profit_loss", "normal_plan", "track_plan"):
            plans.extend(_rows(client.get_tpsl_orders(plan_type=plan_type)))
        history = _rows(client.get_position_history(limit=100))
        # Funding/bill permission and schema check; values and credentials are
        # never printed or persisted.
        bills = _rows(client._request("GET", "/api/v2/mix/account/bill",
            params={"productType":"USDT-FUTURES", "pageSize":"100"}, private=True))
        print("BITGET_AUTH_READ_VERIFIED=YES "
              f"accounts={len(accounts)} positions={len(positions)} regular_orders={len(orders)} "
              f"plan_orders={len(plans)} history_rows={len(history)} bill_rows={len(bills)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
