# Exchange-truth analyse — Bitget position history export

**Bron:** `data_store/exchange_exports/bitget_position_history_2026-06-30_2026-07-12.csv`
(eigenaar-export 2026-07-13 03:34; tijden in export zijn CEST, hier genormaliseerd naar UTC).
Cijfers: `reports/exchange_truth_analysis_20260713.json`. "Net" = Position Pnl (na fees),
geverifieerd tegen Bitget netProfit.

## Totaal (30 juni – 12 juli, 206 trades)

| | |
|---|---|
| Netto | **+$0,40** |
| Bruto | +$5,66 |
| Fees | **$5,25 (93% van de bruto winst)** |
| Win rate | 33,5% |
| Profit factor | 1,03 |

De +$0,40 komt volledig van 4 grote SOL-trades (1–3 juli, +$6,83, holds van uren tot
een dag). **De bot-era vanaf 5 juli is elke dag negatief** (202 trades, ~-$6,43):

| Dag | n | net | WR |
|---|---|---|---|
| 07-05 | 8 | -0,26 | 38% |
| 07-06 | 9 | -0,36 | 33% |
| 07-07 | 28 | -0,67 | 32% |
| 07-08 | 54 | -1,23 | 32% |
| 07-09 | 32 | -1,58 | 19% |
| 07-10 | 40 | -1,11 | 35% |
| 07-11 | 18 | -0,39 | 50% |
| 07-12 | 13 | -0,84 | 31% |

Trend: de dagverliezen krimpen sinds 07-09 (pauzes + geometrie-fixes), maar het teken
blijft negatief.

## De twee dominante patronen (exchange-bewijs, geen bot-data)

**1. Shorts verliezen structureel, longs winnen:**

| Richting | n | net | WR | PF |
|---|---|---|---|---|
| LONG | 92 | **+3,45** | 38% | **1,68** |
| SHORT | 114 | **-3,05** | 30% | 0,60 |

**2. Churn onder het uur is de verliesmachine:**

| Hold | n | net |
|---|---|---|
| <30m | 128 | **-3,27** |
| 30–60m | 46 | **-2,04** |
| 1–4h | 30 | +1,31 |
| >4h | 2 | +4,39 |

## Per strategie (gematcht met bot-data op symbol+richting+open-tijd)

| Strategie | n | net | WR | PF |
|---|---|---|---|---|
| low_vol_reclaim | 109 | -3,86 | 23% | 0,45 |
| momentum_breakout | 35 | -0,50 | **54%** | 0,63 |
| momentum_breakdown | 17 | -0,72 | 35% | 0,53 |
| trend_continuation | 13 | -0,90 | 23% | **0,20** |

momentum wint vaker dan hij verliest maar de gemiddelde loss (-0,09) is ~2x de
gemiddelde win — het oogst/stop-ratio is het probleem, niet de richtingskeuze.
trend_continuation is in dit venster de slechtste PF, ondanks zijn goede reputatie
in het (kleinere) journal-sample.

## Post-fix cohort (entries sinds 2026-07-11 14:30 UTC, n=21)

Netto **-1,33**, WR 33%, PF 0,17. momentum: n=10, WR 50%, -0,33. avg win +0,04 vs
avg loss -0,115: winners worden bij BE/TP1 geoogst, losers lopen naar de volle stop.
De geometrie-fixes verbeteren de mechaniek (3 TP-hits op 07-12), maar het cohort is
nog niet positief — de R-verhouding is scheef.

## Data-integriteit

- **31 export-trades ontbreken in de bot-dataset** (waaronder de 4 SOL-trades =
  handmatig, maar óók ~27 bot-trades van 07-06 t/m 07-10: AAVE/ENA/FIL/APT/INJ/ATOM
  e.a.) — dezelfde close-pad-bugfamilie als de ENA-case van 07-12; de lopende
  close-pad-fix-taak moet deze backfillen.
- Waar de dataset wél een rij heeft, klopt hij: 1 mismatch op 175 gematchte trades.

## Conclusies voor besluitvorming (eigenaar)

1. **Shorts beperken/uitschakelen** heeft het grootste directe effect: -3,05 over
   114 trades, terwijl longs +3,45 opleveren. (Bestaand TODO-item, nu met
   206-trade exchange-bewijs.)
2. **Churn <1h aanpakken** (fee-margin-filter / minimum-houdtijd): -5,3 gecombineerd
   onder het uur, +5,7 erboven.
3. Fee-drag blijft de hoofdvijand: $5,25 fees op $5,66 bruto.
