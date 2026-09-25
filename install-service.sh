#!/bin/bash
# Install the dashboard as a systemd service that starts on boot.
#
#   sudo ./install-service.sh                 # run as you (the sudo user), port 8686
#   sudo ./install-service.sh --port 9000 --user plex
#   ./install-service.sh --print              # just show the unit file
#   sudo ./install-service.sh --uninstall

set -euo pipefail

ARGS=("$@")
DIR=$(cd "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")" && pwd)
NAME=reencode-dashboard
UNIT="/etc/systemd/system/${NAME}.service"
PORT=8686
RUN_USER="${SUDO_USER:-$(id -un)}"
ACTION=install

while [[ $# -gt 0 ]]; do
    case "$1" in
        --port) PORT="$2"; shift 2 ;;
        --user) RUN_USER="$2"; shift 2 ;;
        --print) ACTION=print; shift ;;
        --uninstall) ACTION=uninstall; shift ;;
        -h|--help) sed -n '2,8p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
        *) echo "Unknown option: $1" >&2; exit 1 ;;
    esac
done

if [[ ! "$PORT" =~ ^[0-9]+$ ]]; then
    echo "--port must be a number" >&2
    exit 1
fi
if ! id "$RUN_USER" >/dev/null 2>&1; then
    echo "No such user: $RUN_USER" >&2
    exit 1
fi

unit() {
    local groups=() g python
    # video/render give access to /dev/dri for hardware encoding.
    for g in video render; do
        getent group "$g" >/dev/null && groups+=("$g")
    done
    python=$(command -v python3 || echo /usr/bin/python3)
    cat <<EOF
[Unit]
Description=Reencode dashboard
After=network-online.target remote-fs.target
Wants=network-online.target

[Service]
User=${RUN_USER}
${groups[*]:+SupplementaryGroups=${groups[*]}}
WorkingDirectory=${DIR}
ExecStart="${python}" "${DIR}/dashboard.py" --port ${PORT}
Restart=on-failure
# Let the dashboard stop a running encode cleanly (it removes partial files).
KillMode=mixed
TimeoutStopSec=45

[Install]
WantedBy=multi-user.target
EOF
}

if [[ "$ACTION" == print ]]; then
    unit
    exit 0
fi

if [[ $EUID -ne 0 ]]; then
    exec sudo "$0" "${ARGS[@]}"
fi

if [[ "$ACTION" == uninstall ]]; then
    systemctl disable --now "$NAME" 2>/dev/null || true
    rm -f "$UNIT"
    systemctl daemon-reload
    echo "Removed $NAME."
    exit 0
fi

if [[ "$RUN_USER" == root ]]; then
    echo "Note: running as root. Use --user <name> to run as the account that owns your media."
fi
if ! sudo -u "$RUN_USER" test -w "$DIR"; then
    echo "$RUN_USER can't write to $DIR (it stores its config there). Fix ownership or use --user." >&2
    exit 1
fi

unit > "$UNIT"
systemctl daemon-reload
systemctl enable "$NAME" >/dev/null 2>&1
systemctl restart "$NAME"
sleep 2

if ! systemctl is-active --quiet "$NAME"; then
    echo "The service failed to start. Check: journalctl -u $NAME -n 50" >&2
    exit 1
fi

ip=$(hostname -I 2>/dev/null | awk '{print $1}')
echo "Installed and started $NAME (starts on boot)."
echo "  Open:     http://${ip:-localhost}:${PORT}"
if [[ -f "$DIR/.dashboard_password" ]]; then
    echo "  Password: $(cat "$DIR/.dashboard_password")   (any username)"
fi
echo "  Logs:     journalctl -u $NAME -f"
echo "  Remove:   sudo $0 --uninstall"
