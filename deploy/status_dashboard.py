#!/usr/bin/env python3
"""
Panel web mínimo con el mismo contenido que `python -m scripts.run status`
(resúmenes + posiciones abiertas con precio vía Gamma API).

Solo biblioteca estándar. Uso local:

  python deploy/status_dashboard.py --host 127.0.0.1 --port 8765

En EC2 suele ir detrás de nginx (ver deploy/nginx-status-web.conf.example).
"""
from __future__ import annotations

import argparse
import html
import json
import sys
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

FEE = 0.02
GAMMA_MARKETS = "https://gamma-api.polymarket.com/markets"


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[1]


def _fetch_live_price(condition_id: str, side: str) -> tuple[float | None, bool]:
    """Returns (price, is_live). If request fails, returns (entry fallback handled by caller)."""
    params = urllib.parse.urlencode({"condition_ids": condition_id, "limit": "1"})
    url = f"{GAMMA_MARKETS}?{params}"
    req = urllib.request.Request(url, headers={"Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=12) as resp:
            data = json.loads(resp.read().decode())
    except (urllib.error.URLError, json.JSONDecodeError, TimeoutError, OSError):
        return None, False
    if not data:
        return None, False
    raw = data[0]
    prices = raw.get("outcomePrices", "[]")
    if isinstance(prices, str):
        try:
            prices = json.loads(prices)
        except json.JSONDecodeError:
            return None, False
    try:
        prices_f = [float(x) for x in prices]
    except (TypeError, ValueError):
        return None, False
    if len(prices_f) < 2:
        return None, False
    p = prices_f[0] if side == "YES" else prices_f[1]
    return p, True


def _summary_section(bot_name: str, repo: Path) -> str:
    summary_path = repo / "data" / "logs" / f"{bot_name}_balance_summary.json"
    if not summary_path.is_file():
        return (
            f'<section class="bot"><h2>{html.escape(bot_name)}</h2>'
            '<p class="warn">No hay balance summary todavía. Ejecutá el bot al menos una vez.</p></section>'
        )

    with summary_path.open(encoding="utf-8") as f:
        s = json.load(f)

    pnl = float(s.get("total_pnl_usd", 0))
    pnl_cls = "pos" if pnl >= 0 else "neg"
    pnl_sign = "+" if pnl >= 0 else ""

    rows = [
        ("Modo", str(s.get("mode", "?")).upper()),
        ("Actualizado", html.escape(str(s.get("updated_at", "?"))[:19])),
        ("Cash", f"${float(s.get('cash_usd', 0)):.2f}"),
        ("Valor posiciones abiertas", f"${float(s.get('open_positions_value_usd', 0)):.2f}"),
        ("Valor total", f"${float(s.get('total_value_usd', 0)):.2f}"),
        ("P&amp;L total", f'<span class="{pnl_cls}">{pnl_sign}${pnl:.2f}</span>'),
        ("P&amp;L realizado", f"${float(s.get('realized_pnl_usd', 0)):.2f}"),
        ("P&amp;L no realizado", f"${float(s.get('unrealized_pnl_usd', 0)):.2f}"),
        ("Peak", f"${float(s.get('peak_value_usd', 0)):.2f}"),
        ("Drawdown", f"{float(s.get('drawdown_pct', 0)) * 100:.1f}%"),
        ("Posiciones abiertas", str(s.get("open_position_count", 0))),
        ("Trades totales", str(s.get("total_trades", 0))),
        (
            "Win rate",
            f"{float(s.get('win_rate', 0)) * 100:.1f}% "
            f"({s.get('winning_trades', 0)}W / {s.get('losing_trades', 0)}L)",
        ),
    ]

    table_rows = "".join(
        f"<tr><th>{html.escape(k)}</th><td>{v}</td></tr>" for k, v in rows
    )

    paper_path = repo / "data" / f"{bot_name}_paper_portfolio.json"
    positions_html = ""
    if paper_path.is_file():
        with paper_path.open(encoding="utf-8") as f:
            portfolio = json.load(f)
        open_pos = [
            p for p in portfolio.get("positions", {}).values() if p.get("status") == "OPEN"
        ]
        if open_pos:
            head = (
                "<thead><tr>"
                "<th>#</th><th>Side</th><th>Abierta</th><th>Cierra</th>"
                "<th>Entrada $</th><th>Shares</th><th>P. entrada</th><th>P. actual</th>"
                "<th>P&amp;L</th><th>Edge</th><th>Pregunta</th>"
                "</tr></thead>"
            )
            body_rows: list[str] = []
            total_live_pnl = 0.0
            for i, p in enumerate(open_pos, 1):
                entry_amt = float(p.get("entry_amount_usd", 0))
                entry_price = float(p.get("entry_price", 0))
                shares = float(p.get("shares", 0))
                side = str(p.get("side", "?"))
                edge = float(p.get("entry_edge", 0))
                cid = str(p.get("condition_id", ""))

                live_price, _is_live = _fetch_live_price(cid, side) if cid else (None, False)
                if live_price is not None:
                    net = shares * live_price * (1 - FEE)
                    pnl_pos = net - entry_amt
                    price_str = f"{live_price:.4f}"
                    live_note = ""
                else:
                    net = shares * entry_price * (1 - FEE)
                    pnl_pos = net - entry_amt
                    price_str = f"{entry_price:.4f}"
                    live_note = " (entrada)"

                total_live_pnl += pnl_pos
                pnl_c = "pos" if pnl_pos >= 0 else "neg"
                ps = "+" if pnl_pos >= 0 else ""

                opened_raw = str(p.get("opened_at", ""))
                opened_str = opened_raw[:16].replace("T", " ") if opened_raw else "?"
                end_raw = str(p.get("market_end_date", ""))
                end_str = end_raw[:10] if end_raw else "?"

                q = html.escape(str(p.get("question", ""))[:200])
                body_rows.append(
                    "<tr>"
                    f"<td>{i}</td>"
                    f"<td>{html.escape(side)}</td>"
                    f"<td>{html.escape(opened_str)}</td>"
                    f"<td>{html.escape(end_str)}</td>"
                    f"<td>${entry_amt:.2f}</td>"
                    f"<td>{shares:.2f}</td>"
                    f"<td>{entry_price:.4f}</td>"
                    f"<td>{price_str}{html.escape(live_note)}</td>"
                    f'<td class="{pnl_c}">{ps}${pnl_pos:.2f}</td>'
                    f"<td>{edge * 100:.1f}%</td>"
                    f"<td class=\"q\">{q}</td>"
                    "</tr>"
                )

            sign = "+" if total_live_pnl >= 0 else ""
            tc = "pos" if total_live_pnl >= 0 else "neg"
            positions_html = (
                f'<h3>Posiciones abiertas ({len(open_pos)})</h3>'
                f'<div class="table-wrap"><table class="positions">{head}<tbody>'
                + "".join(body_rows)
                + "</tbody></table></div>"
                f'<p class="footer-pnl">P&amp;L total si cerrás todo ahora: '
                f'<span class="{tc}">{sign}${total_live_pnl:.2f}</span></p>'
            )

    title = f"{bot_name.capitalize()} — portfolio"
    return (
        f'<section class="bot"><h2>{html.escape(title)}</h2>'
        f'<table class="summary">{table_rows}</table>'
        f"{positions_html}</section>"
    )


def build_html(repo: Path) -> str:
    weather = _summary_section("weather", repo)
    crypto = _summary_section("crypto", repo)
    return f"""<!DOCTYPE html>
<html lang="es">
<head>
  <meta charset="utf-8"/>
  <meta name="viewport" content="width=device-width, initial-scale=1"/>
  <meta http-equiv="refresh" content="90"/>
  <title>BetBot — balance</title>
  <style>
    :root {{
      --bg: #0f1419;
      --card: #1a2332;
      --text: #e7ecf3;
      --muted: #8b9cb3;
      --border: #2d3a4d;
      --pos: #3ecf8e;
      --neg: #f07178;
      --warn: #e7c547;
    }}
    body {{
      font-family: system-ui, -apple-system, Segoe UI, sans-serif;
      background: var(--bg);
      color: var(--text);
      margin: 0;
      padding: 1rem 1.25rem 2rem;
      line-height: 1.45;
    }}
    .page-head {{
      display: flex; flex-wrap: wrap; align-items: center; justify-content: space-between;
      gap: 0.75rem 1rem; margin-bottom: 0.35rem;
    }}
    h1 {{ font-size: 1.25rem; font-weight: 600; margin: 0; }}
    button.btn-reload {{
      font: inherit; font-size: 0.9rem; font-weight: 600;
      color: var(--text); background: #243044; border: 1px solid var(--border);
      border-radius: 8px; padding: 0.45rem 1rem; cursor: pointer;
    }}
    button.btn-reload:hover {{ background: #2d3f56; border-color: #3d5270; }}
    button.btn-reload:active {{ transform: scale(0.98); }}
    .sub {{ color: var(--muted); font-size: 0.85rem; margin-bottom: 1.5rem; }}
    section.bot {{
      background: var(--card);
      border: 1px solid var(--border);
      border-radius: 10px;
      padding: 1rem 1.1rem;
      margin-bottom: 1.25rem;
    }}
    h2 {{ font-size: 1.05rem; margin: 0 0 0.75rem; }}
    h3 {{ font-size: 0.95rem; margin: 1rem 0 0.5rem; color: var(--muted); }}
    table.summary {{ width: 100%; border-collapse: collapse; font-size: 0.92rem; }}
    table.summary th {{
      text-align: left; color: var(--muted); font-weight: 500;
      padding: 0.35rem 0.75rem 0.35rem 0; width: 42%; vertical-align: top;
    }}
    table.summary td {{ padding: 0.35rem 0; }}
    .table-wrap {{ overflow-x: auto; margin-top: 0.5rem; }}
    table.positions {{
      width: 100%; border-collapse: collapse; font-size: 0.78rem;
    }}
    table.positions th, table.positions td {{
      border: 1px solid var(--border); padding: 0.4rem 0.45rem; vertical-align: top;
    }}
    table.positions th {{ background: #131c28; color: var(--muted); font-weight: 600; }}
    td.q {{ max-width: 14rem; word-break: break-word; }}
    .pos {{ color: var(--pos); font-weight: 600; }}
    .neg {{ color: var(--neg); font-weight: 600; }}
    .warn {{ color: var(--warn); }}
    .footer-pnl {{ margin-top: 0.75rem; font-size: 0.9rem; }}
  </style>
</head>
<body>
  <header class="page-head">
    <h1>BetBot — balance en vivo</h1>
    <button type="button" class="btn-reload" onclick="location.reload()" aria-label="Actualizar datos">Actualizar</button>
  </header>
  <p class="sub">Mismo origen de datos que <code>./deploy/manage.sh balance</code>. Podés recargar cuando quieras; también se actualiza sola cada 90s.</p>
  {weather}
  {crypto}
</body>
</html>
"""


class _Handler(BaseHTTPRequestHandler):
    repo_root: Path = _repo_root()

    def log_message(self, format: str, *args) -> None:
        sys.stderr.write("%s - - [%s] %s\n" % (self.address_string(), self.log_date_time_string(), format % args))

    def do_GET(self) -> None:
        if self.path.split("?", 1)[0] not in ("/", "/index.html"):
            self.send_error(404, "Not Found")
            return
        try:
            body = build_html(self.repo_root).encode("utf-8")
        except Exception as exc:
            self.send_response(500)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.end_headers()
            self.wfile.write(f"Error generando página: {exc}".encode())
            return
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)


def main() -> int:
    parser = argparse.ArgumentParser(description="Panel HTTP de balance BetBot (stdlib).")
    parser.add_argument("--host", default="127.0.0.1", help="Interfaz (127.0.0.1 solo local; 0.0.0.0 para red)")
    parser.add_argument("--port", type=int, default=8765, help="Puerto TCP (default 8765)")
    args = parser.parse_args()

    _Handler.repo_root = _repo_root()
    server = ThreadingHTTPServer((args.host, args.port), _Handler)
    print(f"BetBot status dashboard → http://{args.host}:{args.port}/", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nDetenido.", flush=True)
        return 0
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
