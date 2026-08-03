# Release A mutation matrix

Each named mutation is guarded by a real production boundary or call-site test.

| Mutation | Test that turns red |
|---|---|
| M1 GET error becomes `0.0` | `test_m1_get_error_is_unknown_and_never_zero` |
| M2 UNKNOWN accepted as flat | `test_m2_unknown_or_bare_closed_is_not_flat` |
| M3 post-close readback removed | `test_m3_and_m4_post_readback_precedes_cleanup` |
| M4 cleanup moved before flat proof | `test_m3_and_m4_post_readback_precedes_cleanup` |
| M5 netProfit passed as gross | `test_m5_and_m6_real_writer_preserves_literal_net_once` |
| M6 fees subtracted twice | `test_m5_and_m6_real_writer_preserves_literal_net_once` |
| M7 emergency recorder call removed | `test_m7_real_emergency_production_caller_consumes_each_identity` |
| M8 unknown holdSide becomes SHORT | `test_m8_emergency_unknown_hold_side_is_not_mapped_to_short` |
| M9 recovery moved after execution | `test_m9_startup_recovery_runs_before_real_execution_call` |
| M10 limit applied before resolved filtering | `test_m10_and_m11_resolved_filtering_precedes_limit_and_rotated_is_loaded` |
| M11 rotated provisional ignored | `test_m10_and_m11_resolved_filtering_precedes_limit_and_rotated_is_loaded` |
| M12 economic dedup disabled | `test_m12_and_m15_active_dedup_blocks_second_real_writer` |
| M13 opening time/size removed from fallback identity | `test_m13_open_time_is_required_for_lifecycle_match` |
| M14 missing money defaulted to zero | `test_m14_missing_money_is_never_zero` |
| M15 second processing writes again | `test_m12_and_m15_active_dedup_blocks_second_real_writer` |

The focused Release A suite also covers LONG/SHORT close payloads, nonzero
remaining size, malformed responses, primary fail-safe, dead-timeout, TP3,
residual cleanup, startup blocking, periodic starvation, active and rotated
dedup, ambiguous history, and restart recovery.
