from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    app_env: str = Field(default="dev", alias="APP_ENV")
    app_mode: str = Field(default="paper", alias="APP_MODE")
    log_level: str = Field(default="INFO", alias="LOG_LEVEL")
    timezone: str = Field(default="Europe/Amsterdam", alias="TIMEZONE")
    python_unbuffered: int = Field(default=1, alias="PYTHONUNBUFFERED")

    bitget_base_url: str = Field(default="https://api.bitget.com", alias="BITGET_BASE_URL")
    bitget_api_key: str = Field(default="", alias="BITGET_API_KEY")
    bitget_api_secret: str = Field(default="", alias="BITGET_API_SECRET")
    bitget_api_passphrase: str = Field(default="", alias="BITGET_API_PASSPHRASE")
    bitget_locale: str = Field(default="en-US", alias="BITGET_LOCALE")

    bitget_product_type: str = Field(default="USDT-FUTURES", alias="BITGET_PRODUCT_TYPE")
    bitget_margin_coin: str = Field(default="USDT", alias="BITGET_MARGIN_COIN")
    bitget_default_granularity: str = Field(default="15m", alias="BITGET_DEFAULT_GRANULARITY")
    bitget_confirmation_granularity: str = Field(default="1H", alias="BITGET_CONFIRMATION_GRANULARITY")
    bitget_candle_limit: int = Field(default=200, alias="BITGET_CANDLE_LIMIT")
    bitget_contract_cache_ttl_sec: int = Field(default=180, alias="BITGET_CONTRACT_CACHE_TTL_SEC")
    bitget_rate_limit_min_interval_ms: int = Field(default=120, alias="BITGET_RATE_LIMIT_MIN_INTERVAL_MS")
    bitget_rate_limit_429_cooldown_sec: float = Field(default=5.0, alias="BITGET_RATE_LIMIT_429_COOLDOWN_SEC")
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
    allow_auto_watchlist_refresh: bool = Field(default=True, alias="ALLOW_AUTO_WATCHLIST_REFRESH")
    min_usdt_volume_24h: float = Field(default=10_000_000, alias="MIN_USDT_VOLUME_24H")
    min_change_pct_24h_abs: float = Field(default=1.5, alias="MIN_CHANGE_PCT_24H_ABS")
    max_symbols: int = Field(default=28, alias="MAX_SYMBOLS")
    strategy_debug_symbols: str = Field(default="", alias="STRATEGY_DEBUG_SYMBOLS")
    enable_shorts: bool = Field(default=True, alias="ENABLE_SHORTS")
    strategy_isolation_enabled: bool = Field(default=False, alias="STRATEGY_ISOLATION_ENABLED")
    enabled_strategies: str = Field(default="", alias="ENABLED_STRATEGIES")
    disabled_strategies: str = Field(default="", alias="DISABLED_STRATEGIES")

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
    break_even_fee_buffer_pct: float = Field(default=0.12, alias="BREAK_EVEN_FEE_BUFFER_PCT")
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
    execution_max_per_cycle: int = Field(default=1, alias="EXECUTION_MAX_PER_CYCLE")
    execution_plan_limit: int = Field(default=2, alias="EXECUTION_PLAN_LIMIT")
    execution_max_live_notional_per_trade_usdt: float = Field(default=35.0, alias="EXECUTION_MAX_LIVE_NOTIONAL_PER_TRADE_USDT")
    execution_min_live_notional_usdt: float = Field(default=10.0, alias="EXECUTION_MIN_LIVE_NOTIONAL_USDT")

    position_manager_enabled: bool = Field(default=True, alias="POSITION_MANAGER_ENABLED")

    dashboard_enabled: bool = Field(default=True, alias="DASHBOARD_ENABLED")
    dashboard_host: str = Field(default="127.0.0.1", alias="DASHBOARD_HOST")
    dashboard_port: int = Field(default=8501, alias="DASHBOARD_PORT")
    dashboard_debug: bool = Field(default=False, alias="DASHBOARD_DEBUG")
    position_sync_on_start: bool = Field(default=True, alias="POSITION_SYNC_ON_START")
    position_loop_enabled: bool = Field(default=True, alias="POSITION_LOOP_ENABLED")
    position_check_interval_sec: int = Field(default=30, alias="POSITION_CHECK_INTERVAL_SEC")
    move_stop_to_be_after_tp1: bool = Field(default=True, alias="MOVE_STOP_TO_BE_AFTER_TP1")
    tp1_close_pct: float = Field(default=40.0, alias="TP1_CLOSE_PCT")
    tp2_close_pct: float = Field(default=30.0, alias="TP2_CLOSE_PCT")
    tp3_close_pct: float = Field(default=30.0, alias="TP3_CLOSE_PCT")
    tp3_close_all_remainder: bool = Field(default=True, alias="TP3_CLOSE_ALL_REMAINDER")

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
        return [s.strip().upper() for s in self.watchlist.split(",") if s.strip()]

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
        return {s.strip().upper() for s in self.execution_confirm_symbols.split(",") if s.strip()}


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
