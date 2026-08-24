from functools import lru_cache

from pydantic import Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from app.symbol_allowlist import OWNER_APPROVED_PRODUCTION_SYMBOLS, parse_symbol_allowlist

#: Owner-approved two-position portfolio for this release. Pinned to exact
#: values rather than an upper bound: 0, 1 or 3 must all fail closed, so a
#: mistyped or silently-defaulted setting can never widen exposure.
LIVE_MAX_OPEN_POSITIONS = 2

#: Ceiling MicroFlow's own leverage may reach. Raised 5 -> 10 on 2026-08-14 by owner
#: request. This is the strategy's bound only: legacy strategies cannot open LIVE
#: entries at all (the allowlist below must be exactly microflow_scalper_v1), so
#: raising MAX_LEVERAGE does not hand them a wider permission.
MICROFLOW_MAX_ALLOWED_LEVERAGE = 10.0
LIVE_MAX_EXECUTIONS_PER_CYCLE = 2


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    app_env: str = Field(default="dev", alias="APP_ENV")
    app_mode: str = Field(default="paper", alias="APP_MODE")
    log_level: str = Field(default="INFO", alias="LOG_LEVEL")
    timezone: str = Field(default="Europe/Amsterdam", alias="TIMEZONE")
    python_unbuffered: int = Field(default=1, alias="PYTHONUNBUFFERED")

    bitget_base_url: str = Field(default="https://api.bitget.com", alias="BITGET_BASE_URL")
    bitget_api_key: SecretStr = Field(default=SecretStr(""), alias="BITGET_API_KEY")
    bitget_api_secret: SecretStr = Field(default=SecretStr(""), alias="BITGET_API_SECRET")
    bitget_api_passphrase: SecretStr = Field(default=SecretStr(""), alias="BITGET_API_PASSPHRASE")
    bitget_locale: str = Field(default="en-US", alias="BITGET_LOCALE")

    bitget_product_type: str = Field(default="USDT-FUTURES", alias="BITGET_PRODUCT_TYPE")
    bitget_margin_coin: str = Field(default="USDT", alias="BITGET_MARGIN_COIN")
    bitget_default_granularity: str = Field(default="15m", alias="BITGET_DEFAULT_GRANULARITY")
    bitget_confirmation_granularity: str = Field(default="1H", alias="BITGET_CONFIRMATION_GRANULARITY")
    bitget_candle_limit: int = Field(default=200, alias="BITGET_CANDLE_LIMIT")
    bitget_contract_cache_ttl_sec: int = Field(default=180, alias="BITGET_CONTRACT_CACHE_TTL_SEC")
    bitget_rate_limit_min_interval_ms: int = Field(default=120, alias="BITGET_RATE_LIMIT_MIN_INTERVAL_MS")
    bitget_rate_limit_429_cooldown_sec: float = Field(default=5.0, alias="BITGET_RATE_LIMIT_429_COOLDOWN_SEC")
    bitget_rate_limit_state_path: str = Field(default="state/bitget_rate_limit.json", alias="BITGET_RATE_LIMIT_STATE_PATH")
    bitget_max_request_retries: int = Field(default=3, alias="BITGET_MAX_REQUEST_RETRIES")
    bitget_retry_backoff_seconds: float = Field(default=1.25, alias="BITGET_RETRY_BACKOFF_SECONDS")
    watchlist: str = Field(
        default=(
            "BTCUSDT,ETHUSDT,SOLUSDT,NEARUSDT,WIFUSDT,DOGEUSDT,AVAXUSDT,LINKUSDT,"
            "OPUSDT,ARBUSDT,INJUSDT,FETUSDT,ADAUSDT,BNBUSDT,APTUSDT,ATOMUSDT,"
            "LDOUSDT,UNIUSDT,TIAUSDT,ENAUSDT,BCHUSDT,RUNEUSDT,SEIUSDT,ICPUSDT,"
            "AAVEUSDT,XLMUSDT,TRXUSDT,FILUSDT"
        ),
        alias="WATCHLIST",
    )
    # Empty by default on purpose.  A LIVE process must receive an explicit,
    # owner-confirmed value; the broad development watchlist is never promoted.
    production_symbol_allowlist: str = Field(
        default="",
        alias="PRODUCTION_SYMBOL_ALLOWLIST",
    )
    allow_auto_watchlist_refresh: bool = Field(default=True, alias="ALLOW_AUTO_WATCHLIST_REFRESH")
    min_usdt_volume_24h: float = Field(default=10_000_000, alias="MIN_USDT_VOLUME_24H")
    min_change_pct_24h_abs: float = Field(default=1.5, alias="MIN_CHANGE_PCT_24H_ABS")
    max_symbols: int = Field(default=28, alias="MAX_SYMBOLS")
    strategy_debug_symbols: str = Field(
        default="NEARUSDT,FETUSDT,FILUSDT,OPUSDT,ADAUSDT,LINKUSDT,WIFUSDT,AAVEUSDT",
        alias="STRATEGY_DEBUG_SYMBOLS",
    )
    momentum_funnel_audit: bool = Field(default=True, alias="MOMENTUM_FUNNEL_AUDIT")
    breakout_context_min_expansion_prob: float = Field(default=70.0, alias="BREAKOUT_CONTEXT_MIN_EXPANSION_PROB")
    breakout_context_min_pressure_score: float = Field(default=45.0, alias="BREAKOUT_CONTEXT_MIN_PRESSURE_SCORE")
    breakout_context_min_structure_score: float = Field(default=1.0, alias="BREAKOUT_CONTEXT_MIN_STRUCTURE_SCORE")
    breakout_context_high_prob_pressure_floor: float = Field(default=35.0, alias="BREAKOUT_CONTEXT_HIGH_PROB_PRESSURE_FLOOR")
    enable_shorts: bool = Field(default=True, alias="ENABLE_SHORTS")
    strategy_isolation_enabled: bool = Field(default=False, alias="STRATEGY_ISOLATION_ENABLED")
    enabled_strategies: str = Field(default="", alias="ENABLED_STRATEGIES")
    disabled_strategies: str = Field(default="", alias="DISABLED_STRATEGIES")
    executor_id: str = Field(default="", alias="EXECUTOR_ID")
    host_id: str = Field(default="", alias="HOST_ID")

    # Separate, fail-closed MicroFlow pilot surface. These settings do not
    # reuse maker-entry or legacy strategy switches.
    microflow_scalper_enabled: bool = Field(default=False, alias="MICROFLOW_SCALPER_ENABLED")
    microflow_symbols: str = Field(default="", alias="MICROFLOW_SYMBOLS")
    microflow_leverage: float = Field(default=1.0, alias="MICROFLOW_LEVERAGE")
    microflow_margin_reserve_pct: float = Field(default=10.0, alias="MICROFLOW_MARGIN_RESERVE_PCT")
    microflow_max_notional_pct_equity: float = Field(default=500.0, alias="MICROFLOW_MAX_NOTIONAL_PCT_EQUITY")
    microflow_max_loss_pct_equity: float = Field(default=2.0, alias="MICROFLOW_MAX_LOSS_PCT_EQUITY")
    microflow_max_slippage_bps: float = Field(default=1.0, alias="MICROFLOW_MAX_SLIPPAGE_BPS")
    microflow_data_dir: str = Field(default="data_store/microflow_live", alias="MICROFLOW_DATA_DIR")

    # dynamic_grid_v1 is an isolated, fail-closed pilot. OFF and SHADOW can
    # never place grid orders; LIVE additionally requires the global LIVE gate.
    dynamic_grid_enabled: bool = Field(default=False, alias="DYNAMIC_GRID_ENABLED")
    dynamic_grid_mode: str = Field(default="OFF", alias="DYNAMIC_GRID_MODE")
    dynamic_grid_symbols: str = Field(default="BTCUSDT,SOLUSDT", alias="DYNAMIC_GRID_SYMBOLS")
    dynamic_grid_max_active_grids: int = Field(default=1, alias="DYNAMIC_GRID_MAX_ACTIVE_GRIDS")
    dynamic_grid_levels: int = Field(default=3, alias="DYNAMIC_GRID_LEVELS")
    dynamic_grid_leverage: float = Field(default=1.0, alias="DYNAMIC_GRID_LEVERAGE")
    dynamic_grid_max_notional_usdt: float = Field(default=30.0, alias="DYNAMIC_GRID_MAX_NOTIONAL_USDT")
    dynamic_grid_max_equity_pct: float = Field(default=3.0, alias="DYNAMIC_GRID_MAX_EQUITY_PCT")
    dynamic_grid_max_equity_risk_pct: float = Field(default=0.25, alias="DYNAMIC_GRID_MAX_EQUITY_RISK_PCT")
    dynamic_grid_max_drawdown_pct: float = Field(default=0.5, alias="DYNAMIC_GRID_MAX_DRAWDOWN_PCT")
    dynamic_grid_max_order_errors: int = Field(default=3, alias="DYNAMIC_GRID_MAX_ORDER_ERRORS")
    dynamic_grid_min_level_notional_usdt: float = Field(default=5.0, alias="DYNAMIC_GRID_MIN_LEVEL_NOTIONAL_USDT")
    dynamic_grid_max_level_notional_usdt: float = Field(default=10.0, alias="DYNAMIC_GRID_MAX_LEVEL_NOTIONAL_USDT")
    dynamic_grid_min_score: float = Field(default=70.0, alias="DYNAMIC_GRID_MIN_SCORE")
    dynamic_grid_min_atr_bps: float = Field(default=8.0, alias="DYNAMIC_GRID_MIN_ATR_BPS")
    dynamic_grid_max_atr_bps: float = Field(default=120.0, alias="DYNAMIC_GRID_MAX_ATR_BPS")
    dynamic_grid_max_spread_bps: float = Field(default=5.0, alias="DYNAMIC_GRID_MAX_SPREAD_BPS")
    dynamic_grid_max_trend_bps: float = Field(default=45.0, alias="DYNAMIC_GRID_MAX_TREND_BPS")
    dynamic_grid_min_depth_usdt: float = Field(default=100_000.0, alias="DYNAMIC_GRID_MIN_DEPTH_USDT")
    dynamic_grid_drag_bps: float = Field(default=1.0, alias="DYNAMIC_GRID_DRAG_BPS")
    dynamic_grid_edge_margin_bps: float = Field(default=2.0, alias="DYNAMIC_GRID_EDGE_MARGIN_BPS")
    dynamic_grid_reset_atr: float = Field(default=0.75, alias="DYNAMIC_GRID_RESET_ATR")
    dynamic_grid_reset_cooldown_minutes: int = Field(default=30, alias="DYNAMIC_GRID_RESET_COOLDOWN_MINUTES")
    dynamic_grid_hard_invalidation_atr: float = Field(default=3.0, alias="DYNAMIC_GRID_HARD_INVALIDATION_ATR")
    dynamic_grid_state_path: str = Field(default="state/dynamic_grid_v1.json", alias="DYNAMIC_GRID_STATE_PATH")
    dynamic_grid_shadow_state_path: str = Field(default="state/dynamic_grid_v1_shadow.json", alias="DYNAMIC_GRID_SHADOW_STATE_PATH")
    dynamic_grid_events_path: str = Field(default="data_store/dynamic_grid_v1_events.jsonl", alias="DYNAMIC_GRID_EVENTS_PATH")
    old_strategies_new_entries_enabled: bool = Field(default=True, alias="OLD_STRATEGIES_NEW_ENTRIES_ENABLED")

    scan_on_start: bool = Field(default=True, alias="SCAN_ON_START")
    scan_loop_enabled: bool = Field(default=True, alias="SCAN_LOOP_ENABLED")
    scan_interval_sec: int = Field(default=60, alias="SCAN_INTERVAL_SEC")

    sweep_pivot_lookback: int = Field(default=12, alias="SWEEP_PIVOT_LOOKBACK")
    sweep_recent_bars: int = Field(default=6, alias="SWEEP_RECENT_BARS")
    sweep_reclaim_tolerance_bps: int = Field(default=12, alias="SWEEP_RECLAIM_TOLERANCE_BPS")
    min_sweep_displacement_pct: float = Field(default=0.12, alias="MIN_SWEEP_DISPLACEMENT_PCT")
    min_sweep_volume_ratio: float = Field(default=1.15, alias="MIN_SWEEP_VOLUME_RATIO")
    strategy_candidate_limit: int = Field(default=5, alias="STRATEGY_CANDIDATE_LIMIT")
    strategy_score_go_threshold: float = Field(default=70.0, alias="STRATEGY_SCORE_GO_THRESHOLD")
    strategy_score_watch_threshold: float = Field(default=60.0, alias="STRATEGY_SCORE_WATCH_THRESHOLD")
    momentum_min_volume_ratio: float = Field(default=1.2, alias="MOMENTUM_MIN_VOLUME_RATIO")
    momentum_breakdown_min_volume_ratio: float = Field(default=1.2, alias="MOMENTUM_BREAKDOWN_MIN_VOLUME_RATIO")

    account_equity_usdt: float = Field(default=1000.0, alias="ACCOUNT_EQUITY_USDT")
    account_balance_usdt: float = Field(default=0.0, alias="ACCOUNT_BALANCE_USDT")
    account_risk_per_trade_pct: float = Field(default=0.75, alias="ACCOUNT_RISK_PER_TRADE_PCT")
    default_leverage: float = Field(default=5.0, alias="DEFAULT_LEVERAGE")
    max_leverage: float = Field(default=5.0, alias="MAX_LEVERAGE")
    max_open_positions: int = Field(default=2, alias="MAX_OPEN_POSITIONS")
    max_total_exposure_pct: float = Field(default=200.0, alias="MAX_TOTAL_EXPOSURE_PCT")
    max_correlated_positions: int = Field(default=2, alias="MAX_CORRELATED_POSITIONS")
    max_cluster_exposure_pct: float = Field(default=120.0, alias="MAX_CLUSTER_EXPOSURE_PCT")
    max_daily_loss_pct: float = Field(default=1.5, alias="MAX_DAILY_LOSS_PCT")
    hard_daily_stop_pct: float = Field(default=2.0, alias="HARD_DAILY_STOP_PCT")
    weekly_freeze_loss_pct: float = Field(default=7.0, alias="WEEKLY_FREEZE_LOSS_PCT")
    planner_ladder_steps: int = Field(default=3, alias="PLANNER_LADDER_STEPS")
    planner_stop_buffer_bps: int = Field(default=8, alias="PLANNER_STOP_BUFFER_BPS")
    planner_tp1_r_multiple: float = Field(default=1.2, alias="PLANNER_TP1_R_MULTIPLE")
    planner_tp2_r_multiple: float = Field(default=1.8, alias="PLANNER_TP2_R_MULTIPLE")
    planner_tp3_r_multiple: float = Field(default=2.6, alias="PLANNER_TP3_R_MULTIPLE")
    planner_min_rr: float = Field(default=1.2, alias="PLANNER_MIN_RR")
    planner_min_rr_to_tp1: float = Field(default=1.0, alias="PLANNER_MIN_RR_TO_TP1")
    planner_strong_continuation_min_rr_to_tp1: float = Field(default=1.0, alias="PLANNER_STRONG_CONTINUATION_MIN_RR_TO_TP1")
    planner_adaptive_fallback_min_rr_to_tp1: float = Field(default=1.0, alias="PLANNER_ADAPTIVE_FALLBACK_MIN_RR_TO_TP1")
    planner_estimated_roundtrip_fee_bps: float = Field(default=12.0, alias="PLANNER_ESTIMATED_ROUNDTRIP_FEE_BPS")
    planner_minimum_net_edge_buffer_bps: float = Field(default=4.0, alias="PLANNER_MINIMUM_NET_EDGE_BUFFER_BPS")
    planner_largest_loss_guard_bps: float = Field(default=85.0, alias="PLANNER_LARGEST_LOSS_GUARD_BPS")
    planner_max_notional_pct_of_equity: float = Field(default=35.0, alias="PLANNER_MAX_NOTIONAL_PCT_OF_EQUITY")
    planner_max_notional_per_trade_usdt: float = Field(default=35.0, alias="PLANNER_MAX_NOTIONAL_PER_TRADE_USDT")
    planner_min_live_notional_usdt: float = Field(default=10.0, alias="PLANNER_MIN_LIVE_NOTIONAL_USDT")
    symbol_cooldown_minutes: int = Field(default=30, alias="SYMBOL_COOLDOWN_MINUTES")
    # Legacy, mutually-exclusive fallback only. Critical BE calculations use
    # the itemised Decimal model below whenever confirmed entry/size exist.
    break_even_fee_buffer_pct: float = Field(default=0.12, alias="BREAK_EVEN_FEE_BUFFER_PCT")
    # Decimal rate, not percent. Used only when no exchange-confirmed opening
    # fee amount or exchange opening fee rate is available.
    break_even_open_fee_fallback_rate: float = Field(
        default=0.0006,
        alias="BREAK_EVEN_OPEN_FEE_FALLBACK_RATE",
    )
    # Decimal rate, not percent: 0.0006 = 6 bps. Stops are market-triggered, so
    # the conservative taker rate is assumed for the expected closing fill.
    break_even_expected_close_fee_rate: float = Field(
        default=0.0006,
        alias="BREAK_EVEN_EXPECTED_CLOSE_FEE_RATE",
    )
    break_even_spread_buffer_pct: float = Field(
        default=0.02,
        alias="BREAK_EVEN_SPREAD_BUFFER_PCT",
    )
    break_even_slippage_buffer_pct: float = Field(
        default=0.03,
        alias="BREAK_EVEN_SLIPPAGE_BUFFER_PCT",
    )
    break_even_extra_buffer_pct: float = Field(
        default=0.01,
        alias="BREAK_EVEN_EXTRA_BUFFER_PCT",
    )
    break_even_mark_safety_ticks: int = Field(
        default=2,
        alias="BREAK_EVEN_MARK_SAFETY_TICKS",
    )
    # Fatal migration assertions are development/test-only. Even when set,
    # position_model.py suppresses them for LIVE execution.
    position_model_dev_assertions: bool = Field(
        default=False,
        alias="POSITION_MODEL_DEV_ASSERTIONS",
    )
    # UTC hour windows where live results are historically negative; risk is
    # multiplied down (never up) inside them. Format: "08-12,23-01" (end exclusive).
    session_risk_reduction_windows_utc: str = Field(default="08-12,23-01", alias="SESSION_RISK_REDUCTION_WINDOWS_UTC")
    session_risk_multiplier: float = Field(default=0.5, alias="SESSION_RISK_MULTIPLIER")
    # Fast lane: 5m-entry detectie op de sterkste symbolen van de basisscan.
    # Frequentie komt uit meer detectiekansen; alle kwaliteits- en fee-poorten
    # blijven identiek gelden.
    fast_lane_enabled: bool = Field(default=True, alias="FAST_LANE_ENABLED")
    fast_lane_symbols: int = Field(default=8, alias="FAST_LANE_SYMBOLS")
    fast_lane_min_score_hint: float = Field(default=50.0, alias="FAST_LANE_MIN_SCORE_HINT")
    fast_lane_granularity: str = Field(default="5m", alias="FAST_LANE_GRANULARITY")
    fast_lane_confirmation_granularity: str = Field(default="15m", alias="FAST_LANE_CONFIRMATION_GRANULARITY")
    # Maker-entry experiment (fees zijn 197% van de bruto-edge). Post-only
    # limit-entry i.p.v. market -> maker-fee i.p.v. taker. STANDAARD UIT tot
    # gevalideerd in een bewaakt venster. Vult de limit niet binnen het
    # wachtvenster -> annuleren en trade skippen (geen taker-fallback).
    #: Capture bid/ask/mark at each entry routing stage. Off by default: the
    #: two GETs sit on the path between plan and submit and would shift the
    #: fill price this observability exists to measure.
    entry_routing_quote_capture: bool = Field(
        default=False, alias="ENTRY_ROUTING_QUOTE_CAPTURE"
    )

    # Default OFF, deliberately. Independent of the weekly freeze -- both
    # gates must pass for a live entry to happen; this flag is not a
    # substitute for risk gating, only an additional off switch. Owner-gated:
    # set to true only by explicit separate authorization, after the full
    # entry/protection/race-scenario test suite is proven, never as a side
    # effect of any code change.
    adaptive_trend_live_entry_enabled: bool = Field(
        default=False, alias="ADAPTIVE_TREND_LIVE_ENTRY_ENABLED"
    )

    maker_entry_enabled: bool = Field(default=False, alias="MAKER_ENTRY_ENABLED")
    # Hybride: vult de maker-limit niet, dan alsnog een market-order (taker)
    # i.p.v. de trade skippen. True = nooit een trade missen + fee besparen waar
    # het kan; False = pure maker (skip bij niet-vullen). Live-data 2026-07-08:
    # maker-fill-rate ~0% bij 4s -> hybride nodig om te blijven traden.
    maker_entry_fallback_market: bool = Field(default=True, alias="MAKER_ENTRY_FALLBACK_MARKET")
    maker_entry_wait_seconds: float = Field(default=4.0, alias="MAKER_ENTRY_WAIT_SECONDS")
    maker_entry_poll_seconds: float = Field(default=1.0, alias="MAKER_ENTRY_POLL_SECONDS")
    # Limit iets binnen de markt zetten (bps) zodat hij snel als maker vult
    # zonder de spread te kruisen. 0 = precies op de marktprijs-anker.
    maker_entry_offset_bps: float = Field(default=1.0, alias="MAKER_ENTRY_OFFSET_BPS")
    # Dead-trade timeout: a flat trade past its window occupies a slot another
    # setup could use. 0 disables. Only fires on flat trades (|pnl| below the
    # max) that never hit TP1, with verified live exchange state.
    dead_trade_timeout_reclaim_minutes: float = Field(default=90.0, alias="DEAD_TRADE_TIMEOUT_RECLAIM_MINUTES")
    dead_trade_timeout_default_minutes: float = Field(default=240.0, alias="DEAD_TRADE_TIMEOUT_DEFAULT_MINUTES")
    dead_trade_max_abs_pnl_pct: float = Field(default=0.20, alias="DEAD_TRADE_MAX_ABS_PNL_PCT")
    # Profit-lock (P1.1A): once MFE reaches this fraction of the TP1 distance,
    # move SL to fee-adjusted break-even. Evidence 2026-07-07: median trade
    # peaked at 50-64% of TP1 with ~zero MAE, then reversed into a loss.
    profit_lock_tp1_fraction: float = Field(default=0.60, alias="PROFIT_LOCK_TP1_FRACTION")

    execution_enabled: bool = Field(default=False, alias="EXECUTION_ENABLED")
    execution_mode: str = Field(default="DRY_RUN", alias="EXECUTION_MODE")
    execution_require_confirmation: bool = Field(default=True, alias="EXECUTION_REQUIRE_CONFIRMATION")
    execution_confirm_symbols: str = Field(default="", alias="EXECUTION_CONFIRM_SYMBOLS")
    execution_margin_mode: str = Field(default="isolated", alias="EXECUTION_MARGIN_MODE")
    execution_max_per_cycle: int = Field(default=1, alias="EXECUTION_MAX_PER_CYCLE")
    execution_plan_limit: int = Field(default=2, alias="EXECUTION_PLAN_LIMIT")
    execution_max_live_notional_per_trade_usdt: float = Field(default=35.0, alias="EXECUTION_MAX_LIVE_NOTIONAL_PER_TRADE_USDT")
    execution_min_live_notional_usdt: float = Field(default=10.0, alias="EXECUTION_MIN_LIVE_NOTIONAL_USDT")

    # Strict public-data runtime. This is deliberately stronger than ordinary
    # observe/DRY_RUN mode: private exchange surfaces are unavailable.
    forward_paper_only: bool = Field(default=False, alias="FORWARD_PAPER_ONLY")

    # Forward-paper is an isolated observer. It never calls exchange order or
    # account endpoints and writes only to data_store/forward_paper_*.
    forward_paper_enabled: bool = Field(default=True, alias="FORWARD_PAPER_ENABLED")
    forward_paper_roundtrip_fee_bps: float = Field(default=12.0, alias="FORWARD_PAPER_ROUNDTRIP_FEE_BPS")
    forward_paper_liquidity_assumption: str = Field(default="taker", alias="FORWARD_PAPER_LIQUIDITY_ASSUMPTION")
    forward_paper_events_path: str = Field(default="data_store/forward_paper_events.jsonl", alias="FORWARD_PAPER_EVENTS_PATH")
    forward_paper_outcomes_path: str = Field(default="data_store/forward_paper_outcomes.csv", alias="FORWARD_PAPER_OUTCOMES_PATH")
    forward_paper_quality_path: str = Field(default="reports/forward_paper_data_quality.json", alias="FORWARD_PAPER_QUALITY_PATH")

    # NON-PRODUCTION engineering test strategy. It exists only to validate that the
    # paper execution lifecycle can open, manage, close, persist and restore a
    # position. It is not an edge claim and is forcibly disabled unless the runtime
    # is strict forward-paper-only (see enforce_forward_paper_only below).
    forward_paper_smoke_strategy_enabled: bool = Field(default=False, alias="FORWARD_PAPER_SMOKE_STRATEGY_ENABLED")
    forward_paper_smoke_symbol: str = Field(default="SOLUSDT", alias="FORWARD_PAPER_SMOKE_SYMBOL")
    forward_paper_smoke_stop_pct: float = Field(default=0.35, alias="FORWARD_PAPER_SMOKE_STOP_PCT")
    forward_paper_smoke_target_pct: float = Field(default=0.35, alias="FORWARD_PAPER_SMOKE_TARGET_PCT")
    forward_paper_smoke_notional_usdt: float = Field(default=25.0, alias="FORWARD_PAPER_SMOKE_NOTIONAL_USDT")

    position_manager_enabled: bool = Field(default=True, alias="POSITION_MANAGER_ENABLED")

    dashboard_enabled: bool = Field(default=True, alias="DASHBOARD_ENABLED")
    dashboard_host: str = Field(default="127.0.0.1", alias="DASHBOARD_HOST")
    dashboard_port: int = Field(default=8501, alias="DASHBOARD_PORT")
    dashboard_debug: bool = Field(default=False, alias="DASHBOARD_DEBUG")
    dashboard_password: SecretStr = Field(default=SecretStr(""), alias="DASHBOARD_PASSWORD")
    dashboard_secret_key: SecretStr = Field(default=SecretStr(""), alias="DASHBOARD_SECRET_KEY")
    position_sync_on_start: bool = Field(default=True, alias="POSITION_SYNC_ON_START")
    position_loop_enabled: bool = Field(default=True, alias="POSITION_LOOP_ENABLED")
    position_check_interval_sec: int = Field(default=30, alias="POSITION_CHECK_INTERVAL_SEC")
    move_stop_to_be_after_tp1: bool = Field(default=True, alias="MOVE_STOP_TO_BE_AFTER_TP1")
    tp1_close_pct: float = Field(default=40.0, alias="TP1_CLOSE_PCT")
    tp2_close_pct: float = Field(default=30.0, alias="TP2_CLOSE_PCT")
    tp3_close_pct: float = Field(default=30.0, alias="TP3_CLOSE_PCT")
    tp3_close_all_remainder: bool = Field(default=True, alias="TP3_CLOSE_ALL_REMAINDER")

    @model_validator(mode="after")
    def enforce_forward_paper_only(self) -> "Settings":
        grid_mode = self.dynamic_grid_mode.strip().upper()
        if grid_mode not in {"OFF", "SHADOW", "LIVE"}:
            raise ValueError("DYNAMIC_GRID_MODE must be OFF, SHADOW, or LIVE")
        if not self.dynamic_grid_enabled and grid_mode != "OFF":
            raise ValueError("DYNAMIC_GRID_MODE requires DYNAMIC_GRID_ENABLED=true")
        if self.dynamic_grid_enabled:
            symbols = tuple(
                symbol.strip().upper()
                for symbol in self.dynamic_grid_symbols.split(",")
                if symbol.strip()
            )
            if symbols != ("BTCUSDT", "SOLUSDT"):
                raise ValueError("dynamic_grid_v1 symbols are frozen to BTCUSDT,SOLUSDT")
            if self.dynamic_grid_levels != 3:
                raise ValueError("dynamic_grid_v1 requires exactly 3 levels")
            if self.dynamic_grid_max_active_grids != 1:
                raise ValueError("dynamic_grid_v1 requires max active grids=1")
            if self.dynamic_grid_leverage != 1.0:
                raise ValueError("dynamic_grid_v1 requires 1x leverage")
            if (
                self.dynamic_grid_max_notional_usdt <= 0
                or self.dynamic_grid_max_equity_pct <= 0
                or self.dynamic_grid_max_equity_risk_pct <= 0
                or self.dynamic_grid_max_drawdown_pct <= 0
                or self.dynamic_grid_max_order_errors <= 0
                or self.dynamic_grid_max_level_notional_usdt <= 0
            ):
                raise ValueError("dynamic_grid_v1 exposure caps must be positive")
        if grid_mode == "LIVE":
            if not self.is_live_execution:
                raise ValueError("dynamic_grid_v1 LIVE requires the global LIVE execution gate")
            if self.old_strategies_new_entries_enabled:
                raise ValueError("dynamic_grid_v1 LIVE requires old strategy entries disabled")
            if self.maker_entry_fallback_market:
                raise ValueError("dynamic_grid_v1 LIVE forbids maker-to-market entry fallback")
        if self.forward_paper_only:
            self.execution_enabled = False
            self.execution_mode = "DRY_RUN"
            self.forward_paper_enabled = True
            self.position_manager_enabled = False
            self.position_loop_enabled = False
            self.position_sync_on_start = False
        # The smoke strategy fabricates entries, so it must never be reachable by
        # anything that can place a real order. Strict forward-paper-only is the
        # only runtime that guarantees no private exchange surface is in play.
        if self.forward_paper_smoke_strategy_enabled and not (
            self.forward_paper_only
            and self.forward_paper_enabled
            and not self.execution_enabled
        ):
            self.forward_paper_smoke_strategy_enabled = False

        if self.is_live_execution:
            symbols = parse_symbol_allowlist(
                self.production_symbol_allowlist,
                required=True,
            )
            if self.max_open_positions != LIVE_MAX_OPEN_POSITIONS:
                raise ValueError(
                    f"LIVE requires MAX_OPEN_POSITIONS={LIVE_MAX_OPEN_POSITIONS}")
            if self.execution_max_per_cycle != LIVE_MAX_EXECUTIONS_PER_CYCLE:
                raise ValueError(
                    f"LIVE requires EXECUTION_MAX_PER_CYCLE={LIVE_MAX_EXECUTIONS_PER_CYCLE}")
            if self.max_symbols != len(symbols):
                raise ValueError(
                    "LIVE MAX_SYMBOLS must equal the canonical production allowlist size"
                )
            if self.allow_auto_watchlist_refresh:
                raise ValueError("LIVE requires ALLOW_AUTO_WATCHLIST_REFRESH=false")
            if not self.execution_require_confirmation:
                raise ValueError("LIVE requires EXECUTION_REQUIRE_CONFIRMATION=true")
            if self.execution_margin_mode.strip().lower() != "isolated":
                raise ValueError("LIVE requires EXECUTION_MARGIN_MODE=isolated")
            if self.is_production:
                if tuple(symbols) != OWNER_APPROVED_PRODUCTION_SYMBOLS:
                    raise ValueError(
                        "production LIVE requires exactly the owner-approved allowlist "
                        f"({len(OWNER_APPROVED_PRODUCTION_SYMBOLS)} symbols): "
                        + ",".join(OWNER_APPROVED_PRODUCTION_SYMBOLS)
                    )
                if not self.strategy_isolation_enabled:
                    raise ValueError("production LIVE requires STRATEGY_ISOLATION_ENABLED=true")
                if self.enabled_strategy_set == {"microflow_scalper_v1"}:
                    if not self.microflow_scalper_enabled:
                        raise ValueError("production LIVE requires MICROFLOW_SCALPER_ENABLED=true")
                    if parse_symbol_allowlist(self.microflow_symbols, required=True) != OWNER_APPROVED_PRODUCTION_SYMBOLS:
                        raise ValueError("MICROFLOW_SYMBOLS must equal the approved production universe")
                    if not (0 < self.microflow_leverage <= MICROFLOW_MAX_ALLOWED_LEVERAGE):
                        raise ValueError(
                            f"MICROFLOW_LEVERAGE must be >0 and <={MICROFLOW_MAX_ALLOWED_LEVERAGE:g}")
                    if self.microflow_leverage > self.max_leverage:
                        raise ValueError("MICROFLOW_LEVERAGE may not exceed MAX_LEVERAGE")
                    # Sizing bounds. Each fails closed: a missing or absurd value
                    # must stop the bot, never silently widen exposure.
                    if not (0.0 <= self.microflow_margin_reserve_pct < 100.0):
                        raise ValueError("MICROFLOW_MARGIN_RESERVE_PCT must be >=0 and <100")
                    if not (0 < self.microflow_max_notional_pct_equity <= 1000.0):
                        raise ValueError("MICROFLOW_MAX_NOTIONAL_PCT_EQUITY must be >0 and <=1000")
                    if not (0 < self.microflow_max_loss_pct_equity <= 5.0):
                        raise ValueError("MICROFLOW_MAX_LOSS_PCT_EQUITY must be >0 and <=5")
                    if not (0 < self.microflow_max_slippage_bps <= 1.0):
                        raise ValueError("MICROFLOW_MAX_SLIPPAGE_BPS must be >0 and <=1")
                elif self.enabled_strategy_set == {"adaptive_trend_tsmom_v1"}:
                    # Owner-gated: mirrors the identical coupling enforced in
                    # ExecutionService.execute()'s HYBRID SAFE MODE gate and in
                    # scripts/lib/env_guard.sh -- ENABLED_STRATEGIES alone is
                    # not sufficient for this strategy.
                    if not self.adaptive_trend_live_entry_enabled:
                        raise ValueError(
                            "production LIVE requires ADAPTIVE_TREND_LIVE_ENTRY_ENABLED=true "
                            "for adaptive_trend_tsmom_v1"
                        )
                else:
                    raise ValueError(
                        "production LIVE allowlist must be exactly microflow_scalper_v1 "
                        "or adaptive_trend_tsmom_v1"
                    )
                if self.max_leverage > MICROFLOW_MAX_ALLOWED_LEVERAGE:
                    raise ValueError(
                        f"MAX_LEVERAGE may not exceed {MICROFLOW_MAX_ALLOWED_LEVERAGE:g} in production LIVE")
                if self.old_strategies_new_entries_enabled:
                    raise ValueError("production LIVE requires OLD_STRATEGIES_NEW_ENTRIES_ENABLED=false")
                if self.dynamic_grid_enabled or grid_mode != "OFF":
                    raise ValueError("dynamic grid must remain OFF for the v2 pilot")
                required_explicit = {
                    "execution_margin_mode",
                    "break_even_open_fee_fallback_rate",
                    "break_even_expected_close_fee_rate",
                    "break_even_spread_buffer_pct",
                    "break_even_slippage_buffer_pct",
                    "break_even_extra_buffer_pct",
                    "break_even_fee_buffer_pct",
                    "break_even_mark_safety_ticks",
                }
                missing = sorted(required_explicit - self.model_fields_set)
                if missing:
                    raise ValueError(
                        "production LIVE requires explicit safety config: "
                        + ",".join(missing)
                    )
        return self

    @property
    def is_production(self) -> bool:
        return self.app_env.strip().lower() == "production"

    @property
    def is_dev(self) -> bool:
        return self.app_env.strip().lower() in {"dev", "development", "local"}

    @property
    def is_live_execution(self) -> bool:
        return (
            self.execution_enabled
            and self.execution_mode.strip().upper() == "LIVE"
        )

    @property
    def watchlist_symbols(self) -> list[str]:
        if self.is_live_execution:
            return list(self.production_symbols)
        return [s.strip().upper() for s in self.watchlist.split(",") if s.strip()]

    @property
    def production_symbols(self) -> tuple[str, ...]:
        return parse_symbol_allowlist(
            self.production_symbol_allowlist,
            required=self.is_live_execution,
        )

    @property
    def production_symbol_set(self) -> frozenset[str]:
        return frozenset(self.production_symbols)

    @property
    def strategy_debug_symbol_set(self) -> set[str]:
        return {s.strip().upper() for s in self.strategy_debug_symbols.split(",") if s.strip()}

    @property
    def enabled_strategy_set(self) -> set[str]:
        return {s.strip().lower() for s in self.enabled_strategies.split(",") if s.strip()}

    @property
    def disabled_strategy_set(self) -> set[str]:
        return {s.strip().lower() for s in self.disabled_strategies.split(",") if s.strip()}


    @property
    def execution_confirm_symbol_set(self) -> set[str]:
        if self.is_live_execution:
            return set(self.production_symbols)
        return {s.strip().upper() for s in self.execution_confirm_symbols.split(",") if s.strip()}

    @property
    def dynamic_grid_symbol_set(self) -> frozenset[str]:
        return frozenset(
            symbol.strip().upper()
            for symbol in self.dynamic_grid_symbols.split(",")
            if symbol.strip()
        )

    @property
    def microflow_symbol_set(self) -> frozenset[str]:
        return frozenset(
            symbol.strip().upper()
            for symbol in self.microflow_symbols.split(",")
            if symbol.strip()
        )


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
