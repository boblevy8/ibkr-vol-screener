"""Typer CLI for ibkr-vol-screener.

Commands:
  once             one-shot screen across selected markets
  watch            re-run every --interval seconds with live refresh
  scanner-params   download + cache scanner XML, print STK locations/scan codes
  config-example   print a fully-commented TOML config

All commands are read-only by construction (see test_readonly.py).
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import replace
from pathlib import Path

import typer
from rich.console import Console
from rich.live import Live
from rich.logging import RichHandler

from .alerts import AlertState, build_rules_from_kwargs, write_alerts
from .analytics import (
    compute_top_k_churn,
    load_history,
    overview,
    per_symbol_series,
    render_churn_table,
    render_overview_table,
    render_persistent_series_table,
    render_scan_predictiveness_table,
    scan_code_predictiveness,
    write_long_form_csv,
)
from .backtest import format_weight_toml, run_backtest, write_outcomes_csv
from .charts import MatplotlibMissing, plot_metric_history, plot_snapshot_grid, save_figure
from .config import (
    Config,
    MarketBucket,
    apply_cli_overrides,
    config_summary,
    example_config_toml,
    load_config,
)
from .filter_expr import FilterParseError, compile_filters
from .historical import fetch_all, fetch_prior_closes
from .ib_client import ib_session, set_market_data_type
from .metrics import compute_metrics
from .observability import (
    SubscriptionErrorTracker,
    configure_file_logging,
    is_scanner_cancel_receipt,
)
from .reporting import (
    _VALID_SORT_KEYS,
    filter_rows,
    rank_rows,
    render_table,
    write_csv,
    write_html,
    write_json,
)
from .scanner_params import (
    cached_tsx_location_path,
    discover_hk_location,
    discover_tsx_location,
    discover_uk_eu_location,
    fetch_scanner_xml,
    print_stock_scanner_info,
    probe_hk_location,
    probe_tsx_location,
    probe_uk_eu_location,
    read_cache,
    read_cached_hk,
    read_cached_tsx,
    read_cached_uk_eu,
    write_cache,
    write_cached_hk,
    write_cached_tsx,
    write_cached_uk_eu,
)
from .scanners import gather_candidates
from .snapshot import open_for_replay, save_snapshot
from .streaming import StreamingSession
from .watchlist import load_watchlist, qualify_watchlist

# pretty_exceptions_show_locals=False keeps tracebacks tight; the actionable
# fix hint from ib_client.format_connect_hint is logged at ERROR before raise,
# which is what the user actually needs to read.
app = typer.Typer(
    add_completion=False,
    no_args_is_help=True,
    pretty_exceptions_show_locals=False,
)


def _make_console(width: int | None = None) -> Console:
    # Defensive on Windows legacy consoles (cp1252): force UTF-8 if available,
    # otherwise let Rich downgrade.
    import sys as _sys

    reconfigure = getattr(_sys.stdout, "reconfigure", None)
    if reconfigure is not None:
        try:
            reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass
    return Console(width=width) if width is not None else Console()


def _resolve_width(cli_value: int | None) -> int | None:
    """CLI flag wins; otherwise honor IBKR_VOL_SCREENER_WIDTH env."""
    import os as _os

    if cli_value is not None:
        return cli_value
    env_val = _os.environ.get("IBKR_VOL_SCREENER_WIDTH")
    if env_val:
        try:
            return int(env_val)
        except ValueError:
            return None
    return None


console = _make_console()
log = logging.getLogger("ibkr_vol_screener")


class _ScannerCancelFilter(logging.Filter):
    """Suppress IBKR error-162 'API scanner subscription cancelled' lines.

    These are benign completion receipts surfaced by ib_async as errors, not
    actionable failures. They confuse users on the console; we still keep
    them at DEBUG via the file handler.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            msg = record.getMessage()
        except Exception:
            return True
        return not is_scanner_cancel_receipt(msg)


def _configure_logging(verbose: bool, log_file: Path | None = None) -> None:
    rich_handler = RichHandler(
        console=console, show_path=False, show_time=False, markup=False
    )
    rich_handler.addFilter(_ScannerCancelFilter())
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(message)s",
        handlers=[rich_handler],
        force=True,
    )
    # ib_async is chatty at DEBUG; keep it quiet unless explicitly verbose.
    logging.getLogger("ib_async").setLevel(logging.DEBUG if verbose else logging.WARNING)
    if log_file is not None:
        configure_file_logging(log_file)


def _resolve_markets(value: str | None) -> tuple[MarketBucket, ...] | None:
    if not value:
        return None
    items = [s.strip().lower() for s in value.split(",") if s.strip()]
    return tuple(MarketBucket(i) for i in items)


def _build_config(
    *,
    config_path: Path | None,
    host: str | None,
    port: int | None,
    client_id: int | None,
    markets: str | None,
    top_candidates_per_scan: int | None,
    top_results: int | None,
    rth_only: bool | None,
    include_extended_hours: bool | None,
    min_price: float | None,
    min_volume_60m: float | None,
    min_dollar_volume_60m: float | None,
    sort_key: str | None,
    csv_path: Path | None,
    json_path: Path | None,
    html_path: Path | None,
    verbose: bool | None,
    dry_run: bool | None,
    allow_delayed: bool | None = None,
    with_gap: bool | None = None,
    log_file: Path | None = None,
    save_snapshot_dir: Path | None = None,
) -> Config:
    cfg = load_config(config_path) if config_path else Config()

    use_rth: bool | None = None
    if rth_only:
        use_rth = True
    elif include_extended_hours:
        use_rth = False

    apply_cli_overrides(
        cfg,
        host=host,
        port=port,
        client_id=client_id,
        markets=_resolve_markets(markets),
        top_candidates_per_scan=top_candidates_per_scan,
        top_results=top_results,
        use_rth=use_rth,
        sort_key=sort_key,
        csv_path=csv_path,
        json_path=json_path,
        html_path=html_path,
        verbose=verbose,
        dry_run=dry_run,
        allow_delayed=allow_delayed,
        with_gap=with_gap,
        log_file=log_file,
        save_snapshot_dir=save_snapshot_dir,
    )

    # Per-bucket override applied uniformly when the user passes single thresholds.
    if any(v is not None for v in (min_price, min_volume_60m, min_dollar_volume_60m)):
        for bucket, prof in cfg.profiles.items():
            kwargs = {}
            if min_price is not None:
                kwargs["min_price"] = min_price
            if min_volume_60m is not None:
                kwargs["min_volume_60m"] = min_volume_60m
            if min_dollar_volume_60m is not None:
                kwargs["min_dollar_volume_60m"] = min_dollar_volume_60m
            cfg.profiles[bucket] = replace(prof, **kwargs)
    return cfg


async def _ensure_scanner_xml(ib, cfg: Config) -> str:
    cached = read_cache(cfg.cache_dir, cfg.scanner_xml_max_age_h)
    if cached:
        log.info("Using cached scanner XML.")
        return cached
    xml = await fetch_scanner_xml(ib)
    write_cache(xml, cfg.cache_dir)
    return xml


async def _resolve_tsx_location(ib, cfg: Config, xml: str) -> str | None:
    profile = cfg.profiles[MarketBucket.TSX]
    if profile.location_code:
        return profile.location_code
    cached = read_cached_tsx(cfg.cache_dir)
    if cached:
        log.info("Using cached TSX location: %s", cached)
        return cached
    code = discover_tsx_location(xml)
    if code:
        write_cached_tsx(code, cfg.cache_dir)
        return code
    code = await probe_tsx_location(ib, xml)
    if code:
        write_cached_tsx(code, cfg.cache_dir)
        return code
    return None


async def _resolve_uk_eu_location(ib, cfg: Config, xml: str) -> str | None:
    profile = cfg.profiles[MarketBucket.UK_EU]
    if profile.location_code:
        return profile.location_code
    cached = read_cached_uk_eu(cfg.cache_dir)
    if cached:
        log.info("Using cached UK/EU location: %s", cached)
        return cached
    code = discover_uk_eu_location(xml)
    if code:
        write_cached_uk_eu(code, cfg.cache_dir)
        return code
    code = await probe_uk_eu_location(ib, xml)
    if code:
        write_cached_uk_eu(code, cfg.cache_dir)
        return code
    return None


async def _resolve_hk_location(ib, cfg: Config, xml: str) -> str | None:
    profile = cfg.profiles[MarketBucket.HK]
    if profile.location_code:
        return profile.location_code
    cached = read_cached_hk(cfg.cache_dir)
    if cached:
        log.info("Using cached HK location: %s", cached)
        return cached
    code = discover_hk_location(xml)
    if code:
        write_cached_hk(code, cfg.cache_dir)
        return code
    code = await probe_hk_location(ib, xml)
    if code:
        write_cached_hk(code, cfg.cache_dir)
        return code
    return None


async def _run_once(cfg: Config, *, show_progress: bool = True) -> list:
    """Connect, scan, fetch bars, compute, filter, rank. Returns ranked rows."""
    from rich.progress import (
        BarColumn,
        MofNCompleteColumn,
        Progress,
        SpinnerColumn,
        TextColumn,
        TimeElapsedColumn,
    )

    prior_closes: dict[int, float] = {}
    sub_summary: str | None = None

    async with ib_session(cfg.host, cfg.port, cfg.client_id) as ib:
        if cfg.allow_delayed or cfg.market_data_type != 1:
            set_market_data_type(ib, 3 if cfg.allow_delayed else cfg.market_data_type)

        tracker = SubscriptionErrorTracker.attach(ib)
        try:
            xml = await _ensure_scanner_xml(ib, cfg)

            # Resolve TSX if needed.
            if MarketBucket.TSX in cfg.markets:
                tsx_loc = await _resolve_tsx_location(ib, cfg, xml)
                if tsx_loc is None:
                    log.warning("No TSX location resolved; dropping TSX bucket for this run.")
                    cfg.markets = tuple(m for m in cfg.markets if m is not MarketBucket.TSX)
                else:
                    prof = cfg.profiles[MarketBucket.TSX]
                    cfg.profiles[MarketBucket.TSX] = replace(prof, location_code=tsx_loc)

            # Resolve UK/EU if needed.
            if MarketBucket.UK_EU in cfg.markets:
                eu_loc = await _resolve_uk_eu_location(ib, cfg, xml)
                if eu_loc is None:
                    log.warning("No UK/EU location resolved; dropping UK/EU bucket for this run.")
                    cfg.markets = tuple(m for m in cfg.markets if m is not MarketBucket.UK_EU)
                else:
                    prof = cfg.profiles[MarketBucket.UK_EU]
                    cfg.profiles[MarketBucket.UK_EU] = replace(prof, location_code=eu_loc)

            # Resolve HK if needed.
            if MarketBucket.HK in cfg.markets:
                hk_loc = await _resolve_hk_location(ib, cfg, xml)
                if hk_loc is None:
                    log.warning("No HK/Asia location resolved; dropping HK bucket for this run.")
                    cfg.markets = tuple(m for m in cfg.markets if m is not MarketBucket.HK)
                else:
                    prof = cfg.profiles[MarketBucket.HK]
                    cfg.profiles[MarketBucket.HK] = replace(prof, location_code=hk_loc)

            # Candidate generation.
            all_candidates = []
            for bucket in cfg.markets:
                prof = cfg.profiles[bucket]
                cands = await gather_candidates(
                    ib,
                    prof,
                    scanner_xml=xml,
                    top_n_per_scan=cfg.top_candidates_per_scan,
                    max_candidates=cfg.max_candidates_per_market,
                )
                all_candidates.extend(cands)

            # Watchlist: add user-supplied symbols (deduped against scanners).
            wl_symbols: list[str] = list(cfg.watchlist)
            if cfg.watchlist_file is not None:
                try:
                    wl_symbols.extend(load_watchlist(cfg.watchlist_file))
                except FileNotFoundError as exc:
                    log.error("Watchlist file not found: %s", exc)
            if wl_symbols:
                # Dedupe preserving order.
                seen: set[str] = set()
                deduped = [s for s in wl_symbols if not (s in seen or seen.add(s))]
                extras = await qualify_watchlist(ib, deduped)
                existing_ids = {c.con_id for c in all_candidates}
                added = 0
                for c in extras:
                    if c.con_id in existing_ids:
                        continue
                    all_candidates.append(c)
                    existing_ids.add(c.con_id)
                    added += 1
                log.info("Watchlist contributed %d new candidates", added)

            if not all_candidates:
                log.warning("No candidates from any scanner. Exiting screen with empty result.")
                return []
            log.info("Total candidates across markets: %d", len(all_candidates))

            # Historical bars (with optional progress bar).
            if show_progress:
                with Progress(
                    SpinnerColumn(),
                    TextColumn("[progress.description]{task.description}"),
                    BarColumn(),
                    MofNCompleteColumn(),
                    TimeElapsedColumn(),
                    console=console,
                    transient=True,
                ) as progress:
                    task = progress.add_task("Fetching 1-min bars", total=len(all_candidates))

                    def _cb(done: int, total: int) -> None:
                        progress.update(task, completed=done, total=total)

                    bar_results = await fetch_all(
                        ib,
                        all_candidates,
                        concurrency=cfg.concurrency,
                        duration_s=cfg.duration_seconds,
                        bar_size=cfg.bar_size,
                        what_to_show=cfg.what_to_show,
                        use_rth=cfg.use_rth,
                        max_retries=cfg.max_retries,
                        request_delay_s=cfg.request_delay_s,
                        progress_callback=_cb,
                    )
                    if cfg.with_gap:
                        task2 = progress.add_task(
                            "Fetching prior closes", total=len(all_candidates)
                        )

                        def _cb2(done: int, total: int) -> None:
                            progress.update(task2, completed=done, total=total)

                        prior_closes = await fetch_prior_closes(
                            ib,
                            all_candidates,
                            concurrency=cfg.concurrency,
                            max_retries=max(1, cfg.max_retries - 1),
                            request_delay_s=cfg.request_delay_s,
                            progress_callback=_cb2,
                        )
            else:
                bar_results = await fetch_all(
                    ib,
                    all_candidates,
                    concurrency=cfg.concurrency,
                    duration_s=cfg.duration_seconds,
                    bar_size=cfg.bar_size,
                    what_to_show=cfg.what_to_show,
                    use_rth=cfg.use_rth,
                    max_retries=cfg.max_retries,
                    request_delay_s=cfg.request_delay_s,
                )
                if cfg.with_gap:
                    prior_closes = await fetch_prior_closes(
                        ib,
                        all_candidates,
                        concurrency=cfg.concurrency,
                        max_retries=max(1, cfg.max_retries - 1),
                        request_delay_s=cfg.request_delay_s,
                    )
            sub_summary = tracker.summary()
            if cfg.save_snapshot_dir is not None:
                try:
                    save_snapshot(
                        cfg.save_snapshot_dir,
                        cfg,
                        all_candidates,
                        bar_results,
                        prior_closes or None,
                    )
                except Exception as exc:
                    log.error("Failed to save snapshot to %s: %s", cfg.save_snapshot_dir, exc)
        finally:
            tracker.detach(ib)

    # Compute metrics (no IB needed beyond here).
    rows = []
    for br in bar_results:
        if not br.bars:
            continue
        row = compute_metrics(
            br.candidate,
            br.bars,
            min_bars=cfg.min_bars_for_metric,
            min_bars_short=cfg.min_bars_short,
            prior_close=prior_closes.get(br.candidate.con_id),
            score_weights=cfg.score_weights,
        )
        if row is not None:
            row.what_to_show = br.what_to_show
            rows.append(row)

    filtered = filter_rows(rows, cfg.profiles)
    if cfg.filter_exprs:
        try:
            pred = compile_filters(list(cfg.filter_exprs))
        except FilterParseError as exc:
            log.error("Invalid --filter expression: %s", exc)
        else:
            before = len(filtered)
            filtered = [r for r in filtered if pred(r)]
            log.info("Composable filter kept %d / %d rows", len(filtered), before)
    ranked = rank_rows(filtered, sort_key=cfg.sort_key, top_n=cfg.top_results)
    if sub_summary:
        console.print(f"[yellow]{sub_summary}[/yellow]")
    return ranked


def _write_outputs(rows: list, cfg: Config) -> None:
    if cfg.csv_path:
        write_csv(rows, cfg.csv_path)
    if cfg.json_path:
        write_json(rows, cfg.json_path)
    if cfg.html_path:
        write_html(rows, cfg.html_path, sort_key=cfg.sort_key)


# ---------------------- COMMANDS ----------------------


@app.command()
def once(
    config: Path | None = typer.Option(None, "--config", help="TOML config path."),
    host: str | None = typer.Option(None, "--host"),
    port: int | None = typer.Option(None, "--port"),
    client_id: int | None = typer.Option(None, "--client-id"),
    markets: str | None = typer.Option(
        None, "--markets", help="Comma list: us_major,tsx,otc"
    ),
    top_candidates_per_scan: int | None = typer.Option(None, "--top-candidates-per-scan"),
    top_results: int | None = typer.Option(None, "--top-results"),
    rth_only: bool = typer.Option(False, "--rth-only", help="Regular hours only."),
    include_extended_hours: bool = typer.Option(
        False, "--include-extended-hours", help="Include pre/post market (default)."
    ),
    min_price: float | None = typer.Option(None, "--min-price"),
    min_volume_60m: float | None = typer.Option(None, "--min-volume-60m"),
    min_dollar_volume_60m: float | None = typer.Option(None, "--min-dollar-volume-60m"),
    sort: str | None = typer.Option(
        None,
        "--sort",
        help="range_pct | abs_return_pct | volume_60m | dollar_volume_60m",
    ),
    csv_out: Path | None = typer.Option(None, "--csv"),
    json_out: Path | None = typer.Option(None, "--json"),
    html_out: Path | None = typer.Option(None, "--html"),
    verbose: bool = typer.Option(False, "--verbose"),
    dry_run: bool = typer.Option(
        False, "--dry-run", help="Skip IB connection; print resolved config and exit."
    ),
    allow_delayed: bool = typer.Option(
        False, "--allow-delayed", help="Switch to 15-min delayed market data."
    ),
    with_gap: bool = typer.Option(
        False,
        "--with-gap",
        help="Also fetch each candidate's prior session close and report gap_pct "
        "(roughly doubles pacing cost).",
    ),
    log_file: Path | None = typer.Option(
        None, "--log-file", help="Append DEBUG-level rotating log to this path."
    ),
    save_snapshot_dir: Path | None = typer.Option(
        None,
        "--save-snapshot",
        help="Persist the fetched bars + candidates to PATH for later replay.",
    ),
    width: int | None = typer.Option(
        None,
        "--width",
        help="Force console width (overrides terminal autodetect).",
    ),
    no_progress: bool = typer.Option(
        False,
        "--no-progress",
        help="Suppress the live progress widget during historical-bar fetch.",
    ),
    watchlist_file: Path | None = typer.Option(
        None,
        "--watchlist",
        help="File of symbols (one per line) to add to the candidate pool.",
    ),
    filter_exprs: list[str] = typer.Option(
        [],
        "--filter",
        help="Filter expression, e.g. 'range_pct>3 AND volume_60m>500000'. "
        "Pass multiple times to AND-combine.",
    ),
) -> None:
    """Run one screen across selected markets and print a ranked table."""
    global console
    resolved_width = _resolve_width(width)
    if resolved_width is not None:
        console = _make_console(resolved_width)
    _configure_logging(verbose, log_file)
    cfg = _build_config(
        config_path=config,
        host=host,
        port=port,
        client_id=client_id,
        markets=markets,
        top_candidates_per_scan=top_candidates_per_scan,
        top_results=top_results,
        rth_only=rth_only,
        include_extended_hours=include_extended_hours,
        min_price=min_price,
        min_volume_60m=min_volume_60m,
        min_dollar_volume_60m=min_dollar_volume_60m,
        sort_key=sort,
        csv_path=csv_out,
        json_path=json_out,
        html_path=html_out,
        verbose=verbose,
        dry_run=dry_run,
        allow_delayed=allow_delayed,
        with_gap=with_gap,
        log_file=log_file,
        save_snapshot_dir=save_snapshot_dir,
    )
    if watchlist_file is not None:
        cfg.watchlist_file = watchlist_file
    if filter_exprs:
        cfg.filter_exprs = tuple(filter_exprs)
    if cfg.dry_run:
        import json as _json

        console.print("[bold yellow]--dry-run: resolved config:[/bold yellow]")
        console.print_json(_json.dumps(config_summary(cfg), default=str))
        return
    rows = asyncio.run(_run_once(cfg, show_progress=not no_progress))
    if not rows:
        console.print("[yellow]No rows passed filters.[/yellow]")
    else:
        console.print(
            render_table(rows, sort_key=cfg.sort_key, console_width=console.size.width)
        )
        _write_outputs(rows, cfg)


def _emit_cycle(live, rows, cfg, rules, alert_state, alerts_log) -> None:
    """Shared post-metrics rendering: alerts -> render -> outputs."""
    alerted_symbols: set[str] = set()
    if rows and rules:
        fired = alert_state.evaluate(rows, rules)
        if fired:
            write_alerts(alerts_log, fired)
            alerted_symbols = {a.symbol for a in fired}
    if rows:
        live.update(
            render_table(
                rows,
                sort_key=cfg.sort_key,
                alerted_symbols=alerted_symbols,
                console_width=console.size.width,
            )
        )
        _write_outputs(rows, cfg)
    else:
        live.update("[yellow]No rows passed filters this cycle.[/yellow]")


async def _run_watch_streaming(
    cfg: Config,
    *,
    interval: float,
    snapshot_dir: Path,
    no_save_snapshot: bool,
    rules: list,
    alert_state,
    alerts_log: Path,
) -> None:
    """Watch loop powered by a persistent StreamingSession.

    Holds one IB connection open for the whole run. Per-cycle work:
      1. Refresh scanner XML (cached). Re-scan markets. Add watchlist.
      2. sync_candidate_pool(): subscribe new candidates, unsubscribe
         dropouts (each new subscription bootstraps via reqHistoricalData).
      3. For each active conId: get_bars() from the rolling buffer,
         compute_metrics, filter, rank.
      4. Save snapshot (from buffer), evaluate alerts, render.
    """
    from datetime import datetime as _dt

    async with ib_session(cfg.host, cfg.port, cfg.client_id) as ib:
        if cfg.allow_delayed or cfg.market_data_type != 1:
            set_market_data_type(ib, 3 if cfg.allow_delayed else cfg.market_data_type)

        tracker = SubscriptionErrorTracker.attach(ib)
        session = StreamingSession(
            ib, max_subscriptions=cfg.streaming_max_subscriptions
        )
        log.info(
            "Streaming watch loop starting (max_subs=%d, backfill=%ds, interval=%.0fs)",
            cfg.streaming_max_subscriptions,
            cfg.streaming_backfill_seconds,
            interval,
        )

        try:
            with Live(console=console, refresh_per_second=2) as live:
                while True:
                    # 1) Scanner XML + candidate generation.
                    xml = await _ensure_scanner_xml(ib, cfg)
                    # Resolve TSX / UK-EU / HK if needed (mutates cfg in-place).
                    if MarketBucket.TSX in cfg.markets:
                        tsx_loc = await _resolve_tsx_location(ib, cfg, xml)
                        if tsx_loc is None:
                            cfg.markets = tuple(m for m in cfg.markets if m is not MarketBucket.TSX)
                        else:
                            prof = cfg.profiles[MarketBucket.TSX]
                            cfg.profiles[MarketBucket.TSX] = replace(prof, location_code=tsx_loc)
                    if MarketBucket.UK_EU in cfg.markets:
                        eu_loc = await _resolve_uk_eu_location(ib, cfg, xml)
                        if eu_loc is None:
                            cfg.markets = tuple(m for m in cfg.markets if m is not MarketBucket.UK_EU)
                        else:
                            prof = cfg.profiles[MarketBucket.UK_EU]
                            cfg.profiles[MarketBucket.UK_EU] = replace(prof, location_code=eu_loc)
                    if MarketBucket.HK in cfg.markets:
                        hk_loc = await _resolve_hk_location(ib, cfg, xml)
                        if hk_loc is None:
                            cfg.markets = tuple(m for m in cfg.markets if m is not MarketBucket.HK)
                        else:
                            prof = cfg.profiles[MarketBucket.HK]
                            cfg.profiles[MarketBucket.HK] = replace(prof, location_code=hk_loc)

                    all_candidates = []
                    for bucket in cfg.markets:
                        prof = cfg.profiles[bucket]
                        cands = await gather_candidates(
                            ib, prof,
                            scanner_xml=xml,
                            top_n_per_scan=cfg.top_candidates_per_scan,
                            max_candidates=cfg.max_candidates_per_market,
                        )
                        all_candidates.extend(cands)

                    # Watchlist additions.
                    wl_symbols: list[str] = list(cfg.watchlist)
                    if cfg.watchlist_file is not None:
                        try:
                            wl_symbols.extend(load_watchlist(cfg.watchlist_file))
                        except FileNotFoundError as exc:
                            log.error("Watchlist file not found: %s", exc)
                    if wl_symbols:
                        seen: set[str] = set()
                        deduped = [s for s in wl_symbols if not (s in seen or seen.add(s))]
                        extras = await qualify_watchlist(ib, deduped)
                        existing_ids = {c.con_id for c in all_candidates}
                        for c in extras:
                            if c.con_id not in existing_ids:
                                all_candidates.append(c)
                                existing_ids.add(c.con_id)

                    # 2) Reconcile subscriptions with the current candidate pool.
                    added, removed = await session.sync_candidate_pool(
                        all_candidates,
                        backfill_seconds=cfg.streaming_backfill_seconds,
                    )
                    if added or removed:
                        log.info(
                            "Subscription sync: +%d / -%d (active=%d)",
                            added, removed, len(session.active_con_ids()),
                        )

                    # 3) Compute metrics from buffers.
                    rows = []
                    bar_results_for_snapshot = []
                    for con_id in session.active_con_ids():
                        cand = session.get_candidate(con_id)
                        if cand is None:
                            continue
                        bars = session.get_bars(con_id)
                        if not bars:
                            continue
                        row = compute_metrics(
                            cand,
                            bars,
                            min_bars=cfg.min_bars_for_metric,
                            min_bars_short=cfg.min_bars_short,
                            score_weights=cfg.score_weights,
                        )
                        if row is not None:
                            row.what_to_show = "TRADES"
                            rows.append(row)
                            # Lightweight BarsResult substitute for snapshot save.
                            from .historical import BarsResult as _BR
                            bar_results_for_snapshot.append(_BR(candidate=cand, bars=list(bars)))

                    # 4) Snapshot save.
                    if not no_save_snapshot:
                        stamp = _dt.utcnow().strftime("%Y-%m-%dT%H-%M-%S")
                        try:
                            save_snapshot(
                                snapshot_dir / stamp,
                                cfg,
                                [br.candidate for br in bar_results_for_snapshot],
                                bar_results_for_snapshot,
                            )
                        except Exception as exc:
                            log.error("Snapshot save failed: %s", exc)

                    # 5) Filter + rank + render.
                    filtered = filter_rows(rows, cfg.profiles)
                    if cfg.filter_exprs:
                        try:
                            pred = compile_filters(list(cfg.filter_exprs))
                            filtered = [r for r in filtered if pred(r)]
                        except FilterParseError as exc:
                            log.error("Invalid --filter expression: %s", exc)
                    ranked = rank_rows(filtered, sort_key=cfg.sort_key, top_n=cfg.top_results)

                    _emit_cycle(live, ranked, cfg, rules, alert_state, alerts_log)

                    summary = tracker.summary()
                    if summary:
                        console.print(f"[yellow]{summary}[/yellow]")

                    await asyncio.sleep(max(5.0, interval))
        finally:
            tracker.detach(ib)
            await session.close()


@app.command()
def watch(
    interval: float = typer.Option(60.0, "--interval", help="Refresh interval in seconds."),
    config: Path | None = typer.Option(None, "--config"),
    host: str | None = typer.Option(None, "--host"),
    port: int | None = typer.Option(None, "--port"),
    client_id: int | None = typer.Option(None, "--client-id"),
    markets: str | None = typer.Option(None, "--markets"),
    top_candidates_per_scan: int | None = typer.Option(None, "--top-candidates-per-scan"),
    top_results: int | None = typer.Option(None, "--top-results"),
    rth_only: bool = typer.Option(False, "--rth-only"),
    include_extended_hours: bool = typer.Option(False, "--include-extended-hours"),
    min_price: float | None = typer.Option(None, "--min-price"),
    min_volume_60m: float | None = typer.Option(None, "--min-volume-60m"),
    min_dollar_volume_60m: float | None = typer.Option(None, "--min-dollar-volume-60m"),
    sort: str | None = typer.Option(None, "--sort"),
    csv_out: Path | None = typer.Option(None, "--csv"),
    json_out: Path | None = typer.Option(None, "--json"),
    html_out: Path | None = typer.Option(None, "--html"),
    verbose: bool = typer.Option(False, "--verbose"),
    allow_delayed: bool = typer.Option(False, "--allow-delayed"),
    with_gap: bool = typer.Option(False, "--with-gap"),
    log_file: Path | None = typer.Option(None, "--log-file"),
    snapshot_dir: Path = typer.Option(
        Path("./snapshots"),
        "--snapshot-dir",
        help="Root directory for per-cycle snapshots in watch mode.",
    ),
    no_save_snapshot: bool = typer.Option(
        False,
        "--no-save-snapshot",
        help="Disable the default per-cycle snapshot saving.",
    ),
    alert_range_pct: float | None = typer.Option(
        None, "--alert-range-pct", help="Alert when range_pct >= N."
    ),
    alert_abs_return_pct: float | None = typer.Option(
        None, "--alert-abs-return-pct"
    ),
    alert_realized_vol: float | None = typer.Option(
        None, "--alert-realized-vol", help="Alert when realized_vol_60m >= N."
    ),
    alert_atr_pct: float | None = typer.Option(
        None, "--alert-atr-pct", help="Alert when atr_pct_60m >= N."
    ),
    alert_volume_60m: float | None = typer.Option(None, "--alert-volume-60m"),
    alert_dollar_volume_60m: float | None = typer.Option(
        None, "--alert-dollar-volume-60m"
    ),
    alert_vwap_dev_pct: float | None = typer.Option(
        None,
        "--alert-vwap-dev-pct",
        help="Alert when |vwap_dev_pct| >= N (signed magnitude).",
    ),
    alert_gap_pct: float | None = typer.Option(
        None,
        "--alert-gap-pct",
        help="Alert when |gap_pct| >= N (signed magnitude; requires --with-gap).",
    ),
    alerts_log: Path = typer.Option(
        Path("./alerts.log"),
        "--alerts-log",
        help="Append fired alerts to this file when any --alert-* is set.",
    ),
    watchlist_file: Path | None = typer.Option(
        None,
        "--watchlist",
        help="File of symbols (one per line) to add to the candidate pool.",
    ),
    filter_exprs: list[str] = typer.Option(
        [], "--filter", help="Composable filter expression (AND-combined)."
    ),
    no_streaming: bool = typer.Option(
        False,
        "--no-streaming",
        help="Disable the streaming-bars architecture and fall back to "
        "per-cycle reqHistoricalData. Streaming is the default for watch.",
    ),
    streaming_max_subs: int | None = typer.Option(
        None,
        "--streaming-max-subs",
        help="Cap on simultaneous live subscriptions (default 100).",
    ),
) -> None:
    """Re-run the screen every --interval seconds with a live-refreshing table."""
    _configure_logging(verbose, log_file)
    cfg = _build_config(
        config_path=config,
        host=host,
        port=port,
        client_id=client_id,
        markets=markets,
        top_candidates_per_scan=top_candidates_per_scan,
        top_results=top_results,
        rth_only=rth_only,
        include_extended_hours=include_extended_hours,
        min_price=min_price,
        min_volume_60m=min_volume_60m,
        min_dollar_volume_60m=min_dollar_volume_60m,
        sort_key=sort,
        csv_path=csv_out,
        json_path=json_out,
        html_path=html_out,
        verbose=verbose,
        dry_run=False,
        allow_delayed=allow_delayed,
        with_gap=with_gap,
        log_file=log_file,
    )
    if watchlist_file is not None:
        cfg.watchlist_file = watchlist_file
    if filter_exprs:
        cfg.filter_exprs = tuple(filter_exprs)
    cfg.streaming = not no_streaming
    if streaming_max_subs is not None:
        cfg.streaming_max_subscriptions = streaming_max_subs

    rules = build_rules_from_kwargs(
        alert_range_pct=alert_range_pct,
        alert_abs_return_pct=alert_abs_return_pct,
        alert_realized_vol=alert_realized_vol,
        alert_atr_pct=alert_atr_pct,
        alert_volume_60m=alert_volume_60m,
        alert_dollar_volume_60m=alert_dollar_volume_60m,
        alert_vwap_dev_pct=alert_vwap_dev_pct,
        alert_gap_pct=alert_gap_pct,
    )
    alert_state = AlertState()
    if rules:
        log.info(
            "Alert rules active: %s; log -> %s",
            ", ".join(f"{r.metric}>={r.threshold:g}" for r in rules),
            alerts_log,
        )

    async def loop_legacy() -> None:
        """Per-cycle reqHistoricalData (the pre-v0.6.0 behavior)."""
        from datetime import datetime as _dt

        with Live(console=console, refresh_per_second=2) as live:
            while True:
                if not no_save_snapshot:
                    stamp = _dt.utcnow().strftime("%Y-%m-%dT%H-%M-%S")
                    cfg.save_snapshot_dir = snapshot_dir / stamp
                else:
                    cfg.save_snapshot_dir = None
                rows = await _run_once(cfg, show_progress=False)
                _emit_cycle(live, rows, cfg, rules, alert_state, alerts_log)
                await asyncio.sleep(max(5.0, interval))

    try:
        if cfg.streaming:
            asyncio.run(_run_watch_streaming(
                cfg,
                interval=interval,
                snapshot_dir=snapshot_dir,
                no_save_snapshot=no_save_snapshot,
                rules=rules,
                alert_state=alert_state,
                alerts_log=alerts_log,
            ))
        else:
            asyncio.run(loop_legacy())
    except KeyboardInterrupt:
        console.print("\n[dim]Interrupted; exiting watch loop.[/dim]")


@app.command("scanner-params")
def scanner_params_cmd(
    host: str = typer.Option("127.0.0.1", "--host"),
    port: int = typer.Option(7497, "--port"),
    client_id: int = typer.Option(42, "--client-id"),
    refresh: bool = typer.Option(False, "--refresh", help="Force refetch (ignore cache)."),
    verbose: bool = typer.Option(False, "--verbose"),
) -> None:
    """Download and pretty-print the IBKR scanner XML (STK locations + scan codes)."""
    _configure_logging(verbose)
    cfg = Config(host=host, port=port, client_id=client_id)

    async def _go() -> None:
        async with ib_session(cfg.host, cfg.port, cfg.client_id) as ib:
            if not refresh:
                cached = read_cache(cfg.cache_dir, cfg.scanner_xml_max_age_h)
                if cached:
                    console.print("[dim]Using cached scanner XML.[/dim]")
                    print_stock_scanner_info(cached)
                    return
            xml = await fetch_scanner_xml(ib)
            write_cache(xml, cfg.cache_dir)
            console.print(f"[green]Scanner XML cached at {cfg.cache_dir / 'scanner_params.xml'}")
            print_stock_scanner_info(xml)
            tsx_cache = cached_tsx_location_path(cfg.cache_dir)
            console.print(
                f"[dim]TSX location cache (if discovered) -> {tsx_cache}[/dim]"
            )

    asyncio.run(_go())


@app.command("config-example")
def config_example_cmd() -> None:
    """Print an example TOML configuration."""
    # Bypass Rich's markup parser so [connection] stays literal.
    typer.echo(example_config_toml())


@app.command()
def replay(
    snapshot_dir: Path = typer.Argument(..., help="Path to a snapshot directory."),
    markets: str | None = typer.Option(None, "--markets"),
    top_results: int | None = typer.Option(None, "--top-results"),
    sort: str | None = typer.Option(None, "--sort"),
    min_price: float | None = typer.Option(None, "--min-price"),
    min_volume_60m: float | None = typer.Option(None, "--min-volume-60m"),
    min_dollar_volume_60m: float | None = typer.Option(None, "--min-dollar-volume-60m"),
    min_bars_for_metric: int | None = typer.Option(None, "--min-bars"),
    csv_out: Path | None = typer.Option(None, "--csv"),
    json_out: Path | None = typer.Option(None, "--json"),
    html_out: Path | None = typer.Option(None, "--html"),
    verbose: bool = typer.Option(False, "--verbose"),
    width: int | None = typer.Option(None, "--width"),
    filter_exprs: list[str] = typer.Option(
        [], "--filter", help="Composable filter expression (AND-combined)."
    ),
) -> None:
    """Re-rank a previously saved snapshot without contacting IBKR."""
    global console
    resolved_width = _resolve_width(width)
    if resolved_width is not None:
        console = _make_console(resolved_width)
    _configure_logging(verbose)
    inputs = open_for_replay(snapshot_dir)
    if inputs.captured_at:
        log.info("Loaded snapshot captured at %s", inputs.captured_at)
    log.info(
        "Replaying %d candidates, %d with bars on disk.",
        len(inputs.candidates),
        sum(1 for b in inputs.bars_by_conid.values() if b),
    )

    # Start from defaults, then layer the captured cfg + CLI overrides.
    cfg = Config()
    captured_markets = inputs.cfg_dict.get("markets") or []
    if captured_markets:
        cfg.markets = tuple(MarketBucket(m) for m in captured_markets)
    apply_cli_overrides(
        cfg,
        markets=_resolve_markets(markets),
        top_results=top_results,
        sort_key=sort,
        csv_path=csv_out,
        json_path=json_out,
        html_path=html_out,
        min_bars_for_metric=min_bars_for_metric,
        verbose=verbose,
    )
    if any(v is not None for v in (min_price, min_volume_60m, min_dollar_volume_60m)):
        for bucket, prof in cfg.profiles.items():
            kwargs = {}
            if min_price is not None:
                kwargs["min_price"] = min_price
            if min_volume_60m is not None:
                kwargs["min_volume_60m"] = min_volume_60m
            if min_dollar_volume_60m is not None:
                kwargs["min_dollar_volume_60m"] = min_dollar_volume_60m
            cfg.profiles[bucket] = replace(prof, **kwargs)

    rows = []
    for cand in inputs.candidates:
        bars = inputs.bars_by_conid.get(cand.con_id, [])
        if not bars:
            continue
        row = compute_metrics(
            cand,
            bars,
            min_bars=cfg.min_bars_for_metric,
            min_bars_short=cfg.min_bars_short,
            prior_close=inputs.prior_closes.get(cand.con_id),
            score_weights=cfg.score_weights,
        )
        if row is not None:
            row.what_to_show = inputs.what_to_show_by_conid.get(cand.con_id, "TRADES")
            rows.append(row)

    filtered = filter_rows(rows, cfg.profiles)
    if filter_exprs:
        try:
            pred = compile_filters(list(filter_exprs))
        except FilterParseError as exc:
            raise typer.BadParameter(str(exc)) from None
        filtered = [r for r in filtered if pred(r)]
    ranked = rank_rows(filtered, sort_key=cfg.sort_key, top_n=cfg.top_results)
    if not ranked:
        console.print("[yellow]No rows passed filters.[/yellow]")
    else:
        console.print(
            render_table(ranked, sort_key=cfg.sort_key, console_width=console.size.width)
        )
        _write_outputs(ranked, cfg)


@app.command()
def analyze(
    root: Path = typer.Argument(
        ..., help="Snapshots root (e.g. ./snapshots)."
    ),
    sort: str = typer.Option(
        "range_pct",
        "--sort",
        help=f"Ranking key for top-K churn. One of: {', '.join(_VALID_SORT_KEYS)}.",
    ),
    top_k: int = typer.Option(10, "--top-k"),
    metric: str = typer.Option(
        "range_pct",
        "--metric",
        help="Metric to summarize in the persistent-symbols + scan-code tables.",
    ),
    min_fraction: float = typer.Option(
        0.25,
        "--min-fraction",
        help="Minimum fraction of cycles a symbol must appear in for the "
        "persistent-symbols table (0..1).",
    ),
    csv_out: Path | None = typer.Option(None, "--csv"),
    width: int | None = typer.Option(None, "--width"),
    verbose: bool = typer.Option(False, "--verbose"),
) -> None:
    """Analyze a directory of saved snapshots (no IBKR connection)."""
    if sort not in _VALID_SORT_KEYS:
        raise typer.BadParameter(
            f"--sort must be one of {', '.join(_VALID_SORT_KEYS)} (got {sort!r})"
        )
    if metric not in _VALID_SORT_KEYS:
        raise typer.BadParameter(
            f"--metric must be one of {', '.join(_VALID_SORT_KEYS)} (got {metric!r})"
        )
    global console
    resolved_width = _resolve_width(width)
    if resolved_width is not None:
        console = _make_console(resolved_width)
    _configure_logging(verbose)

    history = load_history(root)
    if not history:
        console.print(f"[yellow]No snapshots found under {root}[/yellow]")
        return

    ov = overview(history)
    churn = compute_top_k_churn(history, k=top_k, sort_key=sort)
    series = per_symbol_series(history)
    pred = scan_code_predictiveness(history, metric=metric)

    console.print(render_overview_table(ov))
    console.print(render_churn_table(churn))
    console.print(
        render_persistent_series_table(
            series, cycles=ov.cycles, metric=metric, min_fraction=min_fraction
        )
    )
    console.print(render_scan_predictiveness_table(pred))

    if csv_out:
        write_long_form_csv(history, csv_out)


@app.command()
def chart(
    path: Path = typer.Argument(
        ...,
        help="Snapshot directory (default) or snapshots root (with --history).",
    ),
    history: bool = typer.Option(
        False,
        "--history",
        help="Treat `path` as a snapshots root; render a multi-cycle metric history.",
    ),
    top_k: int = typer.Option(8, "--top-k"),
    metric: str = typer.Option(
        "range_pct",
        "--metric",
        help="Metric to plot in --history mode.",
    ),
    sort: str = typer.Option(
        "range_pct",
        "--sort",
        help="Ranking key (top-K by this in both modes).",
    ),
    out: Path | None = typer.Option(
        None, "--out", help="Output file path (.png, .svg, .pdf). Default: ./chart.png"
    ),
    show: bool = typer.Option(
        False, "--show", help="Also open the chart in a GUI viewer."
    ),
    dpi: int = typer.Option(110, "--dpi"),
    verbose: bool = typer.Option(False, "--verbose"),
) -> None:
    """Render a matplotlib chart from a snapshot (or snapshots history).

    Requires the `charts` extra: `pip install ibkr-vol-screener[charts]`.
    """
    if sort not in _VALID_SORT_KEYS:
        raise typer.BadParameter(
            f"--sort must be one of {', '.join(_VALID_SORT_KEYS)} (got {sort!r})"
        )
    if metric not in _VALID_SORT_KEYS:
        raise typer.BadParameter(
            f"--metric must be one of {', '.join(_VALID_SORT_KEYS)} (got {metric!r})"
        )
    _configure_logging(verbose)
    try:
        if history:
            fig = plot_metric_history(path, top_k=top_k, metric=metric, sort_key=sort)
        else:
            fig = plot_snapshot_grid(path, top_k=top_k, sort_key=sort)
    except MatplotlibMissing as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(code=1) from None

    out_path = out or Path("chart.png")
    save_figure(fig, out_path, dpi=dpi)
    console.print(f"[green]Chart written -> {out_path}[/green]")
    if show:
        try:
            import matplotlib
            matplotlib.use("TkAgg", force=True)
            import matplotlib.pyplot as plt
            plt.show()
        except Exception as exc:
            log.warning("Could not open interactive viewer: %s", exc)


@app.command()
def backtest(
    root: Path = typer.Argument(..., help="Snapshots root (e.g. ./snapshots)."),
    k: int = typer.Option(10, "--top-k"),
    target: str = typer.Option(
        "forward_range_pct",
        "--target",
        help="What to predict: forward_range_pct (default, captures absolute "
        "movement) or forward_return_pct (signed direction).",
    ),
    tune_weights: bool = typer.Option(
        False,
        "--tune-weights",
        help="Print a [score] weights TOML block from per-metric correlations.",
    ),
    csv_out: Path | None = typer.Option(None, "--csv"),
    width: int | None = typer.Option(None, "--width"),
    verbose: bool = typer.Option(False, "--verbose"),
) -> None:
    """Walk saved snapshot pairs and report what composite_score actually predicted.

    Offline-only: no IBKR connection. Requires at least two snapshots in `root`.
    """
    if target not in ("forward_range_pct", "forward_return_pct"):
        raise typer.BadParameter(
            f"--target must be forward_range_pct or forward_return_pct (got {target!r})"
        )
    global console
    resolved_width = _resolve_width(width)
    if resolved_width is not None:
        console = _make_console(resolved_width)
    _configure_logging(verbose)

    report = run_backtest(root, k=k, target=target, tune_weights=tune_weights)
    if not report.outcomes:
        console.print(
            f"[yellow]No backtest data: need >= 2 snapshots under {root} "
            f"with overlapping symbols.[/yellow]"
        )
        return

    from rich.table import Table

    # 1. Overview
    overview = Table(title="Backtest overview", show_lines=False)
    overview.add_column("Field", style="bold")
    overview.add_column("Value")
    overview.add_row("Snapshot pairs", str(report.pairs))
    overview.add_row("Forward outcomes", str(len(report.outcomes)))
    overview.add_row("Target", report.target)
    console.print(overview)

    # 2. Per-metric correlation
    corr_tbl = Table(
        title=f"Per-metric Spearman correlation with {report.target}",
        show_lines=False,
    )
    corr_tbl.add_column("Metric", style="bold cyan")
    corr_tbl.add_column("Spearman", justify="right")
    corr_tbl.add_column("|Spearman|", justify="right", style="bold yellow")
    corr_tbl.add_column("N", justify="right")
    for c in report.correlations:
        rho_style = "green" if c.spearman > 0 else ("red" if c.spearman < 0 else "dim")
        corr_tbl.add_row(
            c.metric,
            f"[{rho_style}]{c.spearman:+.4f}[/{rho_style}]",
            f"{c.spearman_abs:.4f}",
            str(c.n),
        )
    console.print(corr_tbl)

    # 3. Top-K vs baseline
    if report.top_k_summary is not None:
        s = report.top_k_summary
        tk = Table(
            title=f"Top-{s.k} by composite_score vs baseline",
            show_lines=False,
        )
        tk.add_column("Stat", style="bold")
        tk.add_column(f"Top-{s.k}", justify="right", style="bold green")
        tk.add_column("Baseline (all)", justify="right")
        tk.add_column("Lift", justify="right", style="bold yellow")
        tk.add_row(
            "Mean",
            f"{s.topk_mean:+.4f}",
            f"{s.baseline_mean:+.4f}",
            f"{s.mean_lift:+.4f}",
        )
        tk.add_row(
            "Median",
            f"{s.topk_median:+.4f}",
            f"{s.baseline_median:+.4f}",
            f"{s.topk_median - s.baseline_median:+.4f}",
        )
        tk.add_row(
            "Win rate",
            f"{s.topk_win_rate:.0%}",
            f"{s.baseline_win_rate:.0%}",
            f"{(s.topk_win_rate - s.baseline_win_rate):+.0%}",
        )
        console.print(tk)

    # 4. Suggested weights
    if report.suggested_weights is not None:
        toml_block = format_weight_toml(report.suggested_weights, len(report.outcomes))
        console.print("\n[bold]Suggested score weights (paste into config TOML):[/bold]")
        typer.echo(toml_block)
    elif tune_weights:
        console.print("[yellow]No usable signal in correlations; weights not tuned.[/yellow]")

    if csv_out:
        write_outcomes_csv(report, csv_out)


def main() -> None:  # pragma: no cover - convenience entrypoint
    app()


if __name__ == "__main__":  # pragma: no cover
    main()
