#!/usr/bin/env bash
# =============================================================================
# manage.sh
# Script de gestión del bot en producción (sin Docker).
# Útil para operaciones rápidas desde SSH sin recordar los comandos systemd.
#
# Uso:
#   ./deploy/manage.sh status
#   ./deploy/manage.sh start
#   ./deploy/manage.sh stop
#   ./deploy/manage.sh restart
#   ./deploy/manage.sh logs
#   ./deploy/manage.sh balance
#   ./deploy/manage.sh operations
#   ./deploy/manage.sh update
# =============================================================================

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
INSTALL_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
SERVICE_NAME="betbot-weather"
VENV_PYTHON="$INSTALL_DIR/.venv/bin/python"
BOT_USER="betbot"

GREEN='\033[0;32m'; YELLOW='\033[1;33m'; RED='\033[0;31m'; NC='\033[0m'
log()  { echo -e "${GREEN}▶${NC} $*"; }
warn() { echo -e "${YELLOW}⚠${NC} $*"; }
err()  { echo -e "${RED}✗${NC} $*"; exit 1; }

CMD="${1:-status}"

case "$CMD" in

    status)
        echo ""
        systemctl status "$SERVICE_NAME" --no-pager
        echo ""
        log "Último ciclo de logs:"
        journalctl -u "$SERVICE_NAME" -n 20 --no-pager
        ;;

    start)
        log "Iniciando $SERVICE_NAME..."
        sudo systemctl start "$SERVICE_NAME"
        sleep 2
        systemctl status "$SERVICE_NAME" --no-pager
        ;;

    stop)
        warn "Deteniendo $SERVICE_NAME..."
        sudo systemctl stop "$SERVICE_NAME"
        log "Servicio detenido."
        ;;

    restart)
        warn "Reiniciando $SERVICE_NAME..."
        sudo systemctl restart "$SERVICE_NAME"
        sleep 2
        systemctl status "$SERVICE_NAME" --no-pager
        ;;

    logs)
        # Ver logs en tiempo real (Ctrl+C para salir)
        log "Logs en tiempo real (Ctrl+C para salir):"
        journalctl -u "$SERVICE_NAME" -f
        ;;

    logs-tail)
        N="${2:-50}"
        journalctl -u "$SERVICE_NAME" -n "$N" --no-pager
        ;;

    balance)
        log "Balance del portfolio:"
        cd "$INSTALL_DIR"
        sudo -u "$BOT_USER" "$VENV_PYTHON" -m scripts.run status
        ;;

    operations)
        N="${2:-30}"
        log "Últimas $N operaciones:"
        cd "$INSTALL_DIR"
        sudo -u "$BOT_USER" "$VENV_PYTHON" -m scripts.run operations --tail "$N"
        ;;

    scan-once)
        # Correr un ciclo manual sin el servicio (útil para debugging)
        MODE="${2:-paper}"
        warn "Corriendo un ciclo manual en modo $MODE..."
        sudo systemctl stop "$SERVICE_NAME" 2>/dev/null || true
        cd "$INSTALL_DIR"
        sudo -u "$BOT_USER" "$VENV_PYTHON" -m scripts.run --verbose weather --mode "$MODE" --once
        log "Ciclo completado. Reiniciando servicio..."
        sudo systemctl start "$SERVICE_NAME"
        ;;

    update)
        # Actualizar código desde git y reiniciar
        log "Actualizando código..."
        sudo systemctl stop "$SERVICE_NAME"
        cd "$INSTALL_DIR"
        sudo -u "$BOT_USER" git pull
        sudo -u "$BOT_USER" "$INSTALL_DIR/.venv/bin/pip" install -e . -q
        sudo systemctl start "$SERVICE_NAME"
        sleep 2
        systemctl status "$SERVICE_NAME" --no-pager
        log "Actualización completa."
        ;;

    enable)
        log "Habilitando arranque automático..."
        sudo systemctl enable "$SERVICE_NAME"
        ;;

    disable)
        warn "Deshabilitando arranque automático..."
        sudo systemctl disable "$SERVICE_NAME"
        ;;

    tail-log-file)
        # Ver el archivo de log del bot (distinto del journal de systemd)
        tail -f "$INSTALL_DIR/data/logs/bot.log"
        ;;

    *)
        echo "Uso: $0 {status|start|stop|restart|logs|balance|operations|scan-once|update}"
        echo ""
        echo "  status        – Estado del servicio + últimos logs"
        echo "  start         – Iniciar el bot"
        echo "  stop          – Detener el bot"
        echo "  restart       – Reiniciar el bot (ej. después de cambiar .env)"
        echo "  logs          – Ver logs en tiempo real (Ctrl+C para salir)"
        echo "  logs-tail N   – Ver últimas N líneas de logs (default 50)"
        echo "  balance       – Ver balance del portfolio"
        echo "  operations N  – Ver últimas N operaciones (default 30)"
        echo "  scan-once     – Correr un ciclo manual (detiene y reinicia el servicio)"
        echo "  update        – git pull como usuario betbot + pip install -e + restart (no uses git pull como ubuntu)"
        echo "  enable        – Habilitar arranque automático con el servidor"
        echo "  disable       – Deshabilitar arranque automático"
        exit 1
        ;;
esac
