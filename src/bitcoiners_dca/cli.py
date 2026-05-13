"""
bitcoiners-dca CLI — entry point.

Commands:
  bitcoiners-dca run            # Start the bot (schedules + arbitrage monitor)
  bitcoiners-dca buy-once       # Run a single DCA cycle right now
  bitcoiners-dca arb-check      # Check for arbitrage opportunities right now
  bitcoiners-dca status         # Show current balances + stats
  bitcoiners-dca prices         # Show ticker across all configured exchanges
  bitcoiners-dca init-config    # Write a starter config.yaml
"""
from __future__ import annotations
import asyncio
import shutil
from decimal import Decimal
from pathlib import Path
from typing import Optional

import typer
from rich.console import Console
from rich.table import Table

from bitcoiners_dca.core.arbitrage import ArbitrageMonitor
from bitcoiners_dca.core.license import Feature, LicenseManager
from bitcoiners_dca.core.notifications import Notifier
from bitcoiners_dca.core.router import SmartRouter
from bitcoiners_dca.core.strategy import DCAStrategy, StrategyConfig
from bitcoiners_dca.exchanges.base import Exchange
from bitcoiners_dca.persistence.db import Database
from bitcoiners_dca.utils.config import AppConfig, load_config


def _license_manager(cfg: AppConfig) -> LicenseManager:
    return LicenseManager.from_config(cfg.license.tier, cfg.license.key)


def _load_runtime_config(config_path: str) -> AppConfig:
    """Load config + apply license filter. Use this everywhere except in the
    `license` CLI itself (which needs to show what the user asked for).
    """
    raw = load_config(config_path)
    mgr = _license_manager(raw)
    return _apply_license_filter(raw, mgr)


def _apply_license_filter(cfg: AppConfig, mgr: LicenseManager) -> AppConfig:
    """Downgrade config to what the licensed tier actually allows.

    This is the SINGLE PLACE in the codebase where tier-gating happens.
    All downstream code reads from the post-filter config and doesn't need
    to know about licensing. Premium features the user enabled in config
    but didn't pay for are silently disabled — a warning is logged.

    Returns a modified copy of `cfg`; the original is untouched so the
    `license` CLI can still show what the user asked for.
    """
    import logging
    log = logging.getLogger(__name__)
    cfg = cfg.model_copy(deep=True)

    # Multi-exchange gate
    if not mgr.is_feature_enabled(Feature.MULTI_EXCHANGE):
        enabled_names = [
            n for n, ex in (("okx", cfg.exchanges.okx),
                            ("binance", cfg.exchanges.binance),
                            ("bitoasis", cfg.exchanges.bitoasis))
            if ex.enabled
        ]
        if len(enabled_names) > 1:
            log.warning(
                "License tier %s allows 1 exchange; keeping %s, disabling %s",
                mgr.tier.value, enabled_names[0], enabled_names[1:],
            )
            # Keep the first enabled, disable the rest
            for n in enabled_names[1:]:
                getattr(cfg.exchanges, n).enabled = False

    # Multi-hop routing gate
    if not mgr.is_feature_enabled(Feature.MULTI_HOP_ROUTING):
        if cfg.routing.enable_two_hop:
            log.warning("License tier %s does not include multi-hop routing — disabling", mgr.tier.value)
            cfg.routing.enable_two_hop = False

    # Cross-exchange alerts gate
    if not mgr.is_feature_enabled(Feature.CROSS_EXCHANGE_ALERTS):
        if cfg.routing.enable_cross_exchange_alerts:
            log.warning("License tier %s does not include cross-exchange alerts — disabling", mgr.tier.value)
            cfg.routing.enable_cross_exchange_alerts = False

    # Maker-mode execution gate
    if not mgr.is_feature_enabled(Feature.MAKER_MODE):
        if cfg.execution.mode != "taker":
            log.warning(
                "License tier %s does not include maker-mode execution — "
                "falling back to taker", mgr.tier.value,
            )
            cfg.execution.mode = "taker"

    # Dip overlay gate
    if not mgr.is_feature_enabled(Feature.DIP_OVERLAY):
        if cfg.overlays.buy_the_dip.enabled:
            log.warning("License tier %s does not include buy-the-dip overlay — disabling", mgr.tier.value)
            cfg.overlays.buy_the_dip.enabled = False

    # Volatility-weighted overlay gate
    if not mgr.is_feature_enabled(Feature.VOLATILITY_WEIGHTED):
        if cfg.overlays.volatility_weighted.enabled:
            log.warning("License tier %s does not include volatility-weighted DCA — disabling", mgr.tier.value)
            cfg.overlays.volatility_weighted.enabled = False

    # Time-of-day overlay gate
    if not mgr.is_feature_enabled(Feature.TIME_OF_DAY):
        if cfg.overlays.time_of_day.enabled:
            log.warning("License tier %s does not include time-of-day DCA — disabling", mgr.tier.value)
            cfg.overlays.time_of_day.enabled = False

    # Drawdown overlay gate
    if not mgr.is_feature_enabled(Feature.DRAWDOWN_SIZING):
        if cfg.overlays.drawdown_aware.enabled:
            log.warning("License tier %s does not include drawdown-aware sizing — disabling", mgr.tier.value)
            cfg.overlays.drawdown_aware.enabled = False

    # Funding monitor gate
    if not mgr.is_feature_enabled(Feature.FUNDING_MONITOR):
        if cfg.funding_monitor.enabled:
            log.warning("License tier %s does not include funding-rate monitor — disabling", mgr.tier.value)
            cfg.funding_monitor.enabled = False

    return cfg


app = typer.Typer(help="bitcoiners-dca: self-hosted multi-exchange DCA bot")
console = Console()


def _build_exchanges(cfg: AppConfig) -> list[Exchange]:
    """Instantiate configured + credentialed exchange adapters.

    Credentials resolved in priority order: SecretStore (dashboard-managed) →
    env vars (config.exchanges.<name>.api_key_env etc). This means a customer
    who pastes keys in the dashboard sees them used, while self-hosters who
    set env vars don't have to migrate.
    """
    from bitcoiners_dca.persistence.secrets import (
        SecretStore, SecretStoreError, credentials_for,
    )
    secrets = None
    try:
        secrets = SecretStore(cfg.persistence.db_path)
    except SecretStoreError:
        pass

    def _creds(exchange: str, env_map: dict[str, str]) -> dict[str, str]:
        out: dict[str, str] = {}
        if secrets is not None:
            out.update(credentials_for(secrets, exchange))
        import os
        for field, env_name in env_map.items():
            if field not in out:
                val = os.environ.get(env_name)
                if val:
                    out[field] = val
        return out

    out: list[Exchange] = []
    if cfg.exchanges.okx.enabled:
        c = _creds("okx", {
            "api_key": cfg.exchanges.okx.api_key_env or "OKX_API_KEY",
            "api_secret": cfg.exchanges.okx.api_secret_env or "OKX_API_SECRET",
            "passphrase": cfg.exchanges.okx.passphrase_env or "OKX_API_PASSPHRASE",
        })
        if c.get("api_key"):
            from bitcoiners_dca.exchanges.okx import OKXExchange
            out.append(OKXExchange(
                api_key=c["api_key"], api_secret=c.get("api_secret", ""),
                passphrase=c.get("passphrase", ""), dry_run=cfg.dry_run,
            ))
    if cfg.exchanges.binance.enabled:
        c = _creds("binance", {
            "api_key": cfg.exchanges.binance.api_key_env or "BINANCE_API_KEY",
            "api_secret": cfg.exchanges.binance.api_secret_env or "BINANCE_API_SECRET",
        })
        if c.get("api_key"):
            from bitcoiners_dca.exchanges.binance import BinanceExchange
            out.append(BinanceExchange(
                api_key=c["api_key"], api_secret=c.get("api_secret", ""),
                use_uae_endpoint=cfg.exchanges.binance.use_uae_endpoint,
                dry_run=cfg.dry_run,
            ))
    if cfg.exchanges.bitoasis.enabled:
        c = _creds("bitoasis", {
            "token": cfg.exchanges.bitoasis.token_env or "BITOASIS_API_TOKEN",
        })
        if c.get("token"):
            from bitcoiners_dca.exchanges.bitoasis import BitOasisExchange
            out.append(BitOasisExchange(
                api_token=c["token"], dry_run=cfg.dry_run,
            ))
    return out


def _build_router(cfg: AppConfig) -> SmartRouter:
    return SmartRouter(
        exclude_if_spread_pct_above=cfg.routing.exclude_if_spread_pct_above,
        preferred_exchange=cfg.routing.preferred_exchange,
        preferred_bonus_pct=cfg.routing.preferred_bonus_pct,
        enable_two_hop=cfg.routing.enable_two_hop,
        intermediates=cfg.routing.intermediates,
        enable_cross_exchange_alerts=cfg.routing.enable_cross_exchange_alerts,
        cross_exchange_min_size_aed=cfg.routing.cross_exchange_min_size_aed,
        cross_exchange_withdrawal_costs=cfg.routing.cross_exchange_withdrawal_costs,
    )


def _build_overlays(cfg: AppConfig) -> list:
    """Construct the active overlay stack from config + license filter has
    already disabled any not-allowed-on-this-tier overlay configs."""
    from bitcoiners_dca.strategies import (
        BuyTheDipOverlay, DrawdownOverlay, TimeOfDayOverlay,
        VolatilityWeightedOverlay,
    )
    from bitcoiners_dca.strategies.drawdown import DrawdownTier

    out: list = []
    if cfg.overlays.buy_the_dip.enabled:
        out.append(BuyTheDipOverlay(
            threshold_pct=cfg.overlays.buy_the_dip.threshold_pct,
            multiplier=cfg.overlays.buy_the_dip.multiplier,
            lookback_days=cfg.overlays.buy_the_dip.lookback_days,
        ))
    if cfg.overlays.volatility_weighted.enabled:
        out.append(VolatilityWeightedOverlay(
            target_vol_pct=cfg.overlays.volatility_weighted.target_vol_pct,
            slope=cfg.overlays.volatility_weighted.slope,
            min_factor=cfg.overlays.volatility_weighted.min_factor,
            max_factor=cfg.overlays.volatility_weighted.max_factor,
        ))
    if cfg.overlays.time_of_day.enabled:
        out.append(TimeOfDayOverlay(
            mode=cfg.overlays.time_of_day.mode,
            preferred_hours=cfg.overlays.time_of_day.preferred_hours,
            spread_scale_min=cfg.overlays.time_of_day.spread_scale_min,
            spread_scale_max=cfg.overlays.time_of_day.spread_scale_max,
            # Honour the strategy's scheduling timezone so "preferred hours"
            # are interpreted in the user's local time, not UTC.
            timezone=cfg.strategy.timezone or "Asia/Dubai",
        ))
    if cfg.overlays.drawdown_aware.enabled:
        out.append(DrawdownOverlay(
            tiers=[
                DrawdownTier(t.threshold_pct, t.multiplier)
                for t in cfg.overlays.drawdown_aware.tiers
            ],
        ))
    return out


def _build_strategy(cfg: AppConfig, router: SmartRouter) -> DCAStrategy:
    return DCAStrategy(
        config=StrategyConfig(
            base_amount_aed=cfg.strategy.amount_aed,
            frequency=cfg.strategy.frequency,
            dip_overlay_enabled=cfg.overlays.buy_the_dip.enabled,
            dip_threshold_pct=cfg.overlays.buy_the_dip.threshold_pct,
            dip_lookback_days=cfg.overlays.buy_the_dip.lookback_days,
            dip_multiplier=cfg.overlays.buy_the_dip.multiplier,
            auto_withdraw_enabled=cfg.auto_withdraw.enabled,
            auto_withdraw_address=cfg.auto_withdraw.destination_address,
            auto_withdraw_threshold_btc=cfg.auto_withdraw.threshold_btc,
            auto_withdraw_exchanges={
                name: {
                    "enabled": p.enabled,
                    "destination": p.destination,
                    "network": p.network,
                    "threshold_btc": p.threshold_btc,
                }
                for name, p in (cfg.auto_withdraw.exchanges or {}).items()
            },
            execution_mode=cfg.execution.mode,
            maker_limit_at=cfg.execution.maker.limit_at,
            maker_spread_bps_below_market=cfg.execution.maker.spread_bps_below_market,
            maker_timeout_seconds=cfg.execution.maker.timeout_seconds,
            max_pct_of_balance=cfg.risk.max_pct_of_balance,
        ),
        router=router,
        overlays=_build_overlays(cfg),
    )


@app.command()
def init_config(path: str = "./config.yaml"):
    """Write a starter config.yaml to the current directory."""
    template = Path(__file__).parent.parent.parent / "config.example.yaml"
    dest = Path(path)
    if dest.exists():
        console.print(f"[red]{dest} already exists. Move it aside first.[/red]")
        raise typer.Exit(code=1)
    if template.exists():
        shutil.copy(template, dest)
    else:
        # Inline template fallback
        dest.write_text(_INLINE_TEMPLATE)
    console.print(f"[green]Wrote {dest}. Edit it and set your environment variables.[/green]")


@app.command()
def prices(
    config_path: str = typer.Option("./config.yaml", "--config", "-c"),
    pair: str = "BTC/AED",
):
    """Show current ticker prices across all configured exchanges."""
    asyncio.run(_prices(config_path, pair))


async def _prices(config_path: str, pair: str):
    cfg = _load_runtime_config(config_path)
    exchanges = _build_exchanges(cfg)
    if not exchanges:
        console.print("[red]No exchanges configured. Run `bitcoiners-dca init-config` first.[/red]")
        return
    table = Table(title=f"Current prices · {pair}")
    table.add_column("Exchange"); table.add_column("Bid"); table.add_column("Ask")
    table.add_column("Last"); table.add_column("Spread %")
    for ex in exchanges:
        try:
            t = await ex.get_ticker(pair)
            table.add_row(
                ex.name, f"{t.bid:.2f}", f"{t.ask:.2f}",
                f"{t.last:.2f}", f"{t.spread_pct:.3f}",
            )
        except Exception as e:
            table.add_row(ex.name, f"[red]error: {str(e)[:60]}[/red]", "", "", "")
    console.print(table)
    for ex in exchanges:
        await ex.close()


@app.command()
def buy_once(
    config_path: str = typer.Option("./config.yaml", "--config", "-c"),
    dry: bool = typer.Option(False, "--dry", help="Force dry-run regardless of config"),
):
    """Execute one DCA cycle immediately, then exit."""
    asyncio.run(_buy_once(config_path, dry))


async def _buy_once(config_path: str, dry: bool):
    from bitcoiners_dca.core.market_data import MarketDataProvider
    from bitcoiners_dca.core.risk import RiskManager
    cfg = _load_runtime_config(config_path)
    if dry:
        cfg.dry_run = True
    exchanges = _build_exchanges(cfg)
    router = _build_router(cfg)
    strategy = _build_strategy(cfg, router)
    db = Database(cfg.persistence.db_path)
    notifier = Notifier(cfg.notifications)
    market_data = MarketDataProvider(db=db)
    snap = market_data.snapshot()

    # Apply the same risk cap as the scheduled path. Without this, Buy-now
    # tries to spend the raw strategy.amount_aed (e.g. 15000) in one shot
    # — which fails on any account that doesn't actually hold that much
    # AED, even though scheduled cycles work fine via per-cycle clamping.
    rm = RiskManager(
        db,
        max_daily_aed=cfg.risk.max_daily_aed,
        max_single_buy_aed=cfg.risk.max_single_buy_aed,
        max_consecutive_failures=cfg.risk.max_consecutive_failures,
    )
    decision = rm.evaluate(Decimal(str(cfg.strategy.amount_aed)))
    if not decision.allow:
        console.print(f"[red]Risk caps blocked the cycle: {'; '.join(decision.reasons)}[/red]")
        for ex in exchanges:
            await ex.close()
        db.close()
        return

    result = await strategy.execute(
        exchanges,
        historical_price_7d_ago=snap.price_7d_ago_aed,
        risk_cap_aed=decision.amount_aed,
        market_context=snap.to_context_dict(),
    )
    if decision.reasons:
        result.notes.extend(decision.reasons)
    db.record_cycle(result)
    await notifier.notify_cycle(result)

    if result.order:
        console.print(f"[green]✓ Bought {result.order.amount_base} BTC on {result.order.exchange} for AED {result.intended_amount_aed}[/green]")
        if result.routing_decision:
            console.print(f"  {result.routing_decision.reason}")
        if result.overlay_applied:
            console.print(f"  Overlay: {result.overlay_applied}")
    else:
        console.print("[red]No order placed.[/red]")
    for e in result.errors:
        console.print(f"[yellow]! {e}[/yellow]")

    for ex in exchanges:
        await ex.close()
    db.close()


@app.command()
def arb_check(
    config_path: str = typer.Option("./config.yaml", "--config", "-c"),
):
    """Check for arbitrage opportunities right now."""
    asyncio.run(_arb_check(config_path))


async def _arb_check(config_path: str):
    cfg = _load_runtime_config(config_path)
    exchanges = _build_exchanges(cfg)
    if len(exchanges) < 2:
        console.print("[red]Need at least 2 exchanges configured for arbitrage detection.[/red]")
        return
    monitor = ArbitrageMonitor(
        min_spread_pct=cfg.arbitrage.min_spread_pct,
        slippage_buffer_pct=cfg.arbitrage.slippage_buffer_pct,
    )
    db = Database(cfg.persistence.db_path)
    notifier = Notifier(cfg.notifications)

    opps = await monitor.detect(exchanges)
    if not opps:
        console.print("[dim]No arbitrage opportunities right now (gross spread below threshold).[/dim]")
    else:
        table = Table(title="Arbitrage opportunities (sorted by net profit)")
        for col in ("Buy on", "Sell on", "Gross %", "Net %"): table.add_column(col)
        for opp in opps:
            table.add_row(
                opp.cheap_exchange, opp.expensive_exchange,
                f"{opp.spread_pct:.2f}", f"{opp.net_profit_pct_after_fees:.2f}",
            )
            db.record_arbitrage(opp, alerted=True)
            await notifier.notify_arbitrage(opp)
        console.print(table)

    for ex in exchanges:
        await ex.close()
    db.close()


@app.command()
def status(
    config_path: str = typer.Option("./config.yaml", "--config", "-c"),
):
    """Show balances + lifetime stats."""
    asyncio.run(_status(config_path))


async def _status(config_path: str):
    cfg = _load_runtime_config(config_path)
    db = Database(cfg.persistence.db_path)
    exchanges = _build_exchanges(cfg)

    console.print("[bold]Lifetime totals[/bold]")
    console.print(f"  Total BTC bought: {db.total_btc_bought()}")
    console.print(f"  Total AED spent:  {db.total_aed_spent()}")

    for ex in exchanges:
        console.print(f"\n[bold]{ex.name}[/bold]")
        try:
            for b in await ex.get_balances():
                console.print(f"  {b.asset}: free={b.free} total={b.total}")
        except Exception as e:
            console.print(f"  [red]error: {e}[/red]")

    for ex in exchanges:
        await ex.close()
    db.close()


@app.command()
def run(
    config_path: str = typer.Option("./config.yaml", "--config", "-c"),
):
    """Run the full bot in foreground — DCA scheduler + arbitrage poller + health checks.

    Blocks until SIGTERM/SIGINT. Recommended deployment: PM2, systemd, or Docker.
    """
    asyncio.run(_run_daemon(config_path))


async def _run_daemon(config_path: str):
    import logging
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    )

    from bitcoiners_dca.core.arbitrage import ArbitrageMonitor
    from bitcoiners_dca.core.scheduler import DCAScheduler

    cfg = _load_runtime_config(config_path)
    exchanges = _build_exchanges(cfg)
    if not exchanges:
        # New tenant before they paste any API keys — the dashboard is the
        # next thing they'll touch. Idle-wait so the container doesn't
        # restart-loop; pick up the first exchange the moment they configure
        # one through the dashboard.
        logging.info(
            "no exchanges configured — daemon idle. "
            "Add an exchange via the dashboard, then I'll start automatically."
        )
        while not exchanges:
            await asyncio.sleep(15)
            try:
                cfg = _load_runtime_config(config_path)
                exchanges = _build_exchanges(cfg)
            except Exception as e:
                logging.warning("config reload failed while idle: %s", e)
        logging.info("exchanges configured — exiting idle loop, starting daemon")

    router = _build_router(cfg)
    strategy = _build_strategy(cfg, router)
    db = Database(cfg.persistence.db_path)
    notifier = Notifier(cfg.notifications)
    monitor = ArbitrageMonitor(
        min_spread_pct=cfg.arbitrage.min_spread_pct,
        slippage_buffer_pct=cfg.arbitrage.slippage_buffer_pct,
    )

    def rebuild():
        """Hot-reload factory — re-reads config + secrets + rebuilds every
        component the scheduler depends on. Called at the top of every
        scheduled task so dashboard edits to config.yaml take effect on
        the next cycle without a daemon restart."""
        fresh_cfg = _load_runtime_config(config_path)
        fresh_exchanges = _build_exchanges(fresh_cfg)
        fresh_router = _build_router(fresh_cfg)
        fresh_strategy = _build_strategy(fresh_cfg, fresh_router)
        fresh_monitor = ArbitrageMonitor(
            min_spread_pct=fresh_cfg.arbitrage.min_spread_pct,
            slippage_buffer_pct=fresh_cfg.arbitrage.slippage_buffer_pct,
        )
        return {
            "config": fresh_cfg,
            "exchanges": fresh_exchanges,
            "router": fresh_router,
            "strategy": fresh_strategy,
            "monitor": fresh_monitor,
        }

    scheduler = DCAScheduler(
        config=cfg, exchanges=exchanges, strategy=strategy,
        router=router, monitor=monitor, db=db, notifier=notifier,
        rebuild_dependencies=rebuild,
    )
    await scheduler.run_forever()


@app.command(name="export-tax-csv")
def export_tax_csv(
    config_path: str = typer.Option("./config.yaml", "--config", "-c"),
    year: int = typer.Option(None, "--year", help="Tax year (e.g. 2026). Omit for lifetime."),
):
    """Export trade history as a CSV — useful for record-keeping."""
    from bitcoiners_dca.persistence.reports import export_uae_tax_csv

    cfg = _load_runtime_config(config_path)
    db = Database(cfg.persistence.db_path)
    out = export_uae_tax_csv(db, cfg.reports.uae_tax_csv_path, year=year)
    db.close()
    console.print(f"[green]✓ Wrote {out}[/green]")


@app.command()
def dashboard(
    config_path: str = typer.Option("./config.yaml", "--config", "-c"),
    host: str = "127.0.0.1",
    port: int = 8000,
):
    """Run the web dashboard. Open http://localhost:8000 after starting."""
    import os
    import uvicorn
    console.print(f"[green]Starting dashboard at http://{host}:{port}/[/green]")
    # uvicorn imports `bitcoiners_dca.web.dashboard:app` (module-level
    # factory call) — we can't pass kwargs through the import string, so
    # we ferry the config path via env var. dashboard.create_app() reads
    # DCA_DASHBOARD_CONFIG when no explicit arg is given.
    os.environ["DCA_DASHBOARD_CONFIG"] = config_path
    uvicorn.run(
        "bitcoiners_dca.web.dashboard:app",
        host=host, port=port, log_level="info",
    )


@app.command()
def backup(
    config_path: str = typer.Option("./config.yaml", "--config", "-c"),
    output_dir: str = typer.Option(None, "--out", help="Override the destination directory"),
):
    """Snapshot the SQLite event log + recent tax CSVs into a tarball.

    Recommended cron: nightly. The output is a single timestamped file
    you can `rsync` to your NAS or encrypt and ship to S3. The bot's
    full state lives in the .db file — restoring just means moving it
    back into place.
    """
    import shutil
    import tarfile
    import sqlite3
    from datetime import datetime as _dt

    cfg = _load_runtime_config(config_path)
    db_path = Path(cfg.persistence.db_path)
    if not db_path.exists():
        console.print(f"[red]No DB at {db_path} — nothing to back up.[/red]")
        raise typer.Exit(code=1)

    out_dir = Path(output_dir) if output_dir else Path("./backups")
    out_dir.mkdir(parents=True, exist_ok=True)

    stamp = _dt.now().strftime("%Y%m%d-%H%M%S")
    db_snapshot_path = out_dir / f"dca-{stamp}.db"
    # Use SQLite's online-backup API so we don't corrupt mid-write.
    src = sqlite3.connect(str(db_path))
    dst = sqlite3.connect(str(db_snapshot_path))
    with dst:
        src.backup(dst)
    src.close(); dst.close()

    tar_path = out_dir / f"bitcoiners-dca-{stamp}.tar.gz"
    with tarfile.open(tar_path, "w:gz") as tar:
        tar.add(db_snapshot_path, arcname=f"dca-{stamp}.db")
        # Include reports/ if present
        reports = Path(cfg.reports.uae_tax_csv_path)
        if reports.exists():
            tar.add(reports, arcname=f"reports-{stamp}")
        # Include the config.yaml (sans secrets — only YAML)
        cfg_file = Path(config_path)
        if cfg_file.exists():
            tar.add(cfg_file, arcname=f"config-{stamp}.yaml")
    db_snapshot_path.unlink()  # tar already contains it

    size_mb = tar_path.stat().st_size / 1024 / 1024
    console.print(f"[green]✓ Backup written:[/green] {tar_path.resolve()}")
    console.print(f"  Size: {size_mb:.2f} MB")
    console.print("[dim]Tip: rsync this to your NAS, or encrypt with `age` and ship to S3.[/dim]")


@app.command()
def doctor(
    config_path: str = typer.Option("./config.yaml", "--config", "-c"),
):
    """System-check: deeper inspection than `validate`. Surfaces common
    misconfigurations + environmental issues in one report. Run this when
    something's not behaving and you want a holistic picture.
    """
    import os
    import shutil
    import sys

    cfg_path = Path(config_path)
    has_config = cfg_path.exists()

    console.print("[bold]bitcoiners-dca · doctor[/bold]\n")

    # Python version
    py = sys.version_info
    py_ok = py >= (3, 11)
    console.print(
        f"  Python: {py.major}.{py.minor}.{py.micro} "
        f"[{'green' if py_ok else 'red'}]"
        f"{'OK' if py_ok else 'NEED 3.11+'}[/]"
    )

    # Optional system tools
    for tool in ("docker", "git"):
        path = shutil.which(tool)
        console.print(
            f"  {tool}: "
            f"[{'green' if path else 'yellow'}]"
            f"{path or 'not on PATH (only needed for some deploys)'}[/]"
        )

    # Config file
    console.print(
        f"  config.yaml: "
        f"[{'green' if has_config else 'red'}]"
        f"{cfg_path.resolve() if has_config else 'MISSING — run init-config'}[/]"
    )
    if not has_config:
        console.print("\n[red]Stopping — no config to inspect.[/red]")
        raise typer.Exit(code=1)

    cfg = load_config(config_path)
    mgr = _license_manager(cfg)

    # License
    console.print(
        f"  License tier: [cyan]{mgr.tier.value}[/cyan]  "
        f"(features: {len(mgr.enabled_features)})"
    )

    # Exchanges configured
    enabled = [
        n for n, ex in (("okx", cfg.exchanges.okx),
                        ("binance", cfg.exchanges.binance),
                        ("bitoasis", cfg.exchanges.bitoasis))
        if ex.enabled
    ]
    console.print(
        f"  Exchanges enabled: "
        f"[{'green' if enabled else 'red'}]{enabled or 'NONE'}[/]"
    )

    # Env-var presence
    needs = {
        "okx": ["OKX_API_KEY", "OKX_API_SECRET", "OKX_API_PASSPHRASE"],
        "binance": ["BINANCE_API_KEY", "BINANCE_API_SECRET"],
        "bitoasis": ["BITOASIS_API_TOKEN"],
    }
    missing_env = []
    for ex_name in enabled:
        for var in needs[ex_name]:
            if not os.environ.get(var):
                missing_env.append((ex_name, var))
    if missing_env:
        console.print("  [red]Missing env vars:[/red]")
        for ex_name, var in missing_env:
            console.print(f"    - {ex_name} needs ${var}")
    else:
        console.print("  Env vars: [green]all present for enabled exchanges[/green]")

    # Dry-run posture
    dry_label = "[yellow]ON — simulated only[/yellow]" if cfg.dry_run else "[red]OFF — LIVE TRADING[/red]"
    console.print(f"  dry_run: {dry_label}")

    # DB writeability
    db_dir = Path(cfg.persistence.db_path).parent
    db_writable = db_dir.exists() and os.access(db_dir, os.W_OK) if db_dir != Path("") else True
    console.print(
        f"  DB dir: "
        f"[{'green' if db_writable else 'red'}]"
        f"{db_dir.resolve() if db_dir != Path('') else '(none)'}[/]"
    )

    # Suggested next steps
    console.print("\n[bold]Suggested next steps:[/bold]")
    if mgr.tier.value == "free" and len(enabled) > 1:
        console.print("  • Free tier limits you to 1 exchange — the license filter")
        console.print(f"    will disable all but the first. Get a Pro key to use {len(enabled)} live.")
    if cfg.dry_run:
        console.print("  • Run a few `bitcoiners-dca buy-once` cycles to dry-run end-to-end.")
        console.print("  • Then `bitcoiners-dca validate` for a config audit.")
        console.print("  • When confident, flip `dry_run: false` and start the daemon with `run`.")
    else:
        console.print("  • Live trading is ENABLED. Verify auto-withdraw address one more time.")
        console.print("  • Start the daemon: `bitcoiners-dca run` (or `docker compose up -d`)")


@app.command()
def license(
    config_path: str = typer.Option("./config.yaml", "--config", "-c"),
):
    """Show the current license tier + enabled feature set."""
    from bitcoiners_dca.core.license import LicenseManager

    cfg = load_config(config_path)
    mgr = LicenseManager.from_config(cfg.license.tier, cfg.license.key)
    info = mgr.describe()

    console.print(f"[bold]License tier:[/bold] [cyan]{info['tier']}[/cyan]")
    if "customer_id" in info:
        console.print(f"  Customer:    {info['customer_id']}")
        console.print(f"  Issued:      {info['issued_at']}")
        console.print(f"  Expires:     {info['expires_at']}")
        if info.get('notes'):
            console.print(f"  Notes:       {info['notes']}")
    console.print()
    console.print(f"[bold]Features enabled ({info['feature_count']}):[/bold]")
    if not info['features']:
        console.print("  [dim]Free tier — base DCA + tax CSV + on-chain auto-withdraw + risk circuit breakers.[/dim]")
        console.print("  [dim]Upgrade for multi-exchange, multi-hop routing, maker mode, Lightning, more strategies.[/dim]")
        console.print("  [dim]Visit https://bitcoiners.ae/dca-bot to get a Pro/Business key.[/dim]")
    else:
        for f in info['features']:
            console.print(f"  ✓ {f}")


@app.command()
def funding(
    config_path: str = typer.Option("./config.yaml", "--config", "-c"),
    history: bool = typer.Option(
        False, "--history", help="Include 30-day average + range."
    ),
):
    """Show current BTC perpetual funding rates (OKX). Detection-only.

    See `docs/FUNDING_MONITOR.md` for what to do with the numbers.
    """
    asyncio.run(_funding(config_path, history))


async def _funding(config_path: str, show_history: bool):
    import httpx
    from decimal import Decimal as D

    cfg = _load_runtime_config(config_path)
    table = Table(title="BTC perpetual funding (live)")
    table.add_column("Exchange"); table.add_column("Instrument")
    table.add_column("8h rate"); table.add_column("Annualized"); table.add_column("Next settle (UTC)")

    headers = {"User-Agent": "bitcoiners-dca"}
    async with httpx.AsyncClient(timeout=15, headers=headers) as client:
        for inst in cfg.funding_monitor.instruments:
            if inst.exchange.lower() != "okx":
                table.add_row(inst.exchange, inst.symbol, "n/a", "n/a", "n/a")
                continue
            try:
                resp = await client.get(
                    "https://www.okx.com/api/v5/public/funding-rate",
                    params={"instId": inst.symbol},
                )
                resp.raise_for_status()
                d = resp.json()["data"][0]
                rate = D(d["fundingRate"])
                ann = rate * D(3) * D(365) * D(100)
                from datetime import datetime, timezone
                nxt = datetime.fromtimestamp(
                    int(d["nextFundingTime"]) / 1000, tz=timezone.utc,
                )
                color = "green" if abs(ann) < 15 else "red"
                table.add_row(
                    inst.exchange.upper(), inst.symbol,
                    f"{rate*100:+.5f}%",
                    f"[{color}]{ann:+.2f}%[/{color}]",
                    nxt.strftime("%Y-%m-%d %H:%M"),
                )
                if show_history:
                    hist = await client.get(
                        "https://www.okx.com/api/v5/public/funding-rate-history",
                        params={"instId": inst.symbol, "limit": 90},
                    )
                    rates = [
                        D(x["fundingRate"]) for x in hist.json()["data"]
                        if x.get("fundingRate")
                    ]
                    if rates:
                        avg = sum(rates) / len(rates)
                        positive = sum(1 for r in rates if r > 0)
                        console.print(
                            f"  [dim]30-day avg: {avg*D(3)*D(365)*D(100):+.2f}% "
                            f"ann | {positive}/{len(rates)} fundings positive | "
                            f"range {min(rates)*100:+.5f}% to {max(rates)*100:+.5f}%[/dim]"
                        )
            except Exception as e:
                table.add_row(inst.exchange, inst.symbol, "err", str(e)[:40], "—")
    console.print(table)


@app.command()
def routes(
    config_path: str = typer.Option("./config.yaml", "--config", "-c"),
    amount_aed: str = typer.Option("500", "--amount", help="Cycle size to audit."),
    pair: str = typer.Option("BTC/AED", "--pair", help="Target pair (BASE/QUOTE)."),
):
    """Show every viable route at a given cycle size — audit tool.

    Lists direct paths, same-exchange two-hop paths, and cross-exchange
    alerts (when enabled). Useful for understanding what the smart router
    would pick under your current config + market conditions.
    """
    asyncio.run(_routes(config_path, Decimal(amount_aed), pair))


async def _routes(config_path: str, amount_aed: Decimal, pair: str):
    cfg = _load_runtime_config(config_path)
    exchanges = _build_exchanges(cfg)
    if not exchanges:
        console.print("[red]No exchanges configured.[/red]")
        return
    router = _build_router(cfg)
    try:
        decision = await router.pick(
            exchanges, pair, required_quote_amount=amount_aed
        )
    except Exception as e:
        console.print(f"[red]Router failed: {e}[/red]")
        for ex in exchanges:
            await ex.close()
        return

    target_asset, quote_ccy = pair.split("/")
    table = Table(title=f"Routes for {amount_aed} {quote_ccy} → {target_asset}")
    table.add_column("Rank"); table.add_column("Route"); table.add_column("Effective price")
    table.add_column("Expected output"); table.add_column("Balance"); table.add_column("Note")

    candidates = [decision.chosen] + decision.alternatives
    all_underfunded = all(
        c.quote_balance is not None and c.quote_balance < amount_aed
        for c in candidates if c.quote_balance is not None
    )
    for i, c in enumerate(candidates, 1):
        expected = c.route.expected_output(amount_aed)
        underfunded = (
            c.quote_balance is not None and c.quote_balance < amount_aed
        )
        bal = (
            f"{c.quote_balance:.2f} {quote_ccy}"
            if c.quote_balance is not None else "—"
        )
        if underfunded:
            bal = f"[yellow]{bal} ⚠[/yellow]"
        note_parts = []
        if i == 1:
            note_parts.append("[bold green]← picked[/bold green]")
        if c.note:
            note_parts.append(c.note)
        if underfunded:
            note_parts.append("[yellow]underfunded[/yellow]")
        table.add_row(
            str(i), c.route.label,
            f"{c.effective_price:,.2f} {quote_ccy}/{target_asset}",
            f"{expected:.8f} {target_asset}", bal, " · ".join(note_parts),
        )
    console.print(table)
    if all_underfunded:
        console.print(
            f"[yellow]⚠ Every candidate route is underfunded at "
            f"{amount_aed} {quote_ccy}. Fund the relevant exchange before "
            f"running a real cycle.[/yellow]"
        )

    if decision.cross_exchange_alerts:
        alert_table = Table(
            title=f"Cross-exchange alerts (manual execution only)"
        )
        alert_table.add_column("Route"); alert_table.add_column("Effective price")
        alert_table.add_column("Expected output")
        for a in decision.cross_exchange_alerts:
            expected = a.route.expected_output(amount_aed)
            alert_table.add_row(
                a.route.label,
                f"{a.effective_price:,.2f} {quote_ccy}/{target_asset}",
                f"{expected:.8f} {target_asset}",
            )
        console.print(alert_table)

    for ex in exchanges:
        await ex.close()


@app.command()
def backtest(
    days: int = typer.Option(
        365, "--days", help="How many days of history to backtest (max 365 free)."
    ),
    amount_aed: str = typer.Option(
        "500", "--amount", help="AED per scheduled buy."
    ),
    frequency: str = typer.Option(
        "weekly", "--frequency", help="daily | weekly | monthly"
    ),
    day_of_week: int = typer.Option(
        0, "--dow", help="0=Mon..6=Sun (weekly only)"
    ),
    taker_fee_pct: str = typer.Option(
        "0.005", "--taker-fee", help="Decimal pct (0.005 = 0.5%)"
    ),
    dip_overlay: bool = typer.Option(
        False, "--dip", help="Enable buy-the-dip overlay"
    ),
    dip_threshold_pct: str = typer.Option(
        "-10", "--dip-threshold", help="Price-change %% that triggers overlay"
    ),
    dip_multiplier: str = typer.Option(
        "2.0", "--dip-multiplier", help="Buy-size multiplier when triggered"
    ),
    vs_currency: str = typer.Option(
        "aed", "--currency", help="Quote currency for historical prices"
    ),
    show_cycles: int = typer.Option(
        20, "--show-cycles", help="Last N cycles to print individually"
    ),
):
    """Replay a DCA strategy against historical BTC prices and print the result."""
    from bitcoiners_dca.core.backtest import (
        BacktestConfig, naive_baseline, run_backtest,
    )
    from bitcoiners_dca.core.historical_prices import (
        HistoricalPriceSource, HistoricalPricesError,
    )

    cfg = BacktestConfig(
        base_amount_aed=Decimal(amount_aed),
        frequency=frequency,
        day_of_week=day_of_week,
        taker_fee_pct=Decimal(taker_fee_pct),
        dip_overlay_enabled=dip_overlay,
        dip_threshold_pct=Decimal(dip_threshold_pct),
        dip_multiplier=Decimal(dip_multiplier),
    )

    try:
        source = HistoricalPriceSource()
        points = source.fetch(vs_currency=vs_currency, days=days)
    except HistoricalPricesError as e:
        console.print(f"[red]{e}[/red]")
        raise typer.Exit(code=1)

    main = run_backtest(cfg, points)
    baseline = naive_baseline(cfg, points) if dip_overlay else None

    # Recent cycles
    if main.cycles:
        recent = main.cycles[-show_cycles:]
        table = Table(title=f"Last {len(recent)} cycles ({main.start_day} → {main.end_day})")
        table.add_column("Date"); table.add_column("Price (AED)"); table.add_column("AED")
        table.add_column("BTC"); table.add_column("Overlay")
        for c in recent:
            table.add_row(
                str(c.day),
                f"{c.price_aed:,.0f}",
                f"{c.aed_spent}",
                f"{c.btc_bought:.8f}",
                "✓" if c.overlay_applied else "",
            )
        console.print(table)

    # Summary
    quote = vs_currency.upper()
    summary = Table(title="Backtest summary")
    summary.add_column("Metric"); summary.add_column("Strategy")
    if baseline:
        summary.add_column("Naive (no overlay)")

    rows = [
        ("Period", f"{main.start_day} → {main.end_day}", None),
        ("Cycles", str(main.cycle_count),
         str(baseline.cycle_count) if baseline else None),
        (f"Total {quote} spent", f"{main.total_aed_spent:,.2f}",
         f"{baseline.total_aed_spent:,.2f}" if baseline else None),
        ("Total BTC bought", f"{main.total_btc_bought:.8f}",
         f"{baseline.total_btc_bought:.8f}" if baseline else None),
        (f"Avg cost ({quote}/BTC)", f"{main.avg_price_aed:,.2f}",
         f"{baseline.avg_price_aed:,.2f}" if baseline else None),
        ("Dip triggers", str(main.overlay_triggers),
         "n/a" if baseline else None),
    ]
    for label, a, b in rows:
        if baseline:
            summary.add_row(label, a, b or "")
        else:
            summary.add_row(label, a)
    console.print(summary)

    if baseline and baseline.total_btc_bought > 0 and main.total_btc_bought > 0:
        sat_diff = main.total_btc_bought - baseline.total_btc_bought
        aed_diff = main.total_aed_spent - baseline.total_aed_spent
        pct_btc = (sat_diff / baseline.total_btc_bought) * Decimal(100)
        if sat_diff > 0:
            console.print(
                f"[green]Overlay net effect:[/green] +{sat_diff:.8f} BTC vs naive "
                f"(+{pct_btc:.2f}%) at +AED {aed_diff:,.2f} extra deployed."
            )
        else:
            console.print(
                f"[yellow]Overlay net effect:[/yellow] {sat_diff:+.8f} BTC vs naive "
                f"({pct_btc:+.2f}%)."
            )


@app.command()
def risk(
    action: str = typer.Argument(
        "status",
        help="status | pause | resume — manage the risk-manager pause state.",
    ),
    reason: str = typer.Option("manual", "--reason", help="Reason logged when pausing."),
    config_path: str = typer.Option("./config.yaml", "--config", "-c"),
):
    """Inspect or toggle the bot's risk-manager pause state."""
    from bitcoiners_dca.core.risk import RiskManager
    cfg = _load_runtime_config(config_path)
    db = Database(cfg.persistence.db_path)
    rm = RiskManager(
        db=db,
        max_daily_aed=cfg.risk.max_daily_aed,
        max_single_buy_aed=cfg.risk.max_single_buy_aed,
        max_consecutive_failures=cfg.risk.max_consecutive_failures,
    )

    act = action.lower()
    if act == "pause":
        rm.pause(reason)
        console.print(f"[yellow]Paused.[/yellow] reason: {reason}")
    elif act == "resume":
        rm.resume()
        console.print("[green]Resumed.[/green] Consecutive-failure counter reset.")
    elif act == "status":
        paused = rm.is_paused()
        console.print(f"[bold]Risk manager state[/bold]")
        console.print(f"  Paused              : {'yes' if paused else 'no'}")
        if paused:
            console.print(f"  Reason              : {rm.paused_reason() or 'n/a'}")
        console.print(f"  Consecutive failures: {rm.consecutive_failures()}")
        console.print(f"  Daily spend (today) : AED {rm.daily_spend_aed()}")
        console.print(f"  Max daily cap       : AED {cfg.risk.max_daily_aed or 'none'}")
        console.print(f"  Max single-buy cap  : AED {cfg.risk.max_single_buy_aed or 'none'}")
        console.print(f"  Failure threshold   : {cfg.risk.max_consecutive_failures}")
    else:
        console.print(f"[red]Unknown action: {action}. Use status | pause | resume.[/red]")
        raise typer.Exit(code=1)
    db.close()


@app.command()
def validate(
    config_path: str = typer.Option("./config.yaml", "--config", "-c"),
    skip_network: bool = typer.Option(
        False, "--skip-network", help="Skip live health_check calls; check config only."
    ),
):
    """Validate config + secrets + exchange connectivity before going live.

    Run this BEFORE flipping dry_run to false. It catches:
      - typos in config.yaml
      - missing env-vars for enabled exchanges
      - bad API keys (live health_check)
      - invalid auto-withdraw destination
      - missing telegram bot token / chat_id when notifications are on
      - unwritable db / reports paths
    """
    failures = asyncio.run(_validate(config_path, skip_network))
    raise typer.Exit(code=1 if failures else 0)


async def _validate(config_path: str, skip_network: bool) -> int:
    from bitcoiners_dca.core.lightning import WithdrawalNetwork, detect_network

    table = Table(title="bitcoiners-dca · config validation", show_lines=False)
    table.add_column("Section"); table.add_column("Check"); table.add_column("Status")
    table.add_column("Detail")

    failures = 0
    warnings = 0

    def row(section: str, check: str, status: str, detail: str = ""):
        nonlocal failures, warnings
        color = {"PASS": "green", "FAIL": "red", "WARN": "yellow", "SKIP": "dim"}.get(status, "white")
        table.add_row(section, check, f"[{color}]{status}[/{color}]", detail)
        if status == "FAIL":
            failures += 1
        elif status == "WARN":
            warnings += 1

    # 1. Config syntax
    try:
        cfg = load_config(config_path)
        row("config", "YAML syntax + schema", "PASS", config_path)
    except Exception as e:
        row("config", "YAML syntax + schema", "FAIL", str(e)[:80])
        console.print(table)
        return 1

    # 2. Exchange env vars + connectivity
    for ex_name, ex_cfg in [
        ("okx", cfg.exchanges.okx),
        ("binance", cfg.exchanges.binance),
        ("bitoasis", cfg.exchanges.bitoasis),
    ]:
        if not ex_cfg.enabled:
            row(ex_name, "enabled", "SKIP", "disabled in config")
            continue
        # Credential presence
        if ex_name == "bitoasis":
            token = ex_cfg.get_token()
            if not token:
                row(ex_name, "credentials", "FAIL", f"{ex_cfg.token_env} not set")
                continue
            row(ex_name, "credentials", "PASS", f"{ex_cfg.token_env} present")
        else:
            key = ex_cfg.get_api_key()
            secret = ex_cfg.get_api_secret()
            if not key or not secret:
                row(ex_name, "credentials", "FAIL",
                    f"{ex_cfg.api_key_env}/{ex_cfg.api_secret_env} missing")
                continue
            if ex_name == "okx" and not ex_cfg.get_passphrase():
                row(ex_name, "credentials", "FAIL",
                    f"{ex_cfg.passphrase_env} missing (OKX requires API passphrase)")
                continue
            row(ex_name, "credentials", "PASS", "env-vars set")

    if not skip_network:
        exchanges = _build_exchanges(cfg)
        for ex in exchanges:
            try:
                await ex.health_check()
                row(ex.name, "health_check", "PASS", "auth + connectivity OK")
            except Exception as e:
                row(ex.name, "health_check", "FAIL", str(e)[:80])
            finally:
                await ex.close()
    else:
        row("network", "health_check", "SKIP", "--skip-network flag")

    # 3. Auto-withdraw destination
    aw = cfg.auto_withdraw
    if aw.enabled:
        if not aw.destination_address:
            row("auto_withdraw", "destination_address", "FAIL", "enabled but unset")
        else:
            detected = detect_network(aw.destination_address)
            if detected == WithdrawalNetwork.BITCOIN:
                row("auto_withdraw", "destination_address", "PASS",
                    f"on-chain ({aw.destination_address[:10]}…)")
            elif detected in (WithdrawalNetwork.LIGHTNING, WithdrawalNetwork.LNURL):
                row("auto_withdraw", "destination_address", "FAIL",
                    "Lightning invoices expire — use an on-chain address for auto-withdraw")
            else:
                row("auto_withdraw", "destination_address", "WARN",
                    f"unrecognized ({detected.value})")
        if aw.threshold_btc <= 0:
            row("auto_withdraw", "threshold_btc", "WARN", "≤0 — withdraws every cycle")
    else:
        row("auto_withdraw", "enabled", "SKIP", "disabled")

    # 4. Notifications
    tg = cfg.notifications.telegram
    if tg.enabled:
        import os
        token = os.environ.get(tg.bot_token_env)
        if not token:
            row("notifications", "telegram.bot_token", "FAIL", f"{tg.bot_token_env} not set")
        else:
            row("notifications", "telegram.bot_token", "PASS", "env-var present")
        if not tg.chat_id:
            row("notifications", "telegram.chat_id", "FAIL", "chat_id missing in config")
        else:
            row("notifications", "telegram.chat_id", "PASS", str(tg.chat_id))
    else:
        row("notifications", "telegram", "SKIP", "disabled")

    # 5. Paths
    import os
    for label, path in [
        ("persistence.db_path", cfg.persistence.db_path),
        ("reports.uae_tax_csv_path", cfg.reports.uae_tax_csv_path),
    ]:
        parent = os.path.dirname(os.path.abspath(path)) or "."
        if not os.path.exists(parent):
            try:
                os.makedirs(parent, exist_ok=True)
                row("paths", label, "PASS", f"created {parent}")
            except Exception as e:
                row("paths", label, "FAIL", f"unwritable: {e}")
        elif os.access(parent, os.W_OK):
            row("paths", label, "PASS", parent)
        else:
            row("paths", label, "FAIL", f"not writable: {parent}")

    # 6. Strategy sanity
    if cfg.strategy.amount_aed <= 0:
        row("strategy", "amount_aed", "FAIL", f"{cfg.strategy.amount_aed} ≤ 0")
    else:
        row("strategy", "amount_aed", "PASS", f"AED {cfg.strategy.amount_aed}")

    # 6b. Risk caps
    r = cfg.risk
    if r.max_daily_aed and r.max_daily_aed < cfg.strategy.amount_aed:
        row("risk", "max_daily_aed", "WARN",
            f"{r.max_daily_aed} < base buy {cfg.strategy.amount_aed} — every cycle clamps to 0")
    elif r.max_daily_aed:
        row("risk", "max_daily_aed", "PASS", f"AED {r.max_daily_aed}")
    else:
        row("risk", "max_daily_aed", "WARN", "no daily cap set")
    if r.max_single_buy_aed:
        row("risk", "max_single_buy_aed", "PASS", f"AED {r.max_single_buy_aed}")
    if r.max_consecutive_failures < 1:
        row("risk", "max_consecutive_failures", "FAIL", "must be ≥ 1")
    else:
        row("risk", "max_consecutive_failures", "PASS",
            f"auto-pause after {r.max_consecutive_failures}")

    # 7. Dry-run state
    if cfg.dry_run:
        row("runtime", "dry_run", "PASS", "ON — simulated only, no real orders")
    else:
        row("runtime", "dry_run", "WARN", "OFF — LIVE trading, real orders will be placed")

    console.print(table)
    if failures:
        summary = f"[red]{failures} failed[/red]"
    else:
        summary = "[green]All checks passed.[/green]"
    if warnings:
        summary += f"  [yellow]{warnings} warnings[/yellow]"
    console.print(summary)
    return failures


@app.command()
def withdraw(
    address: str = typer.Argument(..., help="BTC address OR BOLT11 Lightning invoice (lnbc…)"),
    amount: str = typer.Argument(..., help="Amount of BTC to withdraw (e.g. 0.005)"),
    exchange: str = typer.Option("okx", "--from", help="Source exchange (okx | binance | bitoasis)"),
    network: str = typer.Option("", "--network", help="Override auto-detection: bitcoin | lightning"),
    config_path: str = typer.Option("./config.yaml", "--config", "-c"),
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip the confirmation prompt"),
):
    """Withdraw BTC ad-hoc. Lightning is auto-detected from BOLT11 invoices (OKX only)."""
    try:
        amount_btc = Decimal(amount)
    except Exception:
        console.print(f"[red]Invalid amount: {amount}[/red]")
        raise typer.Exit(code=1)
    asyncio.run(_withdraw(address, amount_btc, exchange, network, config_path, yes))


async def _withdraw(
    address: str,
    amount: Decimal,
    exchange_name: str,
    network: str,
    config_path: str,
    yes: bool,
):
    from bitcoiners_dca.core.lightning import detect_network as _detect

    cfg = _load_runtime_config(config_path)
    exchanges = _build_exchanges(cfg)
    ex = next((e for e in exchanges if e.name == exchange_name), None)
    if ex is None:
        console.print(f"[red]Exchange '{exchange_name}' not enabled or not configured.[/red]")
        raise typer.Exit(code=1)

    detected = _detect(address)
    console.print(f"[bold]Withdraw plan[/bold]")
    console.print(f"  Exchange : {ex.name}")
    console.print(f"  Amount   : {amount} BTC")
    console.print(f"  Address  : {address[:60]}{'…' if len(address) > 60 else ''}")
    console.print(f"  Detected : {detected.value}")
    console.print(f"  Network  : {network or '(auto)'}")
    console.print(f"  Dry-run  : {cfg.dry_run}")

    if not yes and not cfg.dry_run:
        confirm = typer.confirm("Proceed?")
        if not confirm:
            console.print("[yellow]Cancelled.[/yellow]")
            return

    try:
        # If user didn't override --network, infer from the address. Each
        # adapter then validates that its requested network is supported.
        from bitcoiners_dca.core.lightning import is_lightning
        resolved_network = network or ("lightning" if is_lightning(address) else "bitcoin")
        result = await ex.withdraw_btc(
            amount_btc=amount, address=address, network=resolved_network
        )
        console.print(
            f"[green]✓ Withdrawal submitted.[/green] id={result.withdrawal_id} "
            f"status={result.status.value} fee={result.fee} BTC"
        )
    finally:
        for e in exchanges:
            await e.close()


_INLINE_TEMPLATE = """# bitcoiners-dca starter config
license:
  tier: free                  # free | pro | business; see docs/TIERS.md
  key: null

# Edit this file; set secrets via environment variables.

strategy:
  amount_aed: 500
  frequency: weekly        # daily | weekly | monthly
  day_of_week: monday
  time: "09:00"
  timezone: "Asia/Dubai"

overlays:
  buy_the_dip:
    enabled: true
    threshold_pct: -10
    lookback_days: 7
    multiplier: 2.0

routing:
  mode: best_price
  exclude_if_spread_pct_above: 2.0
  preferred_exchange: null

exchanges:
  okx:
    enabled: true
    api_key_env: OKX_API_KEY
    api_secret_env: OKX_API_SECRET
    passphrase_env: OKX_API_PASSPHRASE
  binance:
    # No BTC/AED pair on binance.com — only enable for BTC/USDT DCA.
    enabled: false
    api_key_env: BINANCE_API_KEY
    api_secret_env: BINANCE_API_SECRET
  bitoasis:
    enabled: false
    token_env: BITOASIS_API_TOKEN

auto_withdraw:
  enabled: false
  destination_address: null        # YOUR hardware wallet address, hardcoded here
  threshold_btc: 0.01

risk:
  max_daily_aed: null
  max_single_buy_aed: null
  max_consecutive_failures: 5

arbitrage:
  enabled: true
  min_spread_pct: 1.5
  slippage_buffer_pct: 0.3
  poll_interval_seconds: 300

notifications:
  telegram:
    enabled: false
    bot_token_env: TG_BOT_TOKEN
    chat_id: null

persistence:
  db_path: "./data/dca.db"

reports:
  uae_tax_csv_path: "./reports"

dry_run: true        # SAFETY: starts ON; flip to false only after dry-run audit
"""


if __name__ == "__main__":
    app()
