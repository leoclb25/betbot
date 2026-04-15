#!/usr/bin/env bash
# =============================================================================
# manage.sh — Gestión del bot en EC2
#
# Uso desde la raíz del repo:
#   ./deploy/manage.sh start
#   ./deploy/manage.sh stop
#   ./deploy/manage.sh restart
#   ./deploy/manage.sh update       ← git pull + reinstalar + reiniciar
#   ./deploy/manage.sh logs         ← ver logs en tiempo real
#   ./deploy/manage.sh status
#   ./deploy/manage.sh balance
#   ./deploy/manage.sh operations
#   ./deploy/manage.sh scan-once    ← un ciclo manual para probar
# =============================================================================

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
INSTALL_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
SERVICE="betbot-weather"
PYTHON="$INSTALL_DIR/.venv/bin/python"

GREEN='\033[0;32m'; YELLOW='\033[1;33m'; RED='\033[0;31m'; BOLD='\033[1m'; NC='\033[0m'
log()  { echo -e "${GREEN}▶${NC} $*"; }
warn() { echo -e "${YELLOW}⚠${NC} $*"; }
err()  { echo -e "${RED}✗${NC} $*"; exit 1; }

[[ -f "$PYTHON" ]] || err "Venv no encontrado en $INSTALL_DIR/.venv — corre setup_ec2.sh primero"

CMD="${1:-help}"

case "$CMD" in

    start)
        log "Iniciando $SERVICE..."
        sudo systemctl start "$SERVICE"
        sleep 1
        sudo systemctl status "$SERVICE" --no-pager -l
        ;;

    stop)
        warn "Deteniendo $SERVICE..."
        sudo systemctl stop "$SERVICE"
        log "Bot detenido."
        ;;

    restart)
        warn "Reiniciando $SERVICE..."
        sudo systemctl restart "$SERVICE"
        sleep 1
        sudo systemctl status "$SERVICE" --no-pager -l
        ;;

    status)
        sudo systemctl status "$SERVICE" --no-pager -l
        echo ""
        log "Últimas 15 líneas de log:"
        sudo journalctl -u "$SERVICE" -n 15 --no-pager
        ;;

    logs)
        log "Logs en tiempo real — Ctrl+C para salir"
        sudo journalctl -u "$SERVICE" -f
        ;;

    logs-n)
        N="${2:-50}"
        sudo journalctl -u "$SERVICE" -n "$N" --no-pager
        ;;

    balance)
        log "Balance del portfolio:"
        cd "$INSTALL_DIR"
        "$PYTHON" -m scripts.run status
        ;;

    operations)
        N="${2:-30}"
        log "Últimas $N operaciones:"
        cd "$INSTALL_DIR"
        "$PYTHON" -m scripts.run operations --tail "$N"
        ;;

    scan-once)
        # Corre un ciclo manual. Detiene el servicio si está corriendo.
        MODE="${2:-paper}"
        RUNNING=false
        if sudo systemctl is-active --quiet "$SERVICE" 2>/dev/null; then
            RUNNING=true
            warn "Deteniendo servicio para correr ciclo manual..."
            sudo systemctl stop "$SERVICE"
        fi

        log "Corriendo un ciclo en modo $MODE (con verbose)..."
        cd "$INSTALL_DIR"
        "$PYTHON" -m scripts.run --verbose weather --mode "$MODE" --once

        if $RUNNING; then
            log "Reiniciando servicio..."
            sudo systemctl start "$SERVICE"
        fi
        ;;

    update)
        # git pull (como el usuario actual) + reinstalar deps + reiniciar
        log "Deteniendo servicio..."
        sudo systemctl stop "$SERVICE" 2>/dev/null || true

        log "Actualizando código..."
        cd "$INSTALL_DIR"
        git pull

        log "Reinstalando dependencias..."
        "$INSTALL_DIR/.venv/bin/pip" install -e . -q

        log "Iniciando servicio..."
        sudo systemctl start "$SERVICE"
        sleep 1
        sudo systemctl status "$SERVICE" --no-pager -l
        log "Actualización completa."
        ;;

    reset-paper)
        # Resetear portfolio paper (empieza de cero)
        warn "Reseteando portfolio paper y logs..."
        RUNNING=false
        if sudo systemctl is-active --quiet "$SERVICE" 2>/dev/null; then
            RUNNING=true
            sudo systemctl stop "$SERVICE"
        fi
        cd "$INSTALL_DIR"
        "$PYTHON" -m scripts.run paper-reset -y --with-logs --bot-log
        if $RUNNING; then
            sudo systemctl start "$SERVICE"
        fi
        ;;

    enable)
        sudo systemctl enable "$SERVICE"
        log "Arranque automático habilitado."
        ;;

    disable)
        sudo systemctl disable "$SERVICE"
        warn "Arranque automático deshabilitado."
        ;;

    help)
        echo ""
        echo -e "${BOLD}╔══════════════════════════════════════════════════════════════╗${NC}"
        echo -e "${BOLD}║               BetBot — Comandos disponibles                 ║${NC}"
        echo -e "${BOLD}╚══════════════════════════════════════════════════════════════╝${NC}"
        echo ""
        echo -e "${BOLD}  SERVICIO${NC}"
        echo -e "  ${GREEN}start${NC}                  Iniciar el bot en segundo plano"
        echo -e "  ${GREEN}stop${NC}                   Detener el bot"
        echo -e "  ${GREEN}restart${NC}                Reiniciar (ej. después de cambiar .env)"
        echo -e "  ${GREEN}status${NC}                 Estado del servicio + últimas líneas de log"
        echo -e "  ${GREEN}enable${NC}                 Arrancar automáticamente al iniciar el servidor"
        echo -e "  ${GREEN}disable${NC}                Quitar el arranque automático"
        echo ""
        echo -e "${BOLD}  LOGS${NC}"
        echo -e "  ${GREEN}logs${NC}                   Ver logs en tiempo real  (Ctrl+C para salir)"
        echo -e "  ${GREEN}logs-n 100${NC}             Ver últimas 100 líneas"
        echo ""
        echo -e "${BOLD}  PORTFOLIO${NC}"
        echo -e "  ${GREEN}balance${NC}                Balance actual + posiciones abiertas"
        echo -e "  ${GREEN}operations${NC}             Últimas 30 operaciones"
        echo -e "  ${GREEN}operations 50${NC}          Últimas 50 operaciones"
        echo ""
        echo -e "${BOLD}  DESARROLLO / MANTENIMIENTO${NC}"
        echo -e "  ${GREEN}update${NC}                 git pull + reinstalar deps + reiniciar"
        echo -e "  ${GREEN}scan-once${NC}              Un ciclo manual en modo paper (para probar)"
        echo -e "  ${GREEN}scan-once live${NC}         Un ciclo manual en modo live"
        echo -e "  ${GREEN}reset-paper${NC}            Borrar portfolio paper y todos los logs (cero)"
        echo ""
        echo -e "  Uso: ${BOLD}./deploy/manage.sh <comando>${NC}"
        echo ""
        ;;

    *)
        echo -e "${RED}✗${NC} Comando desconocido: '$CMD'"
        echo -e "  Ejecuta ${BOLD}./deploy/manage.sh help${NC} para ver los comandos disponibles."
        exit 1
        ;;
esac
