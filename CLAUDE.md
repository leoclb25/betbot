# BetBot – Claude Code Context

Este archivo le da contexto a Claude Code sobre el proyecto para que las conversaciones
futuras sean más eficientes y consistentes.

---

## ¿Qué es este proyecto?

Framework de bots de trading para **Polymarket** (mercados de predicción).
El primer bot (`bots/weather/`) apuesta en mercados de clima usando datos del
ensemble de Open-Meteo (50 modelos gratuitos).

Diseñado para escalar: cada nuevo bot vive en `bots/<nombre>/` y extiende `BaseBot`.

---

## Stack técnico

| Capa | Tecnología |
|---|---|
| Lenguaje | Python 3.11+ |
| Modelos de datos | Pydantic v2 (frozen, computed_field) |
| HTTP | `requests` (sync – no async intencional) |
| CLI | `click` + `rich` |
| Logs | `loguru` |
| Scheduling | `apscheduler` / loop nativo con `time.sleep` |
| Polymarket data | Gamma API pública (`gamma-api.polymarket.com`) |
| Polymarket trading | `py-clob-client` (CLOB API) |
| Clima | Open-Meteo Ensemble API (gratis, sin key) |

---

## Arquitectura en capas

```
scripts/run.py          ← CLI entry point (click)
    ↓
bots/<nombre>/bot.py    ← Orquestador del bot (extiende BaseBot)
    ↓ usa
bots/<nombre>/strategy.py  ← Lógica de entrada/salida
bots/<nombre>/parser.py    ← Parsea preguntas del mercado → struct
    ↓ usa
core/weather/client.py     ← Datos de clima (Open-Meteo)
core/risk/manager.py       ← Kelly, sizing, stop-loss, pausas
core/polymarket/client.py  ← API live (o paper_client.py)
core/portfolio/tracker.py  ← Estado del portfolio
core/portfolio/logger.py   ← Escribe operations.jsonl y balance_summary.json
core/models.py             ← Todos los tipos de datos compartidos
```

---

## Archivos críticos para leer antes de modificar

1. [core/models.py](core/models.py) – todos los tipos; cambiar aquí puede romper todo
2. [bots/base.py](bots/base.py) – interfaz que deben implementar todos los bots
3. [core/risk/manager.py](core/risk/manager.py) – lógica de Kelly y controles de riesgo
4. [core/polymarket/paper_client.py](core/polymarket/paper_client.py) – persiste estado en `data/paper_portfolio.json`

---

## Convenciones del proyecto

- **Sync over async**: el bot corre en un loop con `time.sleep`. No usar `asyncio`.
- **Pydantic v2**: usar `model_copy(update={...})` en vez de `.copy()`. Los modelos son inmutables.
- **Logging**: siempre con `loguru` (`from loguru import logger`). No usar `print()`.
- **Config**: variables de entorno via `python-dotenv`. Defaults en `config/settings.yaml`.
  Nunca hardcodear valores de config en el código.
- **Fees**: siempre considerar `POLYMARKET_FEE_RATE = 0.02` en cálculos de edge.
- **Paper vs Live**: el código nunca debe distinguir entre modos por si mismo; el cliente
  (`PaperClient` vs `PolymarketClient`) encapsula la diferencia.
- **No añadir abstracciones especulativas**: si algo se usa una sola vez, no abstraerlo.

---

## Cómo agregar un nuevo bot

1. Crear `bots/<nombre>/` con `__init__.py`, `parser.py`, `strategy.py`, `bot.py`
2. `bot.py` debe extender `BaseBot` e implementar:
   - `scan_markets() → list[Market]`
   - `evaluate_market(market) → BotSignal`
   - `manage_open_positions() → None`
   - `_execute_entry(signal) → None`
3. Agregar comando en `scripts/run.py` siguiendo el patrón del comando `weather`
4. El bot recibe el mismo `PolymarketClient` o `PaperClient` – no necesita saber cuál es

### Ejemplo de bots futuros posibles
- `bots/sports/` – mercados deportivos, usando APIs de odds/stats
- `bots/politics/` – mercados electorales, usando polls/modelos
- `bots/crypto/` – mercados de precio de crypto, usando on-chain data
- `bots/arb/` – arbitraje entre mercados relacionados

---

## Archivos de output (generados en runtime)

| Archivo | Descripción |
|---|---|
| `data/paper_portfolio.json` | Estado del portfolio paper (posiciones, cash, trades) |
| `data/logs/operations.jsonl` | Log append-only de todas las operaciones |
| `data/logs/balance_summary.json` | Snapshot del balance (se sobreescribe) |
| `data/logs/bot.log` | Log completo del proceso (rotación 10MB) |

---

## Comandos frecuentes de desarrollo

```bash
# Instalar en modo editable
pip install -e ".[dev]"

# Correr un solo ciclo en modo paper (sin loop)
python -m scripts.run weather --mode paper --once

# Ver balance
python -m scripts.run status

# Ver últimas 30 operaciones
python -m scripts.run operations --tail 30

# Ver solo trades
python -m scripts.run operations --filter trade

# Resetear portfolio paper
rm data/paper_portfolio.json

# Ver logs en tiempo real
tail -f data/logs/bot.log
```

---

## Estrategia del WeatherBot (resumen técnico)

```
1. SCAN:    Fetch mercados activos con keywords de clima, filtrar por
            resolución 0-7 días y liquidez ≥ $500

2. PARSE:   Extraer de la pregunta: ciudad → geocodificar → (lat, lon)
            Extraer: fecha, condición (lluvia/temp/viento), umbral

3. FORECAST: Open-Meteo Ensemble → 50 series de datos
             P(evento) = fracción de miembros que predicen el evento

4. ADJUST:  Ajustar probabilidad por incertidumbre temporal:
            adjusted = 0.5 + (raw - 0.5) * confidence[days_out]
            día 1: 92%, día 3: 70%, día 7: 40%

5. EDGE:    edge = |true_prob - market_price| - 0.02 (fee)
            Si edge < 0.05 (MIN_EDGE) → SKIP

6. SIZE:    f* = (p*b - q) / b  [Kelly completo]
            position = portfolio * f* * 0.25  [¼ Kelly]
            cap: max(8% portfolio, risk budget restante)

7. EXIT (cada ciclo):
   a) Stop-loss:    P&L < -40% del entry → SELL
   b) Take-profit:  capturó ≥ 40% del gain teórico → SELL
   c) Thesis flip:  modelo nuevo apoya lado contrario con edge > 5% → SELL
   d) Default:      HOLD hasta resolución (1 solo fee de 2%)
```

---

## Variables de entorno clave

Ver [.env.example](.env.example) para la lista completa.
Las más importantes:

| Variable | Default | Descripción |
|---|---|---|
| `BOT_MODE` | `paper` | `paper` o `live` |
| `PAPER_INITIAL_BALANCE` | `100.0` | Balance inicial del paper bot |
| `MIN_EDGE` | `0.05` | Edge mínimo para entrar (5%) |
| `KELLY_FRACTION` | `0.25` | Fracción del Kelly a usar |
| `MAX_POSITION_PCT` | `0.08` | Máx % del portfolio por posición |
| `STOP_LOSS_PCT` | `0.40` | Salir si pierde 40% del valor |
| `DRAWDOWN_LIMIT` | `0.25` | Pausar si drawdown supera 25% |
| `SCAN_INTERVAL_SECONDS` | `3600` | Cada cuánto escanear (1h) |
