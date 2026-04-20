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
PAPER_STATES = {
    "weather": _REPO_ROOT / "data" / "weather_paper_portfolio.json",
    "crypto":  _REPO_ROOT / "data" / "crypto_paper_portfolio.json",
}
BALANCE_SUMMARIES = {
    "weather": _REPO_ROOT / "data" / "logs" / "weather_balance_summary.json",
    "crypto":  _REPO_ROOT / "data" / "logs" / "crypto_balance_summary.json",
}
# Legacy paths (kept for paper-reset --with-logs)
OPS_LOG = _REPO_ROOT / "data" / "logs" / "weather_operations.jsonl"
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
    parser.add_argument("--bot", default="all", choices=["weather", "crypto", "all"])
    parser.add_argument("--with-logs", action="store_true", help="Borrar operations.jsonl y balance_summary")
    parser.add_argument("--bot-log", action="store_true", help="Borrar bot.log")
    args = parser.parse_args(argv)

    bots = ["weather", "crypto"] if args.bot == "all" else [args.bot]

    to_remove: list[Path] = []
    for b in bots:
        p = PAPER_STATES[b]
        if p.exists():
            to_remove.append(p)
        if args.with_logs:
            summary = BALANCE_SUMMARIES[b]
            if summary.exists():
                to_remove.append(summary)
            ops = _REPO_ROOT / "data" / "logs" / f"{b}_operations.jsonl"
            if ops.exists():
                to_remove.append(ops)
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
            print(f"Permission denied: {p}\nTry: sudo python3 -m scripts.run paper-reset -y")
            return 1

    print("Paper reset complete. Removed:")
    for r in removed:
        print(f"  • {r}")
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

from core.datetime_display import format_utc_datetime_short

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


# ── crypto command ────────────────────────────────────────────────────────────

@cli.command()
@click.option(
    "--mode",
    type=click.Choice(["paper", "live"], case_sensitive=False),
    default="paper",
    show_default=True,
    help="Trading mode: paper (simulation) or live (real wallet).",
)
@click.option("--once", is_flag=True, default=False, help="Run a single scan cycle and exit.")
@click.option("--interval", default=None, type=int, help="Override CRYPTO_SCAN_INTERVAL_SECONDS.")
def crypto(mode: str, once: bool, interval: int | None) -> None:
    """Run the crypto price prediction market bot."""
    import os
    if interval is not None:
        os.environ["CRYPTO_SCAN_INTERVAL_SECONDS"] = str(interval)

    from core.models import BotMode
    from bots.crypto.bot import CryptoBot

    bot_mode = BotMode.PAPER if mode == "paper" else BotMode.LIVE

    if bot_mode == BotMode.LIVE:
        private_key = os.getenv("POLY_PRIVATE_KEY", "")
        if not private_key or private_key.startswith("0xyour_"):
            console.print(
                Panel(
                    "[red bold]Live mode requires POLY_PRIVATE_KEY in .env[/]\n"
                    "Run in [yellow]paper[/] mode first to validate the strategy.",
                    title="[red]Configuration Error[/]",
                )
            )
            sys.exit(1)

    console.print(
        Panel(
            f"[bold]Mode:[/] [cyan]{mode.upper()}[/]\n"
            f"[bold]Bot:[/]  Crypto Price Markets (BTC/ETH)\n"
            f"[bold]Scan:[/] every {os.getenv('CRYPTO_SCAN_INTERVAL_SECONDS', '300')}s\n"
            + ("[yellow]Run once[/] then exit." if once else "[green]Running continuously[/] (Ctrl+C to stop)."),
            title="[bold blue]BetBot – Crypto[/]",
            box=box.ROUNDED,
        )
    )

    bot = CryptoBot.create(bot_mode)
    bot.run(run_once=once)


# ── status command ────────────────────────────────────────────────────────────

def _show_bot_status(bot_name: str) -> None:
    summary_path = BALANCE_SUMMARIES[bot_name]
    if not summary_path.exists():
        console.print(f"[yellow]No balance summary for {bot_name} bot. Run it first.[/]")
        return

    with summary_path.open() as f:
        s = json.load(f)

    pnl = s.get("total_pnl_usd", 0)
    pnl_color = "green" if pnl >= 0 else "red"
    pnl_sign = "+" if pnl >= 0 else ""

    table = Table(title=f"{bot_name.capitalize()} Portfolio Summary", box=box.SIMPLE_HEAVY)
    table.add_column("Metric", style="bold")
    table.add_column("Value", justify="right")

    table.add_row("Mode", s.get("mode", "?").upper())
    table.add_row("Updated", s.get("updated_at", "?")[:19])
    table.add_row("Cash", f"${s.get('cash_usd', 0):.2f}")
    table.add_row("Open Positions Value", f"${s.get('open_positions_value_usd', 0):.2f}")
    table.add_row("Total Value", f"[bold]${s.get('total_value_usd', 0):.2f}[/bold]")
    table.add_row("Total P&L", f"[{pnl_color}]{pnl_sign}${pnl:.2f}[/{pnl_color}]")
    table.add_row("Realized P&L", f"${s.get('realized_pnl_usd', 0):.2f}")
    table.add_row("Unrealized P&L", f"${s.get('unrealized_pnl_usd', 0):.2f}")
    table.add_row("Peak Value", f"${s.get('peak_value_usd', 0):.2f}")
    table.add_row("Drawdown", f"{s.get('drawdown_pct', 0)*100:.1f}%")
    table.add_row("Open Positions", str(s.get("open_position_count", 0)))
    table.add_row("Total Trades", str(s.get("total_trades", 0)))
    table.add_row(
        "Win Rate",
        f"{s.get('win_rate', 0)*100:.1f}% "
        f"({s.get('winning_trades', 0)}W / {s.get('losing_trades', 0)}L)",
    )
    console.print(table)

    paper_state = PAPER_STATES[bot_name]
    if paper_state.exists():
        with paper_state.open() as f:
            portfolio = json.load(f)

        open_pos = [
            p for p in portfolio.get("positions", {}).values()
            if p.get("status") == "OPEN"
        ]

        if open_pos:
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
            pos_table.add_column("Abierta (UTC)", width=22)
            pos_table.add_column("Fin (UTC)",  width=22)
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
                    gross = shares * entry_price
                    net   = gross * (1 - FEE)
                    pnl   = net - entry_amt
                    price_str  = f"{entry_price:.4f}"
                    live_label = "[dim](entrada)[/dim]"

                total_live_pnl += pnl
                pnl_color = "green" if pnl >= 0 else "red"
                pnl_sign  = "+" if pnl >= 0 else ""

                opened_str = format_utc_datetime_short(p.get("opened_at"))
                end_str = format_utc_datetime_short(p.get("market_end_date"))

                pos_table.add_row(
                    str(i), side, opened_str, end_str,
                    f"${entry_amt:.2f}", f"{shares:.2f}", f"{entry_price:.4f}",
                    f"{price_str} {live_label}",
                    f"[{pnl_color}]{pnl_sign}${pnl:.2f}[/{pnl_color}]",
                    f"{edge*100:.1f}%",
                    p.get("question", ""),
                )

            console.print(pos_table)
            console.print(
                "[dim]Abierta / Fin = hora UTC del bot y de la API. "
                "La pregunta del mercado suele mostrar ventanas en hora ET (Polymarket).[/dim]"
            )
            sign = "+" if total_live_pnl >= 0 else ""
            color = "green" if total_live_pnl >= 0 else "red"
            console.print(
                f"  P&L total si cierras todo ahora: [{color}]{sign}${total_live_pnl:.2f}[/{color}]"
            )


@cli.command()
@click.option(
    "--bot",
    type=click.Choice(["weather", "crypto", "all"], case_sensitive=False),
    default="all",
    show_default=True,
)
def status(bot: str) -> None:
    """Show current portfolio balance summary.

    Examples:

    \b
      python -m scripts.run status                   # both bots
      python -m scripts.run status --bot weather     # weather only
      python -m scripts.run status --bot crypto      # crypto only
    """
    bots_to_show = ["weather", "crypto"] if bot == "all" else [bot]
    for b in bots_to_show:
        _show_bot_status(b)


# ── operations command ────────────────────────────────────────────────────────

@cli.command()
@click.option("--tail", "-n", default=20, help="Number of last operations to show.")
@click.option("--filter", "event_filter", default=None, help="Filter by event type (e.g. trade, position_open).")
@click.option(
    "--bot",
    type=click.Choice(["weather", "crypto"], case_sensitive=False),
    default="weather",
    show_default=True,
)
def operations(tail: int, event_filter: str | None, bot: str) -> None:
    """Show recent operations log."""
    ops_path = _REPO_ROOT / "data" / "logs" / f"{bot}_operations.jsonl"
    if not ops_path.exists():
        console.print(f"[yellow]No operations log found for {bot}. Run the bot first.[/]")
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


# ── calibration command ───────────────────────────────────────────────────────


def _load_ops_records(bot_name: str) -> list[dict]:
    path = _REPO_ROOT / "data" / "logs" / f"{bot_name}_operations.jsonl"
    if not path.exists():
        return []
    records: list[dict] = []
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            records.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return records


@cli.command()
@click.option(
    "--bot",
    type=click.Choice(["weather", "crypto"], case_sensitive=False),
    default="weather",
    show_default=True,
)
@click.option("--buckets", default=5, help="Cantidad de buckets de probabilidad.")
def calibration(bot: str, buckets: int) -> None:
    """Reporte de calibración: predicted_true_prob vs actual hit rate.

    Agrupa posiciones cerradas por bucket de probabilidad estimada al entrar y
    compara con la fracción que terminaron ganadoras. Una calibración perfecta
    tiene predicted ≈ actual en cada bucket.

    Casos a interpretar:
      • actual << predicted → modelo overconfident → bajar KELLY_FRACTION o subir MIN_EDGE
      • actual >> predicted → modelo underconfident → subir KELLY_FRACTION
      • spread grande dentro de un bucket → forecast no es fuente de edge
    """
    records = _load_ops_records(bot)
    if not records:
        console.print(f"[yellow]No operations log found for {bot}.[/]")
        return

    # Emparejar position_open → position_close usando (condition_id, side)
    # (asumiendo 1 posición abierta por side+condition a la vez)
    opens: dict[tuple, dict] = {}
    pairs: list[tuple[dict, dict]] = []
    for r in records:
        event = r.get("event", "")
        side = r.get("side", "")
        cid = r.get("condition_id", "")
        if not cid or not side:
            continue
        key = (cid, side)
        if event == "position_open":
            opens[key] = r
        elif event == "position_close":
            op = opens.pop(key, None)
            if op is not None:
                pairs.append((op, r))

    if not pairs:
        console.print(f"[yellow]No closed position pairs found for {bot}.[/]")
        return

    # Para cada par, la "prob de acierto" que el modelo asignó al side apostado:
    #   side=YES → entry_true_prob
    #   side=NO  → 1 - entry_true_prob
    # "win" = pnl_usd > 0 (cerrada con ganancia)
    buckets_data: list[list[tuple[float, bool, float]]] = [[] for _ in range(buckets)]
    overall_wins = 0
    overall_total = 0
    overall_pnl = 0.0

    for op, cl in pairs:
        side = op.get("side", "")
        true_prob = op.get("true_prob")
        if true_prob is None:
            continue
        side_prob = true_prob if side == "YES" else 1.0 - true_prob
        pnl = cl.get("pnl_usd", 0.0)
        is_win = pnl > 0
        bucket_idx = min(buckets - 1, int(side_prob * buckets))
        buckets_data[bucket_idx].append((side_prob, is_win, pnl))
        if is_win:
            overall_wins += 1
        overall_total += 1
        overall_pnl += pnl

    table = Table(
        title=f"Calibration Report — {bot} ({len(pairs)} closed positions)",
        box=box.SIMPLE_HEAVY,
    )
    table.add_column("Bucket (predicted)", width=18)
    table.add_column("N",  justify="right", width=4)
    table.add_column("Avg predicted", justify="right", width=14)
    table.add_column("Actual win rate", justify="right", width=15)
    table.add_column("Bias",           justify="right", width=9)
    table.add_column("Sum P&L",        justify="right", width=10)

    for i in range(buckets):
        low = i / buckets
        high = (i + 1) / buckets
        data = buckets_data[i]
        if not data:
            table.add_row(f"{low:.0%} – {high:.0%}", "0", "—", "—", "—", "—")
            continue
        n = len(data)
        avg_pred = sum(d[0] for d in data) / n
        wins = sum(1 for d in data if d[1])
        actual = wins / n
        bias = actual - avg_pred
        bias_str = f"{bias:+.1%}"
        bias_color = "green" if bias >= 0 else "red"
        pnl_sum = sum(d[2] for d in data)
        pnl_sign = "+" if pnl_sum >= 0 else ""
        pnl_color = "green" if pnl_sum >= 0 else "red"
        table.add_row(
            f"{low:.0%} – {high:.0%}",
            str(n),
            f"{avg_pred:.1%}",
            f"{actual:.1%} ({wins}/{n})",
            f"[{bias_color}]{bias_str}[/{bias_color}]",
            f"[{pnl_color}]{pnl_sign}${pnl_sum:.2f}[/{pnl_color}]",
        )

    console.print(table)

    if overall_total > 0:
        overall_wr = overall_wins / overall_total
        pnl_sign = "+" if overall_pnl >= 0 else ""
        pnl_color = "green" if overall_pnl >= 0 else "red"
        console.print(
            f"\n  Overall: {overall_wins}/{overall_total} win rate "
            f"[bold]{overall_wr:.1%}[/]  |  total P&L: "
            f"[{pnl_color}]{pnl_sign}${overall_pnl:.2f}[/{pnl_color}]"
        )
        console.print(
            "  [dim]Bias positivo = predicciones conservadoras (real > predicho). "
            "Bias negativo = modelo overconfident (real < predicho).[/dim]"
        )


# ── paper-reset command ───────────────────────────────────────────────────────


@cli.command("paper-reset")
@click.option("-y", "--yes", is_flag=True, help="Skip confirmation.")
@click.option(
    "--bot",
    type=click.Choice(["weather", "crypto", "all"], case_sensitive=False),
    default="all",
    show_default=True,
    help="Which bot's portfolio to reset.",
)
@click.option("--with-logs", is_flag=True, help="Also delete operations and balance_summary files.")
@click.option("--bot-log", is_flag=True, help="Also delete data/logs/bot.log.")
def paper_reset(yes: bool, bot: str, with_logs: bool, bot_log: bool) -> None:
    """Remove paper portfolio file(s) so the next run starts with a fresh virtual balance."""
    bots_to_reset = ["weather", "crypto"] if bot == "all" else [bot]

    to_remove: list[Path] = []
    for b in bots_to_reset:
        p = PAPER_STATES[b]
        if p.exists():
            to_remove.append(p)
        if with_logs:
            summary = BALANCE_SUMMARIES[b]
            if summary.exists():
                to_remove.append(summary)
            ops = _REPO_ROOT / "data" / "logs" / f"{b}_operations.jsonl"
            if ops.exists():
                to_remove.append(ops)
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
                f"[dim]{p}[/]. Probá con [bold]sudo[/]:\n"
                "[dim]sudo python3 -m scripts.run paper-reset -y[/]"
            )
            raise SystemExit(1)

    console.print(
        Panel(
            f"[green]Paper reset complete.[/]\n\n"
            f"Removed:\n"
            + "\n".join(f"  • [dim]{r}[/]" for r in removed),
            title="[bold]paper-reset[/]",
            box=box.ROUNDED,
        )
    )


# ── Entry point ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    cli()
