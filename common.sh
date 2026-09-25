#!/bin/bash
# Shared config, detection, and helpers for reencode.sh, encodetv and the dashboard.

SCRIPT_DIR=$(cd "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")" && pwd)

CONFIG_PATH="${REENCODE_CONFIG:-$SCRIPT_DIR/reencode.conf}"
CACHE_FILE="${SCRIPT_DIR}/.encodetv_cache"

# Defaults (overridden by config file)
LIBRARIES=()
TV_DIR=""      # pre-LIBRARIES configs; still honoured
LOG_DIR="${REENCODE_LOG_DIR:-${HOME}/reencode_logs}"
TEMP_DIR="${REENCODE_TEMP_DIR:-/tmp/reencode}"
TARGET_HEIGHT=720
QUALITY=32
ENCODER="vaapi"
VAAPI_DEVICE=""
HW_DECODE="auto"
ENCODE_HOURS=""

VIDEO_FIND_EXPR=( \( -iname "*.mkv" -o -iname "*.mp4" -o -iname "*.avi" -o -iname "*.m4v" -o -iname "*.ts" \) )

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
CYAN='\033[0;36m'
NC='\033[0m'

log()      { printf "[%s] %s\n" "$(date '+%Y-%m-%d %H:%M:%S')" "$*"; }
log_ok()   { printf "[%s] %b%s%b %s\n" "$(date '+%Y-%m-%d %H:%M:%S')" "${GREEN}" "OK" "${NC}" "$*"; }
log_warn() { printf "[%s] %b%s%b %s\n" "$(date '+%Y-%m-%d %H:%M:%S')" "${YELLOW}" "WARN" "${NC}" "$*"; }
log_err()  { printf "[%s] %b%s%b %s\n" "$(date '+%Y-%m-%d %H:%M:%S')" "${RED}" "ERR" "${NC}" "$*" >&2; }

# Print the first existing dir (from the given names under common mount points)
# that contains video files.
detect_library() {
    local name d
    for name in "$@"; do
        for d in \
            "/media/$name" "/data/$name" \
            "/mnt/plex_media/$name" "/mnt/media/$name" "/mnt/library/$name" \
            /media/*/"$name" /mnt/*/"$name" /data/*/"$name" /storage/*/"$name" \
            "$HOME/$name" "$HOME/Videos/$name" "$HOME/Plex/$name"; do
            [[ -d "$d" ]] || continue
            if [[ -n "$(find "$d" -mindepth 1 -maxdepth 3 -type f "${VIDEO_FIND_EXPR[@]}" -print -quit 2>/dev/null)" ]]; then
                echo "$d"
                return 0
            fi
        done
    done
    return 1
}

detect_tv_dir()    { detect_library TV tv "TV Shows" Shows; }
detect_movie_dir() { detect_library Movies movies Films; }

detect_libraries() {
    local d
    d=$(detect_tv_dir) && echo "$d"
    d=$(detect_movie_dir) && echo "$d"
    return 0
}

detect_vaapi() {
    for d in /dev/dri/renderD128 /dev/dri/renderD129; do
        [[ -e "$d" ]] && { echo "$d"; return 0; }
    done
    return 1
}

# Write the current settings to $CONFIG_PATH.
write_config() {
    mkdir -p "$(dirname "$CONFIG_PATH")"
    cat > "$CONFIG_PATH" <<EOF
# reencode.conf - edit and re-run, or delete to regenerate.

# Media libraries to process. Each is a folder of show or movie folders,
# e.g. TV/<Show>/Season 1/*.mkv or Movies/<Movie (Year)>/*.mkv
LIBRARIES=(
$(for l in "${LIBRARIES[@]}"; do printf '  "%s"\n' "$l"; done))

# Where per-episode logs go
LOG_DIR="${LOG_DIR}"

# Scratch space for encoded files (use an SSD if you have one)
TEMP_DIR="${TEMP_DIR}"

# Downscale target: only files taller than this get re-encoded
TARGET_HEIGHT=${TARGET_HEIGHT}

# Quality, lower = better. Used as -qp (vaapi), -cq (nvenc) or -crf (software).
# vaapi: 20 ~ near-lossless, 32 = good/small. software/nvenc: try 24-28.
QUALITY=${QUALITY}

# Encoder: vaapi (Intel/AMD GPU), nvenc (NVIDIA GPU) or software (libx265, CPU, slow)
ENCODER="${ENCODER}"

# VAAPI device (auto-detected if left empty; ignored by other encoders)
VAAPI_DEVICE="${VAAPI_DEVICE}"

# Decode on the GPU too (vaapi/nvenc): auto = try GPU, fall back to CPU per file; no = CPU only
HW_DECODE="${HW_DECODE}"

# Dashboard only: hours it may encode, e.g. "01:00-08:00". Empty = any time.
ENCODE_HOURS="${ENCODE_HOURS}"
EOF
}

load_config() {
    if [[ -f "$CONFIG_PATH" ]]; then
        # shellcheck disable=SC1090
        source "$CONFIG_PATH"
        if [[ ${#LIBRARIES[@]} -eq 0 && -n "$TV_DIR" ]]; then
            LIBRARIES=("$TV_DIR")
        fi
        if [[ "$ENCODER" == "vaapi" && -z "$VAAPI_DEVICE" ]]; then
            VAAPI_DEVICE=$(detect_vaapi || echo "")
        fi
        return 0
    fi

    # No config yet: auto-detect and write one.
    mapfile -t LIBRARIES < <(detect_libraries)
    VAAPI_DEVICE=$(detect_vaapi || echo "")
    write_config

    log_ok "No config found - wrote $CONFIG_PATH"
    log "  Libraries detected: ${LIBRARIES[*]:-<none - edit config>}"
    log "  VAAPI device: ${VAAPI_DEVICE:-<none - edit config>}"
}

# Compute the output name for a re-encoded file. Takes basename without ext.
# Replaces whatever height marker is in the name; appends one if absent.
build_newbase() {
    local base="$1" nb target="${TARGET_HEIGHT}p"
    nb=$(sed -E "s/\b(2160p|4K|UHD|1440p|1440x1080|1920x1080|1280x1080|1080[pi]?)\b/${target}/Ig" <<< "$base")
    if [[ "$nb" == "$base" ]]; then
        nb="${base} ${target}"
    fi
    echo "$nb"
}

# Print every title folder (show or movie) across all libraries, NUL-separated.
list_titles() {
    local lib dir
    for lib in "${LIBRARIES[@]}"; do
        for dir in "${lib%/}"/*/; do
            [[ -d "$dir" ]] && printf '%s\0' "${dir%/}"
        done
    done
}
