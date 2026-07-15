# Bitget historical data source audit — 2026-07-15

- Exchange: Bitget.
- Market: USDT-margined perpetual futures (`USDT-FUTURES`), market-price candles.
- Public endpoint: `GET /api/v2/mix/market/history-candles`; authentication is not required.
- Response timestamp: candle-open Unix timestamp in UTC milliseconds. The historical endpoint returns finished candles.
- Granularity: `15m`; canonical interval is exactly 900,000 ms.
- Page limit: 200 candles. Published frequency limit: 20 requests per second per IP.
- Range parameters: explicit `startTime` and `endTime`; requests are paginated backward from an exclusive aligned end boundary. The downloader filters every response back to the requested half-open interval.
- Retry policy: four attempts with bounded backoff; an exhausted page fails the acquisition. No failed request is accepted as an empty market interval.
- Ordering: Bitget pages are collected backward; raw page order is preserved. Canonical rows are deduplicated by candle-open timestamp and sorted chronologically.
- Missing data: never interpolated. Internal gaps are emitted with `UNKNOWN` classification unless independent exchange-availability evidence exists.
- Availability: all eight requested symbols returned the complete requested 12-month common window. No symbol was excluded.
- Isolation: public HTTP only; no `.env`, credentials, risk, coach, weekly-freeze, reports, state or running bot dependency.
- Storage: versioned raw and canonical layers, atomic file replacement, per-symbol SHA-256 and dataset manifest hash.

Official reference: https://www.bitget.com/api-doc/contract/market/Get-History-Candle-Data
