#!/bin/bash
# Shared config, detection, and helpers for reencode.sh and encodetv.

SCRIPT_DIR=$(cd "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")" && pwd)

CONFIG_PATH="${REENCODE_CONFIG:-$SCRIPT_DIR/reencode.conf}"
CACHE_FILE="${SCRIPT_DIR}/.encodetv_cache"

# Defaults (overridden by config file)
TV_DIR=""
LOG_DIR="${HOME}/reencode_logs"
TEMP_DIR="/tmp/reencode"
TARGET_HEIGHT=720
QUALITY=32
VAAPI_DEVICE=""

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
CYAN='\033[0;36m'
NC='\033[0m'

log()      { printf "[%s] %s\n" "$(date '+%Y-%m-%d %H:%M:%S')" "$*"; }
log_ok()   { printf "[%s] %b%s%b %s\n" "$(date '+%Y-%m-%d %H:%M:%S')" "${GREEN}" "OK" "${NC}" "$*"; }
log_warn() { printf "[%s] %b%s%b %s\n" "$(date '+%Y-%m-%d %H:%M:%S')" "${YELLOW}" "WARN" "${NC}" "$*"; }
log_err()  { printf "[%s] %b%s%b %s\n" "$(date '+%Y-%m-%d %H:%M:%S')" "${RED}" "ERR" "${NC}" "$*" >&2; }

detect_tv_dir() {
    local d
    for d in \
        /mnt/plex_media/TV /mnt/tv /mnt/media/TV /mnt/library/TV \
        /media/*/TV /mnt/*/TV /data/*/TV /storage/*/TV \
        "$HOME/TV" "$HOME/Videos/TV" "$HOME/Plex/TV"; do
        [[ -d "$d" ]] || continue
        if [[ -n "$(find "$d" -mindepth 1 -maxdepth 3 -type f \
            \( -iname "*.mkv" -o -iname "*.mp4" -o -iname "*.avi" -o -iname "*.m4v" -o -iname "*.ts" \) \
            -print -quit 2>/dev/null)" ]]; then
            echo "$d"
            return 0
        fi
    done
    return 1
}

detect_vaapi() {
    for d in /dev/dri/renderD128 /dev/dri/renderD129; do
        [[ -e "$d" ]] && { echo "$d"; return 0; }
    done
    return 1
}

load_config() {
    local defaults
    defaults=$(detect_tv_dir || true)

    if [[ -f "$CONFIG_PATH" ]]; then
        # shellcheck disable=SC1090
        source "$CONFIG_PATH"
        return 0
    fi

    # No config yet: auto-detect and write one.
    TV_DIR="$defaults"
    VAAPI_DEVICE=$(detect_vaapi || echo "")

    cat > "$CONFIG_PATH" <<EOF
# reencode.conf - generated automatically on first run.
# Edit and re-run, or delete to regenerate.

# Path to your TV library (parent dir of your show folders)
TV_DIR="${TV_DIR}"

# Where per-episode logs go
LOG_DIR="${LOG_DIR}"

# Scratch space for encoded files (use an SSD if you have one)
TEMP_DIR="${TEMP_DIR}"

# Downscale target: only files taller than this get re-encoded
TARGET_HEIGHT=${TARGET_HEIGHT}

# VAAPI quality, lower = better (20 ~ near-lossless, 32 = good/small)
QUALITY=${QUALITY}

# Hardware encode device (auto-detected if left empty)
VAAPI_DEVICE="${VAAPI_DEVICE}"
EOF

    log_ok "No config found - wrote $CONFIG_PATH"
    log "  TV dir detected: ${TV_DIR:-<none - edit config>}"
    log "  VAAPI device: ${VAAPI_DEVICE:-<none - edit config>}"
}

# Compute the output name for a re-encoded file. Takes basename without ext.
# Replaces whatever height marker is in the name; appends one if absent.
build_newbase() {
    local base="$1" nb target="${TARGET_HEIGHT}p"
    nb=$(sed -E "s/(1440x1080|1920x1080|1280x1080|1080p|1080)/${target}/Ig" <<< "$base")
    if [[ "$nb" == "$base" ]]; then
        nb="${base} ${target}"
    fi
    echo "$nb"
}
