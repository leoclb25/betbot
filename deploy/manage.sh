#!/usr/bin/env bash
# =============================================================================
# manage.sh — Gestión de BetBot en EC2
#
# Uso desde la raíz del repo:
#   ./deploy/manage.sh start              ← inicia ambos bots
#   ./deploy/manage.sh start weather      ← solo weather
#   ./deploy/manage.sh start crypto       ← solo crypto
#   ./deploy/manage.sh stop               ← detiene ambos
#   ./deploy/manage.sh stop crypto        ← solo crypto
#   ./deploy/manage.sh restart            ← reinicia ambos
#   ./deploy/manage.sh status             ← estado de ambos
#   ./deploy/manage.sh logs               ← logs de ambos en tiempo real
#   ./deploy/manage.sh logs weather       ← logs solo weather
#   ./deploy/manage.sh logs crypto        ← logs solo crypto
#   ./deploy/manage.sh balance            ← balance de ambos portfolios
#   ./deploy/manage.sh balance weather    ← solo weather
#   ./deploy/manage.sh balance crypto     ← solo crypto
#   ./deploy/manage.sh operations         ← últimas 30 operaciones (ambos)
#   ./deploy/manage.sh scan-once          ← ciclo manual (ambos)
#   ./deploy/manage.sh scan-once crypto   ← ciclo manual crypto
#   ./deploy/manage.sh update             ← git pull + reinstalar + reiniciar
#   ./deploy/manage.sh reset-paper        ← borrar portfolios paper (ambos)
#   ./deploy/manage.sh reset-paper crypto ← solo crypto
#
#   ./deploy/manage.sh dashboard-install  ← instala panel web de balance (systemd)
#   ./deploy/manage.sh dashboard-start    ← http://127.0.0.1:8765 (detrás de nginx en prod)
#   ./deploy/manage.sh dashboard-stop
# =============================================================================

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
INSTALL_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
PYTHON="$INSTALL_DIR/.venv/bin/python"

GREEN='\033[0;32m'; YELLOW='\033[1;33m'; RED='\033[0;31m'; BOLD='\033[1m'; NC='\033[0m'
log()  { echo -e "${GREEN}▶${NC} $*"; }
warn() { echo -e "${YELLOW}⚠${NC} $*"; }
err()  { echo -e "${RED}✗${NC} $*"; exit 1; }

[[ -f "$PYTHON" ]] || err "Venv no encontrado en $INSTALL_DIR/.venv — corre setup_ec2.sh primero"

CMD="${1:-help}"
BOT="${2:-all}"   # weather | crypto | all

# Resuelve la lista de servicios a operar
_services() {
    case "$1" in
        weather) echo "betbot-weather" ;;
        crypto)  echo "betbot-crypto"  ;;
        all)     echo "betbot-weather betbot-crypto" ;;
        *)       err "Bot desconocido: '$1'. Usa: weather | crypto | all" ;;
    esac
}

case "$CMD" in

    start)
        for SVC in $(_services "$BOT"); do
            log "Iniciando $SVC..."
            sudo systemctl start "$SVC"
        done
        sleep 1
        for SVC in $(_services "$BOT"); do
            sudo systemctl status "$SVC" --no-pager -l
        done
        ;;

    stop)
        for SVC in $(_services "$BOT"); do
            warn "Deteniendo $SVC..."
            sudo systemctl stop "$SVC"
        done
        log "Listo."
        ;;

    restart)
        for SVC in $(_services "$BOT"); do
            warn "Reiniciando $SVC..."
            sudo systemctl restart "$SVC"
        done
        sleep 1
        for SVC in $(_services "$BOT"); do
            sudo systemctl status "$SVC" --no-pager -l
        done
        ;;

    status)
        for SVC in $(_services "$BOT"); do
            echo ""
            sudo systemctl status "$SVC" --no-pager -l
            echo ""
            log "Últimas 10 líneas de $SVC:"
            sudo journalctl -u "$SVC" -n 10 --no-pager
        done
        ;;

    logs)
        if [[ "$BOT" == "all" ]]; then
            log "Logs en tiempo real (weather + crypto) — Ctrl+C para salir"
            sudo journalctl -u betbot-weather -u betbot-crypto -f
        else
            SVC="betbot-$BOT"
            log "Logs en tiempo real ($SVC) — Ctrl+C para salir"
            sudo journalctl -u "$SVC" -f
        fi
        ;;

    logs-n)
        N="${2:-50}"
        BOT_ARG="${3:-all}"
        for SVC in $(_services "$BOT_ARG"); do
            sudo journalctl -u "$SVC" -n "$N" --no-pager
        done
        ;;

    balance)
        log "Balance del portfolio:"
        cd "$INSTALL_DIR"
        case "$BOT" in
            all)     "$PYTHON" -m scripts.run status ;;
            weather) "$PYTHON" -m scripts.run status --bot weather ;;
            crypto)  "$PYTHON" -m scripts.run status --bot crypto ;;
        esac
        ;;

    operations)
        N="${2:-30}"
        log "Últimas $N operaciones:"
        cd "$INSTALL_DIR"
        "$PYTHON" -m scripts.run operations --tail "$N"
        ;;

    scan-once)
        MODE="${3:-paper}"
        for SVC in $(_services "$BOT"); do
            RUNNING=false
            if sudo systemctl is-active --quiet "$SVC" 2>/dev/null; then
                RUNNING=true
                warn "Deteniendo $SVC para ciclo manual..."
                sudo systemctl stop "$SVC"
            fi

            BOT_NAME="${SVC#betbot-}"
            log "Corriendo un ciclo $BOT_NAME en modo $MODE..."
            cd "$INSTALL_DIR"
            "$PYTHON" -m scripts.run --verbose "$BOT_NAME" --mode "$MODE" --once

            if $RUNNING; then
                log "Reiniciando $SVC..."
                sudo systemctl start "$SVC"
            fi
        done
        ;;

    update)
        log "Deteniendo bots..."
        for SVC in $(_services "$BOT"); do
            sudo systemctl stop "$SVC" 2>/dev/null || true
        done

        log "Actualizando código..."
        cd "$INSTALL_DIR"
        git pull

        log "Reinstalando dependencias..."
        "$INSTALL_DIR/.venv/bin/pip" install -e . -q

        log "Iniciando bots..."
        for SVC in $(_services "$BOT"); do
            sudo systemctl start "$SVC"
        done
        sleep 1
        for SVC in $(_services "$BOT"); do
            sudo systemctl status "$SVC" --no-pager -l
        done
        log "Actualización completa."
        ;;

    reset-paper)
        warn "Reseteando portfolio paper..."
        for SVC in $(_services "$BOT"); do
            if sudo systemctl is-active --quiet "$SVC" 2>/dev/null; then
                sudo systemctl stop "$SVC"
            fi
        done

        cd "$INSTALL_DIR"
        case "$BOT" in
            all)     "$PYTHON" -m scripts.run paper-reset -y --with-logs ;;
            weather) "$PYTHON" -m scripts.run paper-reset --bot weather -y --with-logs ;;
            crypto)  "$PYTHON" -m scripts.run paper-reset --bot crypto  -y --with-logs ;;
        esac

        for SVC in $(_services "$BOT"); do
            sudo systemctl start "$SVC" 2>/dev/null || true
        done
        ;;

    install)
        log "Instalando servicios systemd..."
        for BOT_NAME in weather crypto; do
            SRC="$INSTALL_DIR/deploy/betbot-${BOT_NAME}.service"
            DEST="/etc/systemd/system/betbot-${BOT_NAME}.service"
            [[ -f "$SRC" ]] || err "No se encontró $SRC"
            sudo cp "$SRC" "$DEST"
            sudo sed -i "s|__INSTALL_DIR__|$INSTALL_DIR|g" "$DEST"
            sudo sed -i "s|__SERVICE_USER__|$(whoami)|g" "$DEST"
            log "Instalado: $DEST"
        done
        sudo systemctl daemon-reload
        log "daemon-reload OK. Ahora podés correr: ./deploy/manage.sh start"
        ;;

    dashboard-install)
        SRC="$INSTALL_DIR/deploy/betbot-status-web.service"
        DEST="/etc/systemd/system/betbot-status-web.service"
        [[ -f "$SRC" ]] || err "No se encontró $SRC"
        sudo cp "$SRC" "$DEST"
        sudo sed -i "s|__INSTALL_DIR__|$INSTALL_DIR|g" "$DEST"
        sudo sed -i "s|__SERVICE_USER__|$(whoami)|g" "$DEST"
        log "Instalado: $DEST"
        sudo systemctl daemon-reload
        log "daemon-reload OK. Iniciá con: ./deploy/manage.sh dashboard-start"
        log "Opcional: nginx → ver deploy/nginx-status-web.conf.example"
        ;;

    dashboard-start)
        log "Iniciando betbot-status-web..."
        sudo systemctl start betbot-status-web
        sleep 1
        sudo systemctl status betbot-status-web --no-pager -l
        ;;

    dashboard-stop)
        warn "Deteniendo betbot-status-web..."
        sudo systemctl stop betbot-status-web
        log "Listo."
        ;;

    dashboard-restart)
        warn "Reiniciando betbot-status-web..."
        sudo systemctl restart betbot-status-web
        sleep 1
        sudo systemctl status betbot-status-web --no-pager -l
        ;;

    dashboard-status)
        sudo systemctl status betbot-status-web --no-pager -l
        echo ""
        log "Últimas 15 líneas:"
        sudo journalctl -u betbot-status-web -n 15 --no-pager
        ;;

    enable)
        for SVC in $(_services "$BOT"); do
            sudo systemctl enable "$SVC"
            log "Arranque automático habilitado: $SVC"
        done
        ;;

    disable)
        for SVC in $(_services "$BOT"); do
            sudo systemctl disable "$SVC"
            warn "Arranque automático deshabilitado: $SVC"
        done
        ;;

    help)
        echo ""
        echo -e "${BOLD}╔══════════════════════════════════════════════════════════════════╗${NC}"
        echo -e "${BOLD}║                 BetBot — Comandos disponibles                   ║${NC}"
        echo -e "${BOLD}╚══════════════════════════════════════════════════════════════════╝${NC}"
        echo ""
        echo -e "  Uso: ${BOLD}./deploy/manage.sh <comando> [weather|crypto|all]${NC}"
        echo -e "  El segundo argumento es el bot a operar. Default: ${BOLD}all${NC}"
        echo ""
        echo -e "${BOLD}  SERVICIO${NC}"
        echo -e "  ${GREEN}start${NC}  [bot]          Iniciar bot(s) en segundo plano"
        echo -e "  ${GREEN}stop${NC}   [bot]          Detener bot(s)"
        echo -e "  ${GREEN}restart${NC} [bot]         Reiniciar (ej. después de cambiar .env)"
        echo -e "  ${GREEN}status${NC} [bot]          Estado del servicio + últimas líneas de log"
        echo -e "  ${GREEN}install${NC}               Copiar .service a systemd + daemon-reload (correr una vez)
  ${GREEN}enable${NC} [bot]          Arrancar automáticamente al iniciar el servidor"
        echo -e "  ${GREEN}disable${NC} [bot]         Quitar el arranque automático"
        echo ""
        echo -e "${BOLD}  LOGS${NC}"
        echo -e "  ${GREEN}logs${NC}   [bot]          Ver logs en tiempo real  (Ctrl+C para salir)"
        echo -e "  ${GREEN}logs-n${NC} <N> [bot]      Ver últimas N líneas"
        echo ""
        echo -e "${BOLD}  PORTFOLIO${NC}"
        echo -e "  ${GREEN}balance${NC} [bot]         Balance actual + posiciones abiertas"
        echo -e "  ${GREEN}operations${NC} [N]        Últimas N operaciones (default: 30)"
        echo ""
        echo -e "${BOLD}  PANEL WEB (balance)${NC}"
        echo -e "  ${GREEN}dashboard-install${NC}     Instalar servicio systemd del panel HTTP"
        echo -e "  ${GREEN}dashboard-start${NC}       Iniciar panel (puerto 8765, ver .service)"
        echo -e "  ${GREEN}dashboard-stop${NC}        Detener panel"
        echo -e "  ${GREEN}dashboard-restart${NC}     Reiniciar panel"
        echo -e "  ${GREEN}dashboard-status${NC}      Estado + últimas líneas de journald"
        echo ""
        echo -e "${BOLD}  DESARROLLO / MANTENIMIENTO${NC}"
        echo -e "  ${GREEN}update${NC} [bot]          git pull + reinstalar deps + reiniciar"
        echo -e "  ${GREEN}scan-once${NC} [bot] [mode] Un ciclo manual en modo paper/live"
        echo -e "  ${GREEN}reset-paper${NC} [bot]     Borrar portfolio paper y logs (empieza de cero)"
        echo ""
        echo -e "${BOLD}  Ejemplos:${NC}"
        echo -e "  ${YELLOW}./deploy/manage.sh start${NC}                # inicia weather + crypto"
        echo -e "  ${YELLOW}./deploy/manage.sh start crypto${NC}         # solo crypto"
        echo -e "  ${YELLOW}./deploy/manage.sh stop weather${NC}         # detiene solo weather"
        echo -e "  ${YELLOW}./deploy/manage.sh logs crypto${NC}          # logs del crypto bot"
        echo -e "  ${YELLOW}./deploy/manage.sh balance${NC}              # balance de ambos"
        echo -e "  ${YELLOW}./deploy/manage.sh reset-paper crypto${NC}   # resetea solo crypto"
        echo -e "  ${YELLOW}./deploy/manage.sh scan-once crypto paper${NC} # ciclo manual crypto"
        echo -e "  ${YELLOW}./deploy/manage.sh dashboard-install && ./deploy/manage.sh dashboard-start${NC}"
        echo -e "    ${YELLOW}# nginx: deploy/nginx-status-web.conf.example (quitar sites-enabled/default)${NC}"
        echo ""
        ;;

    *)
        echo -e "${RED}✗${NC} Comando desconocido: '$CMD'"
        echo -e "  Ejecuta ${BOLD}./deploy/manage.sh help${NC} para ver los comandos disponibles."
        exit 1
        ;;
esac
