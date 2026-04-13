# BetBot

Bots de trading automatizados para [Polymarket](https://polymarket.com), el mercado de predicciones descentralizado.

**Bot disponible:** `weather` — apuesta en mercados de clima usando ensemble de modelos meteorológicos.

---

## Índice

- [Estrategia](#estrategia)
- [Requisitos](#requisitos)
- [Instalación](#instalación)
- [Configuración del .env](#configuración-del-env)
  - [Modo paper (dinero ficticio)](#modo-paper-dinero-ficticio)
  - [Modo live (wallet real)](#modo-live-wallet-real)
- [Uso](#uso)
- [Archivos de log](#archivos-de-log)
- [Despliegue en servidor (EC2)](#despliegue-en-servidor-ec2)

---

## Estrategia

El bot compara la **probabilidad real** de un evento climático (calculada con 50 modelos meteorológicos) contra el **precio del mercado** en Polymarket.

Si la diferencia supera el 5% después de fees, entra en la posición.

```
Probabilidad real (Open-Meteo, 50 modelos)
          vs
Precio del mercado (Polymarket)
          =
Edge = diferencia - 2% fee

Si edge > 5% → entrar
Tamaño = ¼ Kelly, máximo 8% del portfolio
```

**Gestión de riesgo:**
- Stop-loss automático si la posición pierde 40% de su valor
- Salida temprana si captura ≥ 40% de la ganancia teórica
- Salida si el modelo meteorológico actualizado contradice la tesis
- Pausa automática si drawdown supera 25% o pérdida diaria supera 10%

**Por defecto: hold hasta resolución** (1 solo fee de 2%). Salida temprana solo si alguna condición de exit se activa.

---

## Requisitos

- Python **3.11** o superior
- Cuenta en Polymarket (para modo live)
- Wallet de **Polygon/MATIC** con USDC (para modo live)
- Servidor con IP fija o EC2 (para correr 24/7)

---

## Instalación

```bash
# Clonar el repositorio
git clone <tu-repo> betbot
cd betbot

# Crear entorno virtual (recomendado)
python3.11 -m venv .venv
source .venv/bin/activate   # en Windows: .venv\Scripts\activate

# Instalar dependencias
pip install -e .

# Copiar template de configuración
cp .env.example .env
```

---

## Configuración del .env

### Modo paper (dinero ficticio)

> Empieza aquí. No necesitas wallet ni credenciales de Polymarket.
> El bot simula todas las operaciones con **$100 virtuales** y registra
> todo como si fuera real, sin apostar ni un centavo.

Edita `.env` con solo estas líneas (el resto puede quedar como está):

```env
BOT_MODE=paper
PAPER_INITIAL_BALANCE=100.0
```

Eso es todo. Puedes correr el bot inmediatamente.

**Variables opcionales para ajustar la estrategia en paper:**

```env
# ── Gestión de riesgo ──────────────────────────────────────
MIN_EDGE=0.05           # Edge mínimo para entrar (5%). Sube a 0.08 para ser más conservador.
KELLY_FRACTION=0.25     # Usa el 25% del Kelly óptimo. Baja a 0.10 para apuestas más pequeñas.
MAX_POSITION_PCT=0.08   # Máximo 8% del portfolio en una sola posición.
STOP_LOSS_PCT=0.40      # Salir si la posición pierde 40% de su valor.
TAKE_PROFIT_PCT=0.40    # Salir temprano si capta 40%+ de la ganancia teórica.
DRAWDOWN_LIMIT=0.25     # Pausar trading si drawdown supera 25% desde el pico.
DAILY_LOSS_LIMIT=0.10   # Pausar si pérdida diaria supera 10%.

# ── Filtros de mercado ──────────────────────────────────────
MIN_LIQUIDITY_USD=500   # Ignorar mercados con menos de $500 de liquidez.
MAX_DAYS_TO_RESOLUTION=7  # Solo mercados que resuelven en máximo 7 días.
MIN_DAYS_TO_RESOLUTION=0  # Incluir mercados que resuelven hoy.

# ── Frecuencia ──────────────────────────────────────────────
SCAN_INTERVAL_SECONDS=3600  # Escanear cada 1 hora.
```

---

### Modo live (wallet real)

> **Advertencia:** en modo live el bot apuesta dinero real. Prueba primero
> con paper trading y entiende bien la estrategia antes de activar live.

#### Paso 1 — Wallet de Polygon

Necesitas una wallet de Ethereum/Polygon con:
- Algo de MATIC para gas (Polygon, muy barato: ~$0.01 por tx)
- USDC en la red Polygon para apostar

Puedes crear una wallet con [MetaMask](https://metamask.io) y fondear con USDC desde un exchange.

Tu **clave privada** es la que empieza con `0x...` en MetaMask → Detalles de cuenta → Mostrar clave privada.

> **NUNCA** compartas tu clave privada. Solo la escribe en tu archivo `.env` local
> que está en `.gitignore` (no se sube al repo).

#### Paso 2 — API keys de Polymarket

1. Entra a [polymarket.com](https://polymarket.com) con tu wallet
2. Ve a **Configuración → API Keys → Crear nueva key**
3. Guarda: `API Key`, `Secret` y `Passphrase`

#### Paso 3 — Llenar el .env

```env
# ── Modo ────────────────────────────────────────────────────
BOT_MODE=live

# ── Wallet ──────────────────────────────────────────────────
# Tu clave privada de la wallet de Polygon (con USDC)
POLY_PRIVATE_KEY=0x_tu_clave_privada_aqui

# ── API de Polymarket ────────────────────────────────────────
POLY_API_KEY=tu_api_key
POLY_API_SECRET=tu_api_secret
POLY_API_PASSPHRASE=tu_passphrase

# Polygon Mainnet = 137 (NO cambies esto salvo que sepas lo que haces)
POLY_CHAIN_ID=137

# ── Gestión de riesgo (empieza conservador) ─────────────────
MIN_EDGE=0.07           # En live, sé más exigente: 7% de edge mínimo
KELLY_FRACTION=0.15     # En live, usa menos Kelly: 15%
MAX_POSITION_PCT=0.05   # Máximo 5% del portfolio por posición
SCAN_INTERVAL_SECONDS=3600
```

---

## Uso

### Comandos principales

```bash
# Activar entorno virtual (si lo usas)
source .venv/bin/activate

# ── Paper trading ────────────────────────────────────────────

# Correr el bot en bucle (se repite cada SCAN_INTERVAL_SECONDS)
python -m scripts.run weather --mode paper

# Correr un solo ciclo y salir (útil para probar)
python -m scripts.run weather --mode paper --once

# Cambiar intervalo sin editar .env
python -m scripts.run weather --mode paper --interval 1800  # cada 30 min

# ── Live trading ─────────────────────────────────────────────
python -m scripts.run weather --mode live
python -m scripts.run weather --mode live --once

# ── Monitoreo ────────────────────────────────────────────────

# Ver balance y estadísticas
python -m scripts.run status

# Ver últimas 20 operaciones
python -m scripts.run operations

# Ver últimas 50
python -m scripts.run operations --tail 50

# Filtrar solo los trades
python -m scripts.run operations --filter trade

# Filtrar solo las entradas
python -m scripts.run operations --filter position_open

# ── Utilidades ───────────────────────────────────────────────

# Ver logs en tiempo real
tail -f data/logs/bot.log

# Resetear portfolio paper (vuelve a $100)
rm data/paper_portfolio.json

# Ver modo verbose (más detalle en consola)
python -m scripts.run --verbose weather --mode paper --once
```

---

## Archivos de log

El bot escribe dos archivos en `data/logs/`:

### `operations.jsonl` — Registro de operaciones

Log append-only (una línea JSON por evento). Nunca se borra, crece con el tiempo.

```jsonc
// Ejemplo de líneas que genera:
{"timestamp": "2025-04-12T10:00:00", "event": "scan_start", "bot": "weather"}
{"timestamp": "2025-04-12T10:00:05", "event": "signal_skipped", "reason": "edge 3.2% below minimum 5.0%", "question": "Will it rain in Miami on April 13?"}
{"timestamp": "2025-04-12T10:00:06", "event": "trade", "side": "YES", "order_side": "BUY", "amount_usd": 8.0, "price": 0.31, "fee_usd": 0.16}
{"timestamp": "2025-04-12T10:00:06", "event": "position_open", "true_prob": 0.62, "edge": 0.09}
{"timestamp": "2025-04-12T14:30:00", "event": "position_close", "pnl_usd": 5.2, "pnl_pct": 0.65, "reason": "take-profit hit"}
```

### `balance_summary.json` — Resumen del balance

Se sobreescribe en cada ciclo con el estado actual.

```json
{
  "updated_at": "2025-04-12T14:30:00",
  "mode": "paper",
  "cash_usd": 97.04,
  "open_positions_value_usd": 10.20,
  "total_value_usd": 107.24,
  "total_pnl_usd": 7.24,
  "realized_pnl_usd": 5.20,
  "unrealized_pnl_usd": 2.04,
  "peak_value_usd": 107.24,
  "drawdown_pct": 0.0,
  "open_position_count": 1,
  "total_trades": 4,
  "winning_trades": 2,
  "losing_trades": 1,
  "win_rate": 0.667
}
```

---

## Despliegue en servidor (EC2 Ubuntu)

Para que el bot corra 24/7 como servicio del sistema usando **systemd** (sin Docker).

### Paso 1 — Crear la instancia EC2

En AWS Console → EC2 → Launch Instance:

| Parámetro | Valor |
|---|---|
| OS | **Ubuntu 22.04 LTS** |
| Tipo | `t3.micro` (free tier) o `t3.small` |
| Almacenamiento | 20 GB gp3 |
| Security Group | Permitir SSH (puerto 22) desde tu IP |

Guardar el archivo `.pem` de la key pair en un lugar seguro.

### Paso 2 — Conectarse al servidor

```bash
chmod 400 tu-key.pem
ssh -i tu-key.pem ubuntu@<IP_PUBLICA_EC2>
```

### Paso 3 — Tener el repo en el servidor (sin `/opt`)

Todo queda en la carpeta del repositorio. El servicio systemd usa esa ruta como `WorkingDirectory`.

**Permisos (importante):** el proceso corre como usuario del sistema **`betbot`**, no como `ubuntu`. En Ubuntu, `/home/ubuntu` suele ser `drwxr-x---` (750), así que **`betbot` no puede entrar** a `/home/ubuntu/betbot` aunque el repo sea suyo. El script **`setup_ec2.sh`** aplica **`chmod o+x /home/ubuntu`** (solo recorrido, no listado) cuando hace falta. Otra opción es instalar bajo **`/home/betbot/betbot`** (p. ej. solo con `BETBOT_REPO_URL` y el script actual).

**Opción A: clonar en el home de `ubuntu`**
```bash
# En el servidor
cd ~
git clone https://github.com/tu-usuario/betbot.git betbot
cd betbot
```

**Opción B: subir desde tu máquina local**
```bash
# En tu máquina local (crea ~/betbot en la EC2)
scp -i tu-key.pem -r ./betbot ubuntu@<IP_EC2>:~/betbot
# En el servidor
ssh -i tu-key.pem ubuntu@<IP_EC2>
cd ~/betbot
```

**Opción C: descargar solo `setup_ec2.sh` y que clone el repo** (por defecto en **`/home/betbot/betbot`**, accesible para el usuario del servicio):

```bash
wget -O setup_ec2.sh "https://raw.githubusercontent.com/tu-usuario/betbot/main/deploy/setup_ec2.sh"
chmod +x setup_ec2.sh
sudo BETBOT_REPO_URL="https://github.com/tu-usuario/betbot.git" ./setup_ec2.sh
# Otra ruta explícita (si ponés el repo bajo /home/ubuntu, el script ajusta o+x al padre):
# sudo BETBOT_REPO_URL="..." BETBOT_INSTALL_DIR=/home/ubuntu/mi-betbot ./setup_ec2.sh
```

Lo habitual en EC2 es la **opción A** (clonar y `cd` al repo) o **B**.

### Paso 4 — Ejecutar el script de setup

Desde la **raíz del repo** (donde están `deploy/`, `pyproject.toml`, etc.):

```bash
cd ~/betbot    # o la ruta donde clonaste
chmod +x deploy/setup_ec2.sh
sudo ./deploy/setup_ec2.sh
```

Variables opcionales:
- **`BETBOT_INSTALL_DIR`** — raíz del repo si no es el directorio desde el que corre el script (debe contener `deploy/betbot-weather.service`).
- **`BETBOT_REPO_URL`** — clonar desde git; si no defines `BETBOT_INSTALL_DIR`, el destino por defecto es **`/home/betbot/betbot`**.

El script hace automáticamente:
- Instala Python 3.11
- Crea el usuario del sistema `betbot` (home Unix `/home/betbot`, distinto del repo)
- Deja el código en el repo (no copia nada a `/opt`)
- Crea el entorno virtual e instala dependencias
- Configura el servicio systemd con restart automático
- Configura logrotate para los archivos de log

Al terminar verás la ruta exacta del repo y los siguientes pasos.

Si el setup falla con **`Permission denied`** al crear **`.venv`** bajo `/home/ubuntu/`, es el recorrido `750` del home de `ubuntu`: **`sudo chmod o+x /home/ubuntu`**, borrá un `.venv` a medias si quedó (`sudo rm -rf .venv`) y volvé a ejecutar **`sudo ./deploy/setup_ec2.sh`** (el script actual también hace ese `chmod` solo).

### Paso 5 — Configurar el .env en el servidor

```bash
sudo nano ~/betbot/.env
```
(Ajusta la ruta si clonaste en otro sitio.)

Para **paper trading** en el servidor (recomendado para empezar):

```env
BOT_MODE=paper
PAPER_INITIAL_BALANCE=100.0
SCAN_INTERVAL_SECONDS=3600
MIN_EDGE=0.05
```

Para **live trading** agrega además:
```env
BOT_MODE=live
POLY_PRIVATE_KEY=0x_tu_clave_privada
POLY_API_KEY=tu_api_key
POLY_API_SECRET=tu_api_secret
POLY_API_PASSPHRASE=tu_passphrase
POLY_CHAIN_ID=137
```

### Paso 6 — Probar y arrancar

```bash
cd ~/betbot   # raíz del repo

# Probar que funciona con un ciclo manual antes de activar el servicio
sudo -u betbot .venv/bin/python -m scripts.run weather --mode paper --once

# Si todo OK, arrancar el servicio
sudo systemctl start betbot-weather

# Verificar que está corriendo
sudo systemctl status betbot-weather
```

---

### Actualizar cuando hay cambios (pull + volver a correr)

Al tener todo en el repo (`~/betbot`), los despliegues de código son:

1. **Atajo (recomendado):** desde la raíz del repo hace `git pull`, reinstala el paquete editable (por si cambió `pyproject.toml` o dependencias) y reinicia el servicio:

```bash
cd ~/betbot
./deploy/manage.sh update
```

2. **A mano:** mismo efectivo — parar, pull como usuario `betbot` (dueño del repo), actualizar el venv si hace falta, arrancar de nuevo:

```bash
cd ~/betbot
sudo systemctl stop betbot-weather
sudo -u betbot git pull
sudo -u betbot .venv/bin/pip install -e . -q
sudo systemctl start betbot-weather
```

Si solo cambiaron archivos `.py` sin tocar dependencias, podés hacer **`sudo -u betbot git pull`** + **`sudo systemctl restart betbot-weather`** (no uses `git pull` solo como `ubuntu`: el `setup` dejó el repo con dueño **`betbot`** y `.git/` no es escribible por `ubuntu`).

#### Git: `dubious ownership` o `FETCH_HEAD: Permission denied`

Tras **`setup_ec2.sh`**, el directorio del repo suele ser **`betbot:betbot`**. Como usuario **`ubuntu`**:

1. **`fatal: detected dubious ownership`** — Git 2.35+ bloquea repos de otro dueño. Una vez (como `ubuntu`):

   ```bash
   git config --global --add safe.directory /home/ubuntu/betbot
   ```

   (Ajustá la ruta si el repo no está ahí.)

2. **`error: cannot open '.git/FETCH_HEAD': Permission denied`** — Es normal: **`git pull` debe ejecutarlo el dueño del repo**:

   ```bash
   cd ~/betbot
   sudo -u betbot git pull
   ```

   O usá **`./deploy/manage.sh update`**, que hace `pull` como `betbot`, reinstala el editable y reinicia el servicio.

---

### Gestión diaria con manage.sh

El archivo `deploy/manage.sh` tiene comandos de atajo para no tener que recordar systemd:

```bash
cd ~/betbot   # raíz del repo (misma ruta que usó setup_ec2.sh)

# Eliminar datos modo paper (con sudo si data/ es de usuario betbot)
sudo python3 -m scripts.run paper-reset -y --with-logs --bot-log
# Con venv: sudo .venv/bin/python -m scripts.run paper-reset -y --with-logs --bot-log

# Dar permisos de ejecución (solo la primera vez)
chmod +x deploy/manage.sh

# Estado del servicio + últimos logs
./deploy/manage.sh status

# Ver logs en tiempo real (Ctrl+C para salir)
./deploy/manage.sh logs

# Ver balance actual
./deploy/manage.sh balance

# Ver últimas 30 operaciones
./deploy/manage.sh operations

# Reiniciar (necesario después de cambiar .env)
./deploy/manage.sh restart

# Actualizar código desde git y reiniciar
./deploy/manage.sh update

# Correr un ciclo manual (para debugging)
./deploy/manage.sh scan-once paper
```

### Comandos systemd directos

```bash
sudo systemctl status betbot-weather     # estado
sudo systemctl start betbot-weather      # iniciar
sudo systemctl stop betbot-weather       # detener
sudo systemctl restart betbot-weather    # reiniciar

# Logs en tiempo real
sudo journalctl -u betbot-weather -f

# Últimas 100 líneas
sudo journalctl -u betbot-weather -n 100

# Logs de hoy
sudo journalctl -u betbot-weather --since today
```

### SSH, segundo plano y cómo apagarlo

Tras el setup con **`setup_ec2.sh`**, el bot **no depende de tu sesión SSH**: lo ejecuta **systemd** como servicio `betbot-weather`. Puedes conectarte, arrancarlo y **cerrar SSH**; el proceso sigue en el servidor hasta que lo detengas o reinicies la instancia (si el servicio está habilitado con `enable`, también puede volver a levantarse solo al boot).

| Qué quieres | Comando (en el servidor, por SSH) |
|---|---|
| Dejarlo corriendo | `sudo systemctl start betbot-weather` |
| Comprobar que sigue activo | `sudo systemctl status betbot-weather` |
| Apagarlo | `sudo systemctl stop betbot-weather` |
| Que arranque solo al reiniciar la EC2 | `sudo systemctl enable betbot-weather` (si aún no lo hizo el setup) |
| Que no arranque al reiniciar la EC2 | `sudo systemctl disable betbot-weather` |

Los logs van a **journald** (`journalctl -u betbot-weather -f`) y el bot también escribe en **`data/logs/bot.log`** dentro del repo (p. ej. `~/betbot/data/logs/bot.log`).

#### Sin systemd (prueba rápida en tu `$HOME`)

Si clonaste el repo **sin** instalar el servicio y solo quieres que no muera al salir de SSH, usa **tmux** (suele venir en Ubuntu; si no: `sudo apt install tmux`):

```bash
cd ~/betbot   # o la ruta donde tengas el proyecto
source .venv/bin/activate

tmux new -s betbot
python -m scripts.run weather --mode paper
# Ctrl+B, luego D  →  desasocia la sesión; puedes cerrar SSH
```

Volver a ver el proceso:

```bash
tmux attach -t betbot
# Ctrl+C  →  detiene el bot; luego exit o Ctrl+D para salir del tmux
```

O matar la sesión entera sin reattach:

```bash
tmux kill-session -t betbot
```

**No recomendado para producción:** `nohup python -m scripts.run weather --mode paper > bot.out 2>&1 &` — al apagar hay que buscar el PID con `ps aux | grep scripts.run` y `kill <pid>`.

### Instancia EC2 recomendada

| Recurso | Mínimo | Suficiente para 1 bot |
|---|---|---|
| Tipo | `t3.micro` (free tier) | `t3.small` |
| OS | Ubuntu 22.04 LTS | Ubuntu 22.04 LTS |
| RAM | 1 GB | 2 GB |
| Disco | 8 GB | 20 GB |

El bot es muy liviano: solo hace llamadas HTTP cada hora.
Un `t3.micro` gratuito es más que suficiente.

---

## Estructura del proyecto

```
betbot/
├── CLAUDE.md               ← Contexto para Claude Code (asistente IA)
├── README.md               ← Esta documentación
├── pyproject.toml          ← Dependencias Python
├── .env.example            ← Template de configuración
├── config/settings.yaml    ← Parámetros por defecto
├── core/                   ← Módulos compartidos
│   ├── models.py           ← Tipos de datos
│   ├── polymarket/         ← Cliente live + paper
│   ├── weather/            ← Cliente Open-Meteo
│   ├── risk/               ← Kelly, stop-loss, pausas
│   └── portfolio/          ← Tracker + logger
├── bots/                   ← Un bot por subcarpeta
│   ├── base.py             ← Clase base abstracta
│   └── weather/            ← Bot de clima
│       ├── parser.py       ← Parsea preguntas del mercado
│       ├── strategy.py     ← Lógica de entrada/salida
│       └── bot.py          ← Orquestador
├── scripts/
│   └── run.py              ← CLI principal
├── deploy/                 ← Archivos de despliegue
│   ├── setup_ec2.sh        ← Setup automático para Ubuntu
│   ├── betbot-weather.service  ← Servicio systemd
│   └── docker-compose.yml  ← Alternativa con Docker
└── data/                   ← Generado en runtime (gitignored)
    ├── paper_portfolio.json
    └── logs/
        ├── operations.jsonl
        ├── balance_summary.json
        └── bot.log
```
