#!/usr/bin/env bash
# =============================================================================
# setup_ec2.sh — Instalación de BetBot en Ubuntu EC2
#
# Requisitos:
#   - Ubuntu 22.04 o 24.04
#   - Repo ya clonado (git clone ...) en la carpeta actual como usuario ubuntu
#   - .env configurado (o se crea desde .env.example)
#
# Uso (desde la raíz del repo):
#   chmod +x deploy/setup_ec2.sh
#   ./deploy/setup_ec2.sh
#
# NO necesita sudo — instala en el repo del usuario actual.
# El servicio systemd sí necesita sudo (el script te lo pide).
# =============================================================================

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
INSTALL_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
RUN_USER="$(whoami)"
PYTHON="python3.11"

GREEN='\033[0;32m'; YELLOW='\033[1;33m'; RED='\033[0;31m'; BOLD='\033[1m'; NC='\033[0m'
log()  { echo -e "${GREEN}✓${NC} $*"; }
warn() { echo -e "${YELLOW}!${NC} $*"; }
err()  { echo -e "${RED}✗${NC} $*"; exit 1; }
step() { echo -e "\n${BOLD}── $* ──${NC}"; }

echo ""
echo -e "${BOLD}BetBot – Setup en EC2${NC}"
echo -e "Directorio: ${INSTALL_DIR}"
echo -e "Usuario:    ${RUN_USER}"
echo ""

# ── 1. Python 3.11 ───────────────────────────────────────────────────────────
step "Python 3.11"

if command -v python3.11 &>/dev/null; then
    log "Python 3.11 ya instalado: $(python3.11 --version)"
else
    log "Instalando Python 3.11..."
    sudo apt-get update -qq
    sudo apt-get install -y -qq software-properties-common
    sudo add-apt-repository -y ppa:deadsnakes/ppa
    sudo apt-get update -qq
    sudo apt-get install -y -qq python3.11 python3.11-venv python3.11-dev
    log "Python 3.11 instalado: $(python3.11 --version)"
fi

# git ya está en Ubuntu EC2 por defecto
sudo apt-get install -y -qq git curl nano 2>/dev/null || true

# ── 2. Entorno virtual ───────────────────────────────────────────────────────
step "Entorno virtual"

VENV_DIR="$INSTALL_DIR/.venv"

if [[ -d "$VENV_DIR" ]]; then
    warn "Venv ya existe en $VENV_DIR, reinstalando dependencias..."
else
    python3.11 -m venv "$VENV_DIR"
    log "Venv creado en $VENV_DIR"
fi

"$VENV_DIR/bin/pip" install --upgrade pip -q
"$VENV_DIR/bin/pip" install -e "$INSTALL_DIR" -q
log "Dependencias instaladas"

# ── 3. Directorios de datos ──────────────────────────────────────────────────
step "Directorios"

mkdir -p "$INSTALL_DIR/data/logs"
log "data/logs/ listo"

# ── 4. Archivo .env ──────────────────────────────────────────────────────────
step "Configuración .env"

if [[ ! -f "$INSTALL_DIR/.env" ]]; then
    cp "$INSTALL_DIR/.env.example" "$INSTALL_DIR/.env"
    chmod 600 "$INSTALL_DIR/.env"
    warn "Creado .env desde template — EDÍTALO ANTES DE INICIAR EL BOT:"
    warn "  nano $INSTALL_DIR/.env"
else
    log ".env ya existe"
fi

# ── 5. Servicio systemd ──────────────────────────────────────────────────────
step "Servicio systemd"

SERVICE_FILE="/etc/systemd/system/betbot-weather.service"

sudo bash -c "sed \
    's|__INSTALL_DIR__|${INSTALL_DIR}|g; s|__SERVICE_USER__|${RUN_USER}|g' \
    '${INSTALL_DIR}/deploy/betbot-weather.service' \
    > '${SERVICE_FILE}'"

sudo systemctl daemon-reload
sudo systemctl enable betbot-weather
log "Servicio instalado y habilitado para arranque automático"

# ── 6. Permisos del manage.sh ────────────────────────────────────────────────
chmod +x "$INSTALL_DIR/deploy/manage.sh"

# ── Resumen ──────────────────────────────────────────────────────────────────
echo ""
echo -e "${GREEN}${BOLD}════════════════════════════════════════════${NC}"
echo -e "${GREEN}${BOLD}  ✓ BetBot instalado correctamente          ${NC}"
echo -e "${GREEN}${BOLD}════════════════════════════════════════════${NC}"
echo ""
echo "  Próximos pasos:"
echo ""
echo "  1. Editar configuración (si aún no lo hiciste):"
echo "     nano $INSTALL_DIR/.env"
echo ""
echo "  2. Probar un ciclo manual:"
echo "     ./deploy/manage.sh scan-once"
echo ""
echo "  3. Iniciar el bot en segundo plano:"
echo "     ./deploy/manage.sh start"
echo ""
echo "  4. Ver logs en tiempo real:"
echo "     ./deploy/manage.sh logs"
echo ""
echo "  Todos los comandos:"
echo "     ./deploy/manage.sh help"
echo ""
