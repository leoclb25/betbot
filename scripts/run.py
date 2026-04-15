"""
BetBot CLI entry point.

Usage:
  # Paper trading (simulation, $100 virtual balance)
  python -m scripts.run weather --mode paper

  # Live trading (real wallet – requires .env credentials)
  python -m scripts.run weather --mode live

  # Single scan (don't loop)
  python -m scripts.run weather --mode paper --once

  # Show current balance summary
  python -m scripts.run status

  # Reset paper trading state (fresh virtual balance from PAPER_INITIAL_BALANCE)
  python -m scripts.run paper-reset -y
  python -m scripts.run paper-reset -y --with-logs   # also clear operations / balance_summary

  # paper-reset funciona también con python3 del sistema (sin venv): solo usa la stdlib
  # antes de importar click/dotenv. Si data/ es de betbot: sudo python3 -m scripts.run paper-reset …
"""

from __future__ import annotations

import sys
from pathlib import Path

# Repo root (…/betbot) — rutas de datos no dependen del cwd
_REPO_ROOT = Path(__file__).resolve().parents[1]
PAPER_STATE = _REPO_ROOT / "data" / "paper_portfolio.json"
OPS_LOG = _REPO_ROOT / "data" / "logs" / "operations.jsonl"
BALANCE_SUMMARY = _REPO_ROOT / "data" / "logs" / "balance_summary.json"
BOT_LOG = _REPO_ROOT / "data" / "logs" / "bot.log"


def _read_env_key(env_path: Path, key: str) -> str | None:
    """Leer una clave de .env sin python-dotenv (solo para paper-reset bare)."""
    if not env_path.is_file():
        return None
    try:
        text = env_path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        if k.strip() != key:
            continue
        v = v.strip().strip("'").strip('"')
        return v if v else None
    return None


def _paper_reset_stdlib(argv: list[str]) -> int:
    """paper-reset sin dependencias de terceros (útil si no activaste el venv)."""
    import argparse
    import os

    parser = argparse.ArgumentParser(prog="python -m scripts.run paper-reset")
    parser.add_argument("-y", "--yes", action="store_true", help="No pedir confirmación")
    parser.add_argument("--with-logs", action="store_true", help="Borrar operations.jsonl y balance_summary")
    parser.add_argument("--bot-log", action="store_true", help="Borrar bot.log")
    args = parser.parse_args(argv)

    to_remove: list[Path] = []
    if PAPER_STATE.exists():
        to_remove.append(PAPER_STATE)
    if args.with_logs:
        to_remove.extend(p for p in (OPS_LOG, BALANCE_SUMMARY) if p.exists())
    if args.bot_log and BOT_LOG.exists():
        to_remove.append(BOT_LOG)

    if not to_remove:
        print("Nothing to reset: no matching files found.")
        return 0

    if not args.yes:
        print("Delete these files?")
        for p in to_remove:
            print(f"  • {p}")
        if input("y/N: ").strip().lower() not in ("y", "yes"):
            print("Aborted.")
            return 0

    removed: list[str] = []
    for p in to_remove:
        try:
            p.unlink()
            removed.append(str(p))
        except PermissionError:
            print(
                f"Permission denied: {p}\n\n"
                "Si el setup dejó data/ con dueño betbot, ubuntu no puede borrarlo.\n"
                "Ejecutá el mismo comando con sudo, por ejemplo:\n"
                "  sudo python3 -m scripts.run paper-reset -y --with-logs --bot-log\n\n"
                "O como betbot (sin cambiar dueños):\n"
                "  sudo -u betbot rm -f data/paper_portfolio.json data/logs/operations.jsonl "
                "data/logs/balance_summary.json data/logs/bot.log"
            )
            return 1

    print("Paper reset complete. Removed:")
    for r in removed:
        print(f"  • {r}")

    if PAPER_STATE in to_remove:
        initial = os.environ.get("PAPER_INITIAL_BALANCE") or _read_env_key(
            _REPO_ROOT / ".env", "PAPER_INITIAL_BALANCE"
        ) or "100.0"
        try:
            bal = float(initial)
        except ValueError:
            bal = 100.0
        print(f"\nNext paper run will start with ${bal:.2f} cash (PAPER_INITIAL_BALANCE).")
    return 0


if __name__ == "__main__":
    if len(sys.argv) >= 2 and sys.argv[1] == "paper-reset":
        raise SystemExit(_paper_reset_stdlib(sys.argv[2:]))

import json

import click
from dotenv import load_dotenv
from loguru import logger
from rich.console import Console
from rich.panel import Panel
from rich.table import Table
from rich import box

console = Console()

# ── Logging setup ─────────────────────────────────────────────────────────────

def _setup_logging(verbose: bool) -> None:
    logger.remove()
    level = "DEBUG" if verbose else "INFO"
    logger.add(
        sys.stderr,
        format="<green>{time:HH:mm:ss}</green> | <level>{level: <8}</level> | {message}",
        level=level,
        colorize=True,
    )
    logger.add(
        str(BOT_LOG),
        rotation="10 MB",
        retention="30 days",
        level="DEBUG",
        format="{time:YYYY-MM-DD HH:mm:ss} | {level} | {message}",
    )


# ── CLI ───────────────────────────────────────────────────────────────────────

@click.group()
@click.option("--verbose", "-v", is_flag=True, help="Enable debug logging.")
@click.pass_context
def cli(ctx: click.Context, verbose: bool) -> None:
    """BetBot – Polymarket prediction market bots."""
    load_dotenv()
    _setup_logging(verbose)
    ctx.ensure_object(dict)
    ctx.obj["verbose"] = verbose


# ── weather command ───────────────────────────────────────────────────────────

@cli.command()
@click.option(
    "--mode",
    type=click.Choice(["paper", "live"], case_sensitive=False),
    default="paper",
    show_default=True,
    help="Trading mode: paper (simulation) or live (real wallet).",
)
@click.option(
    "--once",
    is_flag=True,
    default=False,
    help="Run a single scan cycle and exit (no loop).",
)
@click.option(
    "--interval",
    default=None,
    type=int,
    help="Override SCAN_INTERVAL_SECONDS from .env.",
)
def weather(mode: str, once: bool, interval: int | None) -> None:
    """Run the weather prediction market bot."""
    import os
    if interval is not None:
        os.environ["SCAN_INTERVAL_SECONDS"] = str(interval)

    from core.models import BotMode
    from bots.weather.bot import WeatherBot

    bot_mode = BotMode.PAPER if mode == "paper" else BotMode.LIVE

    if bot_mode == BotMode.LIVE:
        private_key = os.getenv("POLY_PRIVATE_KEY", "")
        if not private_key or private_key.startswith("0xyour_"):
            console.print(
                Panel(
                    "[red bold]Live mode requires POLY_PRIVATE_KEY in .env[/]\n"
                    "Set your Polygon wallet private key and Polymarket API credentials.\n"
                    "Run in [yellow]paper[/] mode first to validate the strategy.",
                    title="[red]Configuration Error[/]",
                )
            )
            sys.exit(1)

    console.print(
        Panel(
            f"[bold]Mode:[/] [cyan]{mode.upper()}[/]\n"
            f"[bold]Bot:[/]  Weather Markets\n"
            f"[bold]Scan:[/] every {os.getenv('SCAN_INTERVAL_SECONDS', '3600')}s\n"
            + ("[yellow]Run once[/] then exit." if once else "[green]Running continuously[/] (Ctrl+C to stop)."),
            title="[bold blue]BetBot – Weather[/]",
            box=box.ROUNDED,
        )
    )

    bot = WeatherBot.create(bot_mode)
    bot.run(run_once=once)


# ── status command ────────────────────────────────────────────────────────────

@cli.command()
@click.option(
    "--mode",
    type=click.Choice(["paper", "live"], case_sensitive=False),
    default="paper",
    show_default=True,
)
def status(mode: str) -> None:
    """Show current portfolio balance summary."""
    summary_path = BALANCE_SUMMARY
    if not summary_path.exists():
        console.print("[yellow]No balance summary found. Run the bot first.[/]")
        return

    with summary_path.open() as f:
        s = json.load(f)

    pnl = s.get("total_pnl_usd", 0)
    pnl_color = "green" if pnl >= 0 else "red"
    pnl_sign = "+" if pnl >= 0 else ""

    table = Table(title="Portfolio Summary", box=box.SIMPLE_HEAVY)
    table.add_column("Metric", style="bold")
    table.add_column("Value", justify="right")

    table.add_row("Mode", s.get("mode", "?").upper())
    table.add_row("Updated", s.get("updated_at", "?")[:19])
    table.add_row("Cash", f"${s.get('cash_usd', 0):.2f}")
    table.add_row("Open Positions Value", f"${s.get('open_positions_value_usd', 0):.2f}")
    table.add_row("Total Value", f"[bold]${s.get('total_value_usd', 0):.2f}[/bold]")
    table.add_row(
        "Total P&L",
        f"[{pnl_color}]{pnl_sign}${pnl:.2f}[/{pnl_color}]",
    )
    table.add_row("Realized P&L", f"${s.get('realized_pnl_usd', 0):.2f}")
    table.add_row("Unrealized P&L", f"${s.get('unrealized_pnl_usd', 0):.2f}")
    table.add_row("Peak Value", f"${s.get('peak_value_usd', 0):.2f}")
    table.add_row("Drawdown", f"{s.get('drawdown_pct', 0)*100:.1f}%")
    table.add_row("Open Positions", str(s.get("open_position_count", 0)))
    table.add_row("Total Trades", str(s.get("total_trades", 0)))
    table.add_row(
        "Win Rate",
        f"{s.get('win_rate', 0)*100:.1f}% "
        f"({s.get('winning_trades',0)}W / {s.get('losing_trades',0)}L)",
    )

    console.print(table)

    # ── Open positions detail ─────────────────────────────────────────────────
    if PAPER_STATE.exists():
        with PAPER_STATE.open() as f:
            portfolio = json.load(f)

        open_pos = [
            p for p in portfolio.get("positions", {}).values()
            if p.get("status") == "OPEN"
        ]

        if open_pos:
            # Fetch live prices from Gamma API for each open position
            import requests as _requests
            _session = _requests.Session()
            _session.headers.update({"Accept": "application/json"})

            def _fetch_live_price(condition_id: str, side: str) -> float | None:
                try:
                    resp = _session.get(
                        "https://gamma-api.polymarket.com/markets",
                        params={"condition_ids": condition_id, "limit": "1"},
                        timeout=8,
                    )
                    if resp.status_code != 200:
                        return None
                    data = resp.json()
                    if not data:
                        return None
                    raw = data[0]
                    prices = raw.get("outcomePrices", "[]")
                    if isinstance(prices, str):
                        prices = json.loads(prices)
                    prices = [float(x) for x in prices]
                    return prices[0] if side == "YES" else prices[1]
                except Exception:
                    return None

            console.print("[dim]Obteniendo precios actuales del mercado...[/dim]")

            pos_table = Table(
                title=f"Open Positions ({len(open_pos)}) — precios en tiempo real",
                box=box.SIMPLE,
            )
            pos_table.add_column("#",          width=3)
            pos_table.add_column("Side",       width=5)
            pos_table.add_column("Abierta",    width=16)
            pos_table.add_column("Cierra",     width=11)
            pos_table.add_column("Entrada $",  justify="right", width=10)
            pos_table.add_column("Shares",     justify="right", width=10)
            pos_table.add_column("P. entrada", justify="right", width=10)
            pos_table.add_column("P. actual",  justify="right", width=10)
            pos_table.add_column("P&L ahora",  justify="right", width=11)
            pos_table.add_column("Edge",       justify="right", width=7)
            pos_table.add_column("Pregunta",   overflow="fold")

            FEE = 0.02
            total_live_pnl = 0.0
            for i, p in enumerate(open_pos, 1):
                entry_amt   = p.get("entry_amount_usd", 0)
                entry_price = p.get("entry_price", 0)
                shares      = p.get("shares", 0)
                side        = p.get("side", "?")
                edge        = p.get("entry_edge", 0)
                cid         = p.get("condition_id", "")

                live_price = _fetch_live_price(cid, side)
                if live_price is not None:
                    gross      = shares * live_price
                    net        = gross * (1 - FEE)
                    pnl        = net - entry_amt
                    price_str  = f"{live_price:.4f}"
                    live_label = ""
                else:
                    # fallback to entry price
                    gross = shares * entry_price
                    net   = gross * (1 - FEE)
                    pnl   = net - entry_amt
                    price_str  = f"{entry_price:.4f}"
                    live_label = "[dim](entrada)[/dim]"

                total_live_pnl += pnl
                pnl_color = "green" if pnl >= 0 else "red"
                pnl_sign  = "+" if pnl >= 0 else ""

                opened_raw = p.get("opened_at", "")
                opened_str = opened_raw[:16].replace("T", " ") if opened_raw else "?"
                end_raw    = p.get("market_end_date", "")
                end_str    = end_raw[:10] if end_raw else "?"

                pos_table.add_row(
                    str(i),
                    side,
                    opened_str,
                    end_str,
                    f"${entry_amt:.2f}",
                    f"{shares:.2f}",
                    f"{entry_price:.4f}",
                    f"{price_str} {live_label}",
                    f"[{pnl_color}]{pnl_sign}${pnl:.2f}[/{pnl_color}]",
                    f"{edge*100:.1f}%",
                    p.get("question", ""),
                )

            console.print(pos_table)
            sign = "+" if total_live_pnl >= 0 else ""
            color = "green" if total_live_pnl >= 0 else "red"
            console.print(
                f"  P&L total si cierras todo ahora: [{color}]{sign}${total_live_pnl:.2f}[/{color}]"
            )


# ── operations command ────────────────────────────────────────────────────────

@cli.command()
@click.option("--tail", "-n", default=20, help="Number of last operations to show.")
@click.option("--filter", "event_filter", default=None, help="Filter by event type (e.g. trade, position_open).")
def operations(tail: int, event_filter: str | None) -> None:
    """Show recent operations log."""
    ops_path = OPS_LOG
    if not ops_path.exists():
        console.print("[yellow]No operations log found. Run the bot first.[/]")
        return

    lines = ops_path.read_text().strip().split("\n")
    records = []
    for line in lines:
        try:
            r = json.loads(line)
            if event_filter and r.get("event") != event_filter:
                continue
            records.append(r)
        except json.JSONDecodeError:
            continue

    records = records[-tail:]

    table = Table(title=f"Last {len(records)} Operations", box=box.SIMPLE)
    table.add_column("Time", style="dim", width=19)
    table.add_column("Event", width=16)
    table.add_column("Side", width=6)
    table.add_column("Amount", justify="right", width=10)
    table.add_column("P&L", justify="right", width=10)
    table.add_column("Question / Notes", overflow="fold")

    for r in records:
        event = r.get("event", "")
        ts = r.get("timestamp", "")[:19]
        side = r.get("side", "")
        amount = f"${r.get('amount_usd', r.get('entry_amount_usd', 0)):.2f}" if r.get("amount_usd") or r.get("entry_amount_usd") else ""
        pnl = r.get("pnl_usd")
        pnl_str = ""
        if pnl is not None:
            sign = "+" if pnl >= 0 else ""
            color = "green" if pnl >= 0 else "red"
            pnl_str = f"[{color}]{sign}${pnl:.2f}[/{color}]"
        question = r.get("question", r.get("notes", r.get("reason", "")))[:60]

        table.add_row(ts, event, side, amount, pnl_str, question)

    console.print(table)


# ── paper-reset command ───────────────────────────────────────────────────────


@cli.command("paper-reset")
@click.option(
    "--yes",
    "-y",
    is_flag=True,
    help="Skip confirmation (useful for scripts).",
)
@click.option(
    "--with-logs",
    is_flag=True,
    help="Also delete data/logs/operations.jsonl and balance_summary.json.",
)
@click.option(
    "--bot-log",
    is_flag=True,
    help="Also delete data/logs/bot.log.",
)
def paper_reset(yes: bool, with_logs: bool, bot_log: bool) -> None:
    """Remove paper portfolio file so the next run starts with a fresh virtual balance."""
    import os

    to_remove: list[Path] = []
    if PAPER_STATE.exists():
        to_remove.append(PAPER_STATE)
    if with_logs:
        to_remove.extend(p for p in (OPS_LOG, BALANCE_SUMMARY) if p.exists())
    if bot_log and BOT_LOG.exists():
        to_remove.append(BOT_LOG)

    if not to_remove:
        console.print("[yellow]Nothing to reset: no matching files found.[/]")
        return

    if not yes:
        preview = "\n".join(f"  • {p}" for p in to_remove)
        if not click.confirm(f"Delete these files?\n{preview}"):
            console.print("Aborted.")
            raise SystemExit(0)

    removed: list[str] = []
    for p in to_remove:
        try:
            p.unlink()
            removed.append(str(p))
        except PermissionError:
            console.print(
                "[red]Permission denied[/] al borrar "
                f"[dim]{p}[/]. Probá con [bold]sudo[/] o borrá como [bold]betbot[/]:\n"
                "[dim]sudo python3 -m scripts.run paper-reset -y --with-logs --bot-log[/]"
            )
            raise SystemExit(1)

    initial = os.getenv("PAPER_INITIAL_BALANCE", "100.0")
    tail = ""
    if PAPER_STATE in to_remove:
        tail = (
            f"\n\nNext [cyan]paper[/] run will start with [bold]${float(initial):.2f}[/] cash "
            f"([dim]PAPER_INITIAL_BALANCE[/])."
        )
    console.print(
        Panel(
            f"[green]Paper reset complete.[/]\n\n"
            f"Removed:\n"
            + "\n".join(f"  • [dim]{r}[/]" for r in removed)
            + tail,
            title="[bold]paper-reset[/]",
            box=box.ROUNDED,
        )
    )


# ── Entry point ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    cli()
