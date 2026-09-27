# Unified candidate lifecycle

## Identitycontract

`candidate_lifecycle.identity.deterministic_candidate_id` is de enige identityfunctie. De invoer wordt in vaste volgorde als canonieke JSON-paren geserialiseerd: strategy (lowercase), symbol (uppercase), direction (uppercase) en candidate-candle-open als millisecondeprecisie UTC. De uitkomst is een SHA-256 hex digest. `scan_id` is nooit onderdeel van de kandidaatidentiteit.

Detectoren maken de identiteit. Runner, selector, scoring, risk en planner behouden de waarde ongewijzigd. De planner maakt afzonderlijk een deterministische `plan_id`; forward-paper maakt daarvan een afzonderlijke `trade_id`. Een liquidity sweep gebruikt de candle die sweepte en reclaimde, niet de latere scancyclus.

## Persistente lifecycle

Structured funnel schema v2 schrijft append-only hash-chained JSONL. De persistente deduplicatiesleutel is `candidate_id:event_type`. Daardoor kan een nieuwe scan of procesrestart geen reeds geschreven lifecycle-stage dupliceren. Een nieuwe candle verandert de kandidaatidentiteit en blijft toegestaan. Een corrupte chain accepteert geen nieuwe records.

Forward-paper schema v2 vereist `candidate_id` en `semantic_key` op elk nieuw event. `TRADE_OPENED` is persistent uniek per kandidaat, ook na restart. Exacte semantische retries worden idempotent genegeerd en conflicterende retries falen gesloten. Outcomes bevatten dezelfde `candidate_id`, naast `plan_id` en `trade_id`.

## Forward-paper transitioncontract

De paperstate wordt uitsluitend uit persistente events gereconstrueerd. `TRADE_OPENED` bepaalt de geopende hoeveelheid en alleen `PARTIAL_EXIT` bepaalt uitgevoerde exitgrootte. `TP_TOUCH` is een observatie en verlaagt de positie nooit. `STOP_UPDATED` bepaalt de actuele stop. `TRADE_CLOSED` is de enige terminale status en mag per trade exact eenmaal bestaan.

De geldige TP-volgorde is `TP_TOUCH -> PARTIAL_EXIT -> EXIT_REASON_TRANSITION -> TRADE_CLOSED`. Iedere stap heeft een persistente semantische sleutel. Na restart wordt de eerste ontbrekende stap hervat op basis van opened quantity, exited quantity en remaining quantity. Een persistente `EXIT_REASON_TRANSITION` is terminale intentie en wordt afgerond voordat een nieuwe marktobservatie wordt geëvalueerd.

Het quality report bevat `fragmented_transition_count`, `duplicate_semantic_transition_count`, `unresolved_open_trade_count` en `terminal_close_conflict_count`.

## Legacy

Schema-v1 forward-paperrecords worden alleen gelezen. Zij worden niet herschreven en krijgen geen afgeleide identiteit. In reads en reconstructie heten zij `LEGACY_UNLINKED`. Nieuwe schema-v2-records zonder kandidaatidentiteit falen gesloten.

## Reconstructie

`scripts/reconstruct_candidate_lifecycle.py` bouwt een deterministisch rapport met uitsluitend exacte `candidate_id`-joins. Structured funneldata heeft voorrang. Symbol/timestamp-inferentie is niet toegestaan wanneer schema v2 van toepassing is.
