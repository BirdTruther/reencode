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
TARGET_HEIGHT=720   # pre-profile configs; now set per title from its profile
QUALITY=32
# Profiles: one for TV libraries, one for movie libraries (see write_config).
TV_HEIGHT=""
TV_QUALITY=""
TV_KEEP_4K=""
MOVIES_HEIGHT=""
MOVIES_QUALITY=""
MOVIES_KEEP_4K=""
LIBRARY_PROFILES=()
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

# Profile for each library above, in the same order: tv or movies
LIBRARY_PROFILES=(
$(for i in "${!LIBRARIES[@]}"; do printf '  "%s"\n' "$(library_profile "${LIBRARIES[i]}")"; done))

# Profiles. HEIGHT: anything taller is shrunk to this. QUALITY: lower = better,
# bigger (vaapi: 28-32, nvenc/software: 24-28). KEEP_4K: no = shrink 4K too,
# if-other-version = leave 4K alone when the folder has another version of it,
# yes = never touch 4K files.
TV_HEIGHT=${TV_HEIGHT}
TV_QUALITY=${TV_QUALITY}
TV_KEEP_4K="${TV_KEEP_4K}"
MOVIES_HEIGHT=${MOVIES_HEIGHT}
MOVIES_QUALITY=${MOVIES_QUALITY}
MOVIES_KEEP_4K="${MOVIES_KEEP_4K}"

# Where per-episode logs go
LOG_DIR="${LOG_DIR}"

# Scratch space for encoded files (use an SSD if you have one)
TEMP_DIR="${TEMP_DIR}"

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

# Fill in profile settings missing from older configs (which had a single
# TARGET_HEIGHT/QUALITY for everything).
finalize_profiles() {
    TV_HEIGHT="${TV_HEIGHT:-$TARGET_HEIGHT}"
    TV_QUALITY="${TV_QUALITY:-$QUALITY}"
    TV_KEEP_4K="${TV_KEEP_4K:-no}"
    MOVIES_HEIGHT="${MOVIES_HEIGHT:-$TARGET_HEIGHT}"
    MOVIES_QUALITY="${MOVIES_QUALITY:-$QUALITY}"
    MOVIES_KEEP_4K="${MOVIES_KEEP_4K:-if-other-version}"
    OVERRIDES_FILE="$(dirname "$CONFIG_PATH")/title_overrides.tsv"
}

load_config() {
    if [[ -f "$CONFIG_PATH" ]]; then
        # shellcheck disable=SC1090
        source "$CONFIG_PATH"
        if [[ ${#LIBRARIES[@]} -eq 0 && -n "$TV_DIR" ]]; then
            LIBRARIES=("$TV_DIR")
        fi
        finalize_profiles
        if [[ "$ENCODER" == "vaapi" && -z "$VAAPI_DEVICE" ]]; then
            VAAPI_DEVICE=$(detect_vaapi || echo "")
        fi
        return 0
    fi

    # No config yet: auto-detect and write one.
    mapfile -t LIBRARIES < <(detect_libraries)
    VAAPI_DEVICE=$(detect_vaapi || echo "")
    TV_HEIGHT=720
    MOVIES_HEIGHT=1080
    finalize_profiles
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

# tv or movies, from LIBRARY_PROFILES, else guessed from the folder name.
library_profile() {
    local lib="${1%/}" i name
    for i in "${!LIBRARIES[@]}"; do
        if [[ "${LIBRARIES[i]%/}" == "$lib" ]]; then
            case "${LIBRARY_PROFILES[i]:-}" in
                tv|movies) echo "${LIBRARY_PROFILES[i]}"; return 0 ;;
            esac
            break
        fi
    done
    name=$(basename "$lib")
    name=${name,,}
    if [[ "$name" == *movie* || "$name" == *film* ]]; then echo movies; else echo tv; fi
}

# Per-title setting from OVERRIDES_FILE (lines: "<height|skip><TAB><title path>").
title_override() {
    [[ -f "${OVERRIDES_FILE:-}" ]] || return 0
    local val path
    while IFS=$'\t' read -r val path; do
        if [[ "$path" == "${1%/}" ]]; then
            echo "$val"
            return 0
        fi
    done < "$OVERRIDES_FILE"
}

# Set TARGET_HEIGHT, QUALITY, KEEP_4K, PROFILE_NAME and TITLE_SKIP for a title folder.
apply_profile() {
    local dir="${1%/}" ov
    PROFILE_NAME=$(library_profile "$(dirname "$dir")")
    if [[ "$PROFILE_NAME" == movies ]]; then
        TARGET_HEIGHT=$MOVIES_HEIGHT; QUALITY=$MOVIES_QUALITY; KEEP_4K=$MOVIES_KEEP_4K
    else
        TARGET_HEIGHT=$TV_HEIGHT; QUALITY=$TV_QUALITY; KEEP_4K=$TV_KEEP_4K
    fi
    TITLE_SKIP=false
    ov=$(title_override "$dir")
    case "$ov" in
        skip) TITLE_SKIP=true ;;
        [0-9]*) TARGET_HEIGHT=$ov ;;
    esac
}

# Files whose names differ only by a resolution marker are versions of the same
# video ("Film 2160p.mkv", "Film 1080p.mkv", "Film 720p.mkv"). Keep in sync with
# group_key() in dashboard.py.
group_key() {
    # LC_ALL=C: plain ASCII rules whatever the locale, same as the Python side.
    # shellcheck disable=SC2018,SC2019
    LC_ALL=C sed -E 's/\b([0-9]{3,4}[pi]|[0-9]{3,4}x[0-9]{3,4}|4K|UHD)\b//Ig; s/[ ._-]{2,}/ /g; s/^[ ._-]+//; s/[ ._-]+$//' <<< "$1" \
        | LC_ALL=C tr 'A-Z' 'a-z'
}

get_video_height() {
    ffprobe -v error -select_streams v:0 -show_entries stream=height -of csv=p=0 "$1" 2>/dev/null | head -1
}

# Decide what to do with each file of a title (after apply_profile). Sets
#   PLAN[file]    encode | ok | keep4k | extra | skip | unknown
#   WHY[file]     a short human-readable reason
#   HEIGHTS[file] video height
# Keep the rules in sync with plan_files() in dashboard.py.
declare -gA PLAN=() WHY=() HEIGHTS=()
plan_title() {
    local f h key c
    local -A key_of=() versions=() small=() best_of=() best_h=()
    PLAN=(); WHY=(); HEIGHTS=()
    for f in "$@"; do
        h=$(get_video_height "$f")
        [[ "$h" =~ ^[0-9]+$ ]] || h=""
        HEIGHTS["$f"]=$h
        key=$(group_key "$(basename "${f%.*}")")
        key_of["$f"]=$key
        c=${versions["$key"]:-0}
        versions["$key"]=$((c + 1))
        if [[ -n "$h" ]] && [ "$h" -le "$TARGET_HEIGHT" ]; then
            small["$key"]=$h
        fi
    done
    for f in "$@"; do
        h=${HEIGHTS["$f"]}
        key=${key_of["$f"]}
        c=${versions["$key"]}
        if [[ -z "$h" ]]; then
            PLAN["$f"]=unknown; WHY["$f"]="can't read the video"
        elif [ "$h" -le "$TARGET_HEIGHT" ]; then
            PLAN["$f"]=ok; WHY["$f"]="already ${h}p"
        elif [[ "$TITLE_SKIP" == true ]]; then
            PLAN["$f"]=skip; WHY["$f"]="set to never shrink"
        elif [ "$h" -ge 2160 ] && [[ "$KEEP_4K" == yes ]]; then
            PLAN["$f"]=keep4k; WHY["$f"]="keeping 4K"
        elif [ "$h" -ge 2160 ] && [[ "$KEEP_4K" == if-other-version ]] && [ "$c" -gt 1 ]; then
            PLAN["$f"]=keep4k; WHY["$f"]="keeping 4K, another version exists"
        elif [[ -n "${small["$key"]:-}" ]]; then
            PLAN["$f"]=extra; WHY["$f"]="a ${small["$key"]}p version already exists"
        else
            PLAN["$f"]=encode; WHY["$f"]="shrink to ${TARGET_HEIGHT}p"
            if [[ -z "${best_of["$key"]:-}" ]] || [ "$h" -gt "${best_h["$key"]}" ]; then
                best_of["$key"]=$f
                best_h["$key"]=$h
            fi
        fi
    done
    # Only the best version of each video is shrunk, so versions can't collide.
    for f in "$@"; do
        key=${key_of["$f"]}
        if [[ "${PLAN["$f"]}" == encode && "${best_of["$key"]}" != "$f" ]]; then
            PLAN["$f"]=extra; WHY["$f"]="another version of this is being shrunk"
        fi
    done
}
