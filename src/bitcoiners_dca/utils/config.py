"""
Config loader — parses config.yaml + resolves secrets from env vars.

The config file is checked into the user's own system (with secrets blank).
Secrets resolved at runtime from environment.
"""
from __future__ import annotations
import os
from decimal import Decimal
from pathlib import Path
from typing import Optional

import yaml
from typing import Literal

from pydantic import BaseModel, Field, model_validator


class LicenseConfig(BaseModel):
    """Licensing — gates premium features. See `docs/TIERS.md`.

    `tier: free` always works without a key.
    `tier: pro` / `tier: business` require a key signed by the publisher.
    Invalid or expired keys silently downgrade to free with a warning log.
    """
    tier: str = "free"                 # free | pro | business
    key: Optional[str] = None          # base64 signed token (see scripts/generate_license.py)


class TelegramConfig(BaseModel):
    enabled: bool = False
    bot_token_env: str = "TG_BOT_TOKEN"
    chat_id: Optional[str] = None


class EmailConfig(BaseModel):
    enabled: bool = False
    smtp_host: Optional[str] = None
    smtp_port: int = 587
    from_addr: Optional[str] = None
    to_addr: Optional[str] = None
    password_env: str = "SMTP_PASSWORD"


class NotificationsConfig(BaseModel):
    telegram: TelegramConfig = Field(default_factory=TelegramConfig)
    email: EmailConfig = Field(default_factory=EmailConfig)


class ExchangeConfig(BaseModel):
    enabled: bool = False
    api_key_env: Optional[str] = None           # HMAC-style exchanges (OKX, Binance)
    api_secret_env: Optional[str] = None
    passphrase_env: Optional[str] = None        # OKX needs this
    token_env: Optional[str] = None             # Bearer-token exchanges (BitOasis)
    use_uae_endpoint: bool = True               # Binance UAE vs global

    def get_api_key(self) -> Optional[str]:
        return os.environ.get(self.api_key_env) if self.api_key_env else None

    def get_api_secret(self) -> Optional[str]:
        return os.environ.get(self.api_secret_env) if self.api_secret_env else None

    def get_passphrase(self) -> Optional[str]:
        return os.environ.get(self.passphrase_env) if self.passphrase_env else None

    def get_token(self) -> Optional[str]:
        return os.environ.get(self.token_env) if self.token_env else None


class ExchangesConfig(BaseModel):
    bitoasis: ExchangeConfig = Field(default_factory=lambda: ExchangeConfig(
        token_env="BITOASIS_API_TOKEN",
    ))
    okx: ExchangeConfig = Field(default_factory=lambda: ExchangeConfig(
        api_key_env="OKX_API_KEY", api_secret_env="OKX_API_SECRET",
        passphrase_env="OKX_API_PASSPHRASE",
    ))
    binance: ExchangeConfig = Field(default_factory=lambda: ExchangeConfig(
        api_key_env="BINANCE_API_KEY", api_secret_env="BINANCE_API_SECRET",
    ))


class StrategyYamlConfig(BaseModel):
    type: str = "standard_dca"

    # The bot reads `amount_aed` as the per-cycle base amount. Customers
    # usually think in terms of a monthly or weekly *spend rate*, so the
    # dashboard accepts `budget_amount` + `budget_period` and derives
    # `amount_aed` from them given the chosen `frequency`. The mapping is
    # one-way: budget_* is the user's stated intent, amount_aed is what
    # the cron + overlay stack actually uses. See `derive_per_cycle()`
    # in `core/strategy.py`.
    amount_aed: Decimal = Decimal("500")
    budget_amount: Optional[Decimal] = None
    budget_period: Literal["cycle", "daily", "weekly", "monthly", "yearly"] = "cycle"

    frequency: Literal["hourly", "daily", "weekly", "monthly"] = "weekly"
    every_n_hours: int = 1  # 1..24; scheduler clamps and snaps to a divisor of 24
    day_of_week: Literal[
        "monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"
    ] = "monday"
    time: str = "09:00"
    timezone: str = "Asia/Dubai"

    @model_validator(mode="after")
    def _backfill_budget(self):
        # Migration for tenants whose config.yaml predates budget_amount:
        # take the existing amount_aed as the budget (per-cycle), so the
        # dashboard form shows the value the user actually entered.
        if self.budget_amount is None:
            self.budget_amount = self.amount_aed
        return self


class DipOverlayConfig(BaseModel):
    enabled: bool = False
    threshold_pct: Decimal = Decimal("-10")
    lookback_days: int = 7
    multiplier: Decimal = Decimal("2.0")


class VolatilityWeightedOverlayConfig(BaseModel):
    """Buy less when realized vol is high, more when low. See docs/STRATEGIES.md."""
    enabled: bool = False
    target_vol_pct: Decimal = Decimal("50")
    slope: Decimal = Decimal("0.02")
    min_factor: Decimal = Decimal("0.25")
    max_factor: Decimal = Decimal("2.0")


class TimeOfDayOverlayConfig(BaseModel):
    """Skip cycles outside the cheapest hours, or scale by hour-of-day spreads."""
    enabled: bool = False
    mode: Literal["skip_if_not_best", "scale_by_spread"] = "skip_if_not_best"
    preferred_hours: list[int] = Field(default_factory=lambda: list(range(9, 19)))
    spread_scale_min: Decimal = Decimal("0.5")
    spread_scale_max: Decimal = Decimal("1.5")


class DrawdownTierConfig(BaseModel):
    threshold_pct: Decimal
    multiplier: Decimal


class DrawdownOverlayConfig(BaseModel):
    """Extra buys at significant drawdowns from ATH."""
    enabled: bool = False
    tiers: list[DrawdownTierConfig] = Field(default_factory=lambda: [
        DrawdownTierConfig(threshold_pct=Decimal("-20"), multiplier=Decimal("1.5")),
        DrawdownTierConfig(threshold_pct=Decimal("-40"), multiplier=Decimal("2.5")),
        DrawdownTierConfig(threshold_pct=Decimal("-60"), multiplier=Decimal("4.0")),
    ])


class OverlaysConfig(BaseModel):
    buy_the_dip: DipOverlayConfig = Field(default_factory=DipOverlayConfig)
    volatility_weighted: VolatilityWeightedOverlayConfig = Field(default_factory=VolatilityWeightedOverlayConfig)
    time_of_day: TimeOfDayOverlayConfig = Field(default_factory=TimeOfDayOverlayConfig)
    drawdown_aware: DrawdownOverlayConfig = Field(default_factory=DrawdownOverlayConfig)


class MakerConfig(BaseModel):
    """How to price the limit order when running in maker mode.

    `limit_at`:
      - "bid": place at the best bid (most-likely fill, but no edge over taker
        once the maker rebate is accounted for)
      - "midpoint": (best_bid + best_ask) / 2
      - "ask_minus_bps": ask × (1 - spread_bps_below_market / 10000) — sits
        inside the spread, lower fill probability, lowest cost
    """
    limit_at: Literal["bid", "midpoint", "ask_minus_bps"] = "bid"
    spread_bps_below_market: int = 5
    timeout_seconds: int = 600


class ExecutionConfig(BaseModel):
    """How each buy gets placed.

    mode:
      - "taker"          : market buy at the ask. Instant fill, full taker fee.
      - "maker_only"     : limit buy; skip the cycle entirely if unfilled at timeout.
      - "maker_fallback" : limit buy; if unfilled at timeout, cancel + market buy.

    See `docs/EXECUTION_MODES.md` for trade-offs and per-exchange tuning.
    """
    mode: Literal["taker", "maker_only", "maker_fallback"] = "taker"
    maker: MakerConfig = Field(default_factory=MakerConfig)


class RoutingConfig(BaseModel):
    """Routing knobs — see `docs/ROUTING.md`."""
    mode: str = "best_price"
    preferred_exchange: Optional[str] = None
    preferred_bonus_pct: Decimal = Decimal("0.5")
    exclude_if_spread_pct_above: Decimal = Decimal("2.0")

    # Multi-hop routing — synthetic paths like AED → USDT → BTC on the same
    # exchange. Beats direct BTC/AED on OKX by ~0.09% at current pricing.
    enable_two_hop: bool = False
    intermediates: list[str] = Field(default_factory=lambda: ["USDT"])

    # Cross-exchange alerts — Telegram-notify when bridging via USDT
    # withdrawal beats every other route. Never auto-executed.
    enable_cross_exchange_alerts: bool = False
    cross_exchange_min_size_aed: Decimal = Decimal("25000")
    cross_exchange_withdrawal_costs: dict[str, Decimal] = Field(
        default_factory=lambda: {"USDT": Decimal("1.5")}  # OKX TRC20
    )


class PerExchangeAutoWithdraw(BaseModel):
    """Per-exchange auto-withdraw policy.

    `destination` accepts:
      - On-chain BTC address (bc1.../1.../3.../bc1p...)
      - Lightning BOLT11 invoice (`lnbc...`) — only valid for `network=lightning`
      - LNURL / Lightning Address (`name@host`) — for some Lightning-capable
        exchanges; depends on adapter support
    Network defaults to on-chain and auto-flips to lightning if the address
    is an LN invoice.
    """
    enabled: bool = False
    destination: Optional[str] = None
    network: str = "bitcoin"     # "bitcoin" | "lightning"
    threshold_btc: Decimal = Decimal("0.001")


class AutoWithdrawConfig(BaseModel):
    """Sweep BTC out of exchanges into self-custody.

    `exchanges` is the source of truth — one policy per exchange the user
    holds. The legacy top-level `destination_address` + `threshold_btc` are
    kept for backwards-compat reads of older config.yaml files; on save,
    they're migrated into a default 'okx' entry by the dashboard.
    """
    enabled: bool = False
    # Legacy single-destination fields. Kept for read-only back-compat;
    # the per-exchange `exchanges` map is the new source of truth.
    destination_address: Optional[str] = None
    threshold_btc: Decimal = Decimal("0.01")

    exchanges: dict[str, PerExchangeAutoWithdraw] = Field(default_factory=dict)


class ArbitrageConfig(BaseModel):
    enabled: bool = True
    min_spread_pct: Decimal = Decimal("1.5")
    slippage_buffer_pct: Decimal = Decimal("0.3")
    poll_interval_seconds: int = 300


class FundingInstrumentConfig(BaseModel):
    exchange: str
    symbol: str


class FundingMonitorConfig(BaseModel):
    """Watch BTC perpetual funding rates; alert when carry becomes attractive.

    See `docs/FUNDING_MONITOR.md` for what to do with the alerts.
    """
    enabled: bool = False
    poll_interval_seconds: int = 3600              # 1 hour
    alert_threshold_pct: Decimal = Decimal("15.0")
    alert_negative_threshold_pct: Decimal = Decimal("-10.0")
    alert_cooldown_hours: int = 24
    instruments: list[FundingInstrumentConfig] = Field(
        default_factory=lambda: [
            FundingInstrumentConfig(exchange="okx", symbol="BTC-USDT-SWAP")
        ]
    )


class RiskConfig(BaseModel):
    """Spend caps + circuit breakers — see `core/risk.py` for behavior."""
    # Maximum fraction of a single exchange's available quote balance the
    # bot can spend in one cycle. With this set to 0.25 and an AED balance
    # of 10,000, no single cycle can take more than 2,500 AED — protects
    # against config typos (e.g. amount_aed=15000 instead of 150) sweeping
    # the whole balance on the first Buy Now click.
    max_pct_of_balance: Decimal = Decimal("0.25")
    max_daily_aed: Optional[Decimal] = None         # None = no daily cap
    max_single_buy_aed: Optional[Decimal] = None    # None = no per-buy cap
    max_consecutive_failures: int = 5               # auto-pause threshold


class ReportsConfig(BaseModel):
    uae_tax_csv_path: str = "./reports"


class PersistenceConfig(BaseModel):
    db_path: str = "./data/dca.db"


class AppConfig(BaseModel):
    license: LicenseConfig = Field(default_factory=LicenseConfig)
    strategy: StrategyYamlConfig = Field(default_factory=StrategyYamlConfig)
    overlays: OverlaysConfig = Field(default_factory=OverlaysConfig)
    execution: ExecutionConfig = Field(default_factory=ExecutionConfig)
    routing: RoutingConfig = Field(default_factory=RoutingConfig)
    exchanges: ExchangesConfig = Field(default_factory=ExchangesConfig)
    auto_withdraw: AutoWithdrawConfig = Field(default_factory=AutoWithdrawConfig)
    arbitrage: ArbitrageConfig = Field(default_factory=ArbitrageConfig)
    funding_monitor: FundingMonitorConfig = Field(default_factory=FundingMonitorConfig)
    risk: RiskConfig = Field(default_factory=RiskConfig)
    notifications: NotificationsConfig = Field(default_factory=NotificationsConfig)
    reports: ReportsConfig = Field(default_factory=ReportsConfig)
    persistence: PersistenceConfig = Field(default_factory=PersistenceConfig)

    dry_run: bool = False    # global dry-run flag overrides everything


def load_config(path: str | Path = "./config.yaml") -> AppConfig:
    p = Path(path)
    if not p.exists():
        # Return defaults — useful for first-run / testing
        return AppConfig()
    with p.open("r") as f:
        raw = yaml.safe_load(f) or {}
    return AppConfig.model_validate(raw)
