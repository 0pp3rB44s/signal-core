"""Microstructuur-data-archivering (observe-only; plaatst nooit orders).

Bronnen:
- orderbook: Bitget REST merge-depth snapshots (top-50), verrijkt met features;
- funding: Bitget REST current-fund-rate polls + gesettelde funding-historie;
- liquidations: Binance USDT-M forceOrder public WebSocket (Bitget biedt geen
  publiek liquidatiekanaal; bron expliciet gelabeld per record).

Zie docs/ARCHIVING.md voor runbook, formaten en retentie.
"""
