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
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

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
        "data/logs/bot.log",
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
    summary_path = Path("data/logs/balance_summary.json")
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


# ── operations command ────────────────────────────────────────────────────────

@cli.command()
@click.option("--tail", "-n", default=20, help="Number of last operations to show.")
@click.option("--filter", "event_filter", default=None, help="Filter by event type (e.g. trade, position_open).")
def operations(tail: int, event_filter: str | None) -> None:
    """Show recent operations log."""
    ops_path = Path("data/logs/operations.jsonl")
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


# ── Entry point ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    cli()
