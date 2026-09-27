# trade_decision_snapshots.csv — schema drift

Observed, not fixed. This document exists because the drift must be understood
before anything is migrated; a silent repair would rewrite history that other
consumers already read.

## The two shapes

The header declares seven columns:

```
timestamp,symbol,strategy,direction,verdict,score,decision_snapshot
```

Rows come in two widths. Counted on the live file at 2026-08-03:

| shape | rows | share |
|---|---|---|
| 7 fields — matches the header | 172 | 9% |
| 9 fields — does not | 1750 | 91% |

## The 9-field variant

```
[0] timestamp                      2026-08-03T14:49:09.603279+00:00
[1] timestamp  (duplicate of [0])  2026-08-03T14:49:09.603279+00:00
[2] symbol                         BTCUSDT
[3] strategy                       low_vol_reclaim
[4] direction                      LONG
[5] verdict                        EXECUTABLE
[6] score                          92.00
[7] decision_snapshot              planner_gate=... | rr_to_tp1=1.30 | ...
[8] key                            BTCUSDT|2026-08-03T14:49:09
```

Field 0 is repeated in field 1, and a `symbol|timestamp` key is appended.

## Why it matters

A reader that trusts the header takes `row[1]` as the symbol. On 91% of rows
that returns a timestamp. Every downstream field is then shifted by one, so
`score` reads the verdict and `decision_snapshot` reads the score. The failure
is silent: the values are well-formed, just the wrong ones.

`decision_snapshot` itself is not JSON despite the name — it is pipe-separated
`key=value` text carrying 17 distinct keys.

## Reading both safely

```python
row = next(csv.reader(line))
offset = 1 if len(row) == 9 else 0
symbol = row[1 + offset]
snapshot = row[6 + offset]
```

`tests/test_entry_routing_observability.py::test_existing_decision_snapshot_variants_both_parse`
pins this.

## Not done here

No writer change, no schema version, no migration, no backfill. Adding a
versioned writer requires first enumerating every consumer of this file and
proving each still reads the historical rows — that is a separate change with
its own evidence, and it is not a prerequisite for entry routing observability,
which writes to its own files.
