#!/usr/bin/env bash
# =============================================================================
# setup_ec2.sh
# Setup automático de BetBot en Ubuntu 22.04 (EC2 o cualquier VPS)
#
# Uso (desde la raíz del repo clonado):
#   chmod +x deploy/setup_ec2.sh
#   sudo ./deploy/setup_ec2.sh
#
# El directorio de instalación es la raíz del repositorio (no se usa /opt).
#   BETBOT_INSTALL_DIR=/ruta/al/repo — otra raíz del repo (debe existir deploy/)
#   BETBOT_REPO_URL=https://...     — clonar; destino = BETBOT_INSTALL_DIR o /home/betbot/betbot
#
# Qué hace:
#   1. Actualiza el sistema
#   2. Instala Python 3.11
#   3. Crea un usuario dedicado 'betbot'
#   4. Deja el código en el repo (o clona con BETBOT_REPO_URL)
#   5. Crea el entorno virtual e instala dependencias
#   6. Instala y activa el servicio systemd
#   7. Crea la estructura de directorios de datos
# =============================================================================

set -euo pipefail

# ── Resolver rutas (funciona aunque se invoque con sudo) ──────────────────────
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DEFAULT_REPO="$(cd "$SCRIPT_DIR/.." && pwd)"

# Colores para output
RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; NC='\033[0m'
log()  { echo -e "${GREEN}[+]${NC} $*"; }
warn() { echo -e "${YELLOW}[!]${NC} $*"; }
err()  { echo -e "${RED}[✗]${NC} $*"; exit 1; }

# ── Configuración ─────────────────────────────────────────────────────────────
REPO_URL="${BETBOT_REPO_URL:-}"
SERVICE_USER="betbot"
BETBOT_UNIX_HOME="/home/${SERVICE_USER}"
PYTHON_VERSION="3.11"

# Sin BETBOT_REPO_URL: se usa el repo donde está este script (o BETBOT_INSTALL_DIR).
# Con BETBOT_REPO_URL: destino = BETBOT_INSTALL_DIR o /home/betbot/betbot (no usar /home/ubuntu/betbot:
# el usuario del servicio no puede atravesar /home/ubuntu si el home está en 750).
if [[ -n "$REPO_URL" ]]; then
    if [[ -n "${BETBOT_INSTALL_DIR:-}" ]]; then
        INSTALL_DIR="$BETBOT_INSTALL_DIR"
    else
        INSTALL_DIR="${BETBOT_UNIX_HOME}/betbot"
        log "Clonando en $INSTALL_DIR (definí BETBOT_INSTALL_DIR= para otra ruta)"
    fi
else
    INSTALL_DIR="${BETBOT_INSTALL_DIR:-$DEFAULT_REPO}"
fi

# ── Verificar que corre como root ─────────────────────────────────────────────
[[ $EUID -eq 0 ]] || err "Ejecutar con sudo: sudo ./deploy/setup_ec2.sh"

# ── 1. Actualizar sistema e instalar dependencias ─────────────────────────────
log "Actualizando sistema..."
apt-get update -qq
apt-get upgrade -y -qq

log "Instalando dependencias del sistema..."
apt-get install -y -qq \
    software-properties-common \
    build-essential \
    curl \
    wget \
    git \
    libssl-dev \
    libffi-dev \
    python3-dev \
    python3-pip \
    python3-venv \
    logrotate \
    htop \
    nano

# ── 2. Instalar Python 3.11 ───────────────────────────────────────────────────
log "Instalando Python $PYTHON_VERSION..."
add-apt-repository -y ppa:deadsnakes/ppa
apt-get update -qq
apt-get install -y -qq "python${PYTHON_VERSION}" "python${PYTHON_VERSION}-venv" "python${PYTHON_VERSION}-dev"

PYTHON_BIN=$(which "python${PYTHON_VERSION}")
log "Python instalado en: $PYTHON_BIN"
"$PYTHON_BIN" --version

# ── 3. Crear usuario del sistema (home propio, distinto del repo) ───────────
if id "$SERVICE_USER" &>/dev/null; then
    warn "Usuario '$SERVICE_USER' ya existe, continuando..."
else
    log "Creando usuario del sistema '$SERVICE_USER' (home $BETBOT_UNIX_HOME)..."
    useradd --system --shell /bin/bash --home-dir "$BETBOT_UNIX_HOME" --create-home "$SERVICE_USER"
fi

# ── 4. Código en INSTALL_DIR (raíz del repo) ─────────────────────────────────
if [[ -n "$REPO_URL" ]]; then
    log "Repositorio git: $REPO_URL → $INSTALL_DIR"
    if [[ -d "$INSTALL_DIR/.git" ]]; then
        warn "Repo ya existe, haciendo pull..."
        sudo -u "$SERVICE_USER" git -C "$INSTALL_DIR" pull
    else
        if [[ -e "$INSTALL_DIR" ]] && [[ -n "$(ls -A "$INSTALL_DIR" 2>/dev/null || true)" ]]; then
            err "INSTALL_DIR no está vacío: $INSTALL_DIR — vacía la carpeta o elige otra con BETBOT_INSTALL_DIR="
        fi
        mkdir -p "$(dirname "$INSTALL_DIR")"
        git clone "$REPO_URL" "$INSTALL_DIR"
        chown -R "$SERVICE_USER:$SERVICE_USER" "$INSTALL_DIR"
    fi
else
    if [[ ! -f "$INSTALL_DIR/deploy/betbot-weather.service" ]]; then
        err "No se encontró el repo en $INSTALL_DIR (falta deploy/betbot-weather.service). " \
            "Ejecuta: cd /ruta/al/betbot && sudo ./deploy/setup_ec2.sh  o exporta BETBOT_INSTALL_DIR="
    fi
    log "Usando el repo ya presente en $INSTALL_DIR"
    chown -R "$SERVICE_USER:$SERVICE_USER" "$INSTALL_DIR"
fi

# ── 5. Crear directorios de datos ───────────────────────────────────────────
log "Creando directorios de datos..."
mkdir -p "$INSTALL_DIR/data/logs"
chown -R "$SERVICE_USER:$SERVICE_USER" "$INSTALL_DIR/data"

# ── 5b. Recorrido hasta el repo (evita fallo si el repo está bajo /home/ubuntu/...) ──
# En Ubuntu, /home/ubuntu suele ser 750: el usuario del servicio no puede entrar aunque sea dueño de ~/betbot.
ensure_service_user_reaches_repo() {
    local d
    d=$(dirname "$INSTALL_DIR")
    while [[ "$d" != "/" && "$d" != "/home" ]]; do
        if [[ "$d" == /home/* ]] && ! sudo -u "$SERVICE_USER" test -x "$d" 2>/dev/null; then
            log "chmod o+x $d (recorrido para usuario $SERVICE_USER hasta el repo)"
            chmod o+x "$d"
        fi
        d=$(dirname "$d")
    done
    if ! sudo -u "$SERVICE_USER" test -w "$INSTALL_DIR" 2>/dev/null; then
        err "El usuario $SERVICE_USER no puede escribir en $INSTALL_DIR. " \
            "Cloná en ${BETBOT_UNIX_HOME}/betbot o definí BETBOT_INSTALL_DIR con una ruta accesible."
    fi
}
ensure_service_user_reaches_repo

# ── 6. Crear entorno virtual e instalar dependencias ────────────────────────
VENV_DIR="$INSTALL_DIR/.venv"

log "Creando entorno virtual en $VENV_DIR..."
sudo -u "$SERVICE_USER" "$PYTHON_BIN" -m venv "$VENV_DIR"

log "Instalando dependencias Python..."
sudo -u "$SERVICE_USER" "$VENV_DIR/bin/pip" install --upgrade pip -q
sudo -u "$SERVICE_USER" "$VENV_DIR/bin/pip" install -e "$INSTALL_DIR" -q

log "Dependencias instaladas:"
sudo -u "$SERVICE_USER" "$VENV_DIR/bin/pip" list --format=columns | grep -E "betbot|requests|pydantic|click|rich|loguru"

# ── 7. Configurar .env ────────────────────────────────────────────────────────
if [[ ! -f "$INSTALL_DIR/.env" ]]; then
    log "Creando .env desde template..."
    cp "$INSTALL_DIR/.env.example" "$INSTALL_DIR/.env"
    chown "$SERVICE_USER:$SERVICE_USER" "$INSTALL_DIR/.env"
    chmod 600 "$INSTALL_DIR/.env"   # solo el owner puede leerlo

    warn "============================================================"
    warn "  IMPORTANTE: Edita $INSTALL_DIR/.env antes de iniciar el bot"
    warn "  nano $INSTALL_DIR/.env"
    warn "============================================================"
else
    warn ".env ya existe, no se sobreescribe."
fi

# ── 8. Configurar logrotate ───────────────────────────────────────────────────
log "Configurando logrotate..."
cat > /etc/logrotate.d/betbot << EOF
$INSTALL_DIR/data/logs/*.log {
    daily
    rotate 30
    compress
    delaycompress
    missingok
    notifempty
    create 0640 $SERVICE_USER $SERVICE_USER
}

$INSTALL_DIR/data/logs/operations.jsonl {
    monthly
    rotate 12
    compress
    delaycompress
    missingok
    notifempty
    copytruncate
    create 0640 $SERVICE_USER $SERVICE_USER
}
EOF

# ── 9. Instalar servicio systemd ──────────────────────────────────────────────
log "Instalando servicio systemd..."

sed "s|__INSTALL_DIR__|$INSTALL_DIR|g; s|__SERVICE_USER__|$SERVICE_USER|g" \
    "$INSTALL_DIR/deploy/betbot-weather.service" \
    > /etc/systemd/system/betbot-weather.service

systemctl daemon-reload
systemctl enable betbot-weather

log "Servicio instalado. Estado inicial:"
systemctl status betbot-weather --no-pager || true

# ── 10. Instrucciones finales ─────────────────────────────────────────────────
echo ""
echo -e "${GREEN}════════════════════════════════════════════════════════${NC}"
echo -e "${GREEN}  ✓ BetBot instalado en el repo: $INSTALL_DIR            ${NC}"
echo -e "${GREEN}════════════════════════════════════════════════════════${NC}"
echo ""
echo "  Próximos pasos:"
echo ""
echo "  1. Editar configuración:"
echo "     sudo nano $INSTALL_DIR/.env"
echo ""
echo "  2. Probar con un ciclo manual:"
echo "     sudo -u $SERVICE_USER $VENV_DIR/bin/python -m scripts.run weather --mode paper --once"
echo ""
echo "  3. Iniciar el servicio:"
echo "     sudo systemctl start betbot-weather"
echo ""
echo "  4. Ver logs en tiempo real:"
echo "     sudo journalctl -u betbot-weather -f"
echo ""
echo "  5. Gestionar desde el repo:"
echo "     cd $INSTALL_DIR && ./deploy/manage.sh status"
echo ""
