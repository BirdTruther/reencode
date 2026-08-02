#!/bin/bash
# Re-encode TV shows down to HEVC (hardware) to save space.
# Usage: ./reencode.sh [--show "Name"] [--all] [--dry-run] [--max N] [--verify-only] [--setup]

set -uo pipefail

# shellcheck source=common.sh
source "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")/common.sh"

DRY_RUN=false
ALL=false
SINGLE_SHOW=""
VERIFY_ONLY=false
FORCE=false
MAX_FILES=0

usage() {
    cat <<EOF
Usage: $0 [OPTIONS]

Options:
  --all               Process every show in the library (default when no --show)
  --show "Name"       Process only the matching show
  --dry-run           Preview without encoding
  --verify-only       Check re-encoded files in temp dir
  --force             Re-encode even if temp file exists
  --max N             Stop after N files (testing)
  --setup             Re-run first-time config wizard
  --help              Show this help

Config is auto-generated at ${CONFIG_PATH} on first run. Edit it to change
library path, target height, quality, etc.
EOF
    exit 0
}

parse_args() {
    while [[ $# -gt 0 ]]; do
        case "$1" in
            --dry-run) DRY_RUN=true; shift ;;
            --all) ALL=true; shift ;;
            --show) SINGLE_SHOW="$2"; shift 2 ;;
            --verify-only) VERIFY_ONLY=true; shift ;;
            --force) FORCE=true; shift ;;
            --max) MAX_FILES="$2"; shift 2 ;;
            --setup) run_setup ;;
            --help) usage ;;
            *) log_err "Unknown option: $1"; usage ;;
        esac
    done
}

run_setup() {
    local tv va
    tv=$(detect_tv_dir || echo "")
    va=$(detect_vaapi || echo "")
    echo ""
    echo "Reencode setup (Enter keeps the default)"
    echo "────────────────────────────────────────────"
    read -rp "TV library dir [${tv:-none detected}]: " ans
    [[ -n "$ans" ]] && TV_DIR="$ans"
    [[ -z "$TV_DIR" && -z "$ans" ]] && TV_DIR="$tv"
    read -rp "Log dir [${LOG_DIR}]: " ans
    [[ -n "$ans" ]] && LOG_DIR="$ans"
    read -rp "Temp dir [${TEMP_DIR}]: " ans
    [[ -n "$ans" ]] && TEMP_DIR="$ans"
    read -rp "Target height [${TARGET_HEIGHT}]: " ans
    [[ -n "$ans" ]] && TARGET_HEIGHT="$ans"
    read -rp "Quality QP [${QUALITY}]: " ans
    [[ -n "$ans" ]] && QUALITY="$ans"
    read -rp "VAAPI device [${va:-auto}]: " ans
    [[ -n "$ans" ]] && VAAPI_DEVICE="$ans"

    mkdir -p "$(dirname "$CONFIG_PATH")"
    cat > "$CONFIG_PATH" <<EOF
# reencode.conf
TV_DIR="${TV_DIR}"
LOG_DIR="${LOG_DIR}"
TEMP_DIR="${TEMP_DIR}"
TARGET_HEIGHT=${TARGET_HEIGHT}
QUALITY=${QUALITY}
VAAPI_DEVICE="${VAAPI_DEVICE}"
EOF
    log_ok "Config written to $CONFIG_PATH"
    exit 0
}

setup_dirs() {
    mkdir -p "$TEMP_DIR" "$LOG_DIR"
}

get_video_height() {
    local file="$1"
    ffprobe -v error -select_streams v:0 -show_entries stream=height -of csv=p=0 "$file" 2>/dev/null | head -1
}

get_video_codec() {
    local file="$1"
    ffprobe -v error -select_streams v:0 -show_entries stream=codec_name -of csv=p=0 "$file" 2>/dev/null | head -1
}

get_file_size_mb() {
    local file="$1"
    local size_bytes
    size_bytes=$(stat -c%s "$file" 2>/dev/null || echo 0)
    echo $(( size_bytes / 1024 / 1024 ))
}

encode_file() {
    local input="$1"
    local filename base newbase ext outfile logfile height codec original_size_mb
    filename=$(basename "$input")
    base="${filename%.*}"
    ext="${filename##*.}"
    newbase=$(build_newbase "$base")
    outfile="${TEMP_DIR}/${newbase}.mkv"
    logfile="${LOG_DIR}/${base}.log"
    original_size_mb=$(get_file_size_mb "$input")

    height=$(get_video_height "$input")
    if [[ -z "$height" || "$height" -le "$TARGET_HEIGHT" ]]; then
        log_warn "SKIP (already <= ${TARGET_HEIGHT}p): $filename [${height:-?}p, ${original_size_mb}MB]"
        return 0
    fi

    codec=$(get_video_codec "$input")

    if [[ "$DRY_RUN" == true ]]; then
        local target_size_mb=$(( original_size_mb * 60 / 100 ))
        log "[DRY-RUN] Would encode: $filename [${height}p, ${codec}, ${original_size_mb}MB] -> ~${target_size_mb}MB"
        return 0
    fi

    if [[ -f "$outfile" ]] && [[ "$FORCE" != true ]]; then
        log_warn "SKIP (already encoded): $filename -> exists in temp"
        return 0
    fi

    log "Encoding: $filename [${height}p, ${codec}, ${original_size_mb}MB]"
    log "  Output: $outfile"

    local start_time
    start_time=$(date +%s)

    if ffmpeg -hide_banner -loglevel warning -stats \
        -vaapi_device "${VAAPI_DEVICE}" \
        -i "$input" \
        -vf "scale=-2:${TARGET_HEIGHT},format=nv12,hwupload" \
        -c:v hevc_vaapi -qp "${QUALITY}" \
        -c:a copy \
        -c:s copy \
        "$outfile" 2>"$logfile"; then

        local end_time elapsed new_size_mb savings
        end_time=$(date +%s)
        elapsed=$(( end_time - start_time ))
        new_size_mb=$(get_file_size_mb "$outfile")
        savings=0
        [[ "$original_size_mb" -gt 0 ]] && savings=$(( (original_size_mb - new_size_mb) * 100 / original_size_mb ))

        log_ok "Encoded: $filename -> ${new_size_mb}MB (${savings}% saved, ${elapsed}s)"

        if ffprobe -v error -select_streams v:0 -show_entries stream=height "$outfile" 2>/dev/null | grep -q "$TARGET_HEIGHT"; then
            log_ok "Verified: $outfile is ${TARGET_HEIGHT}p"
            return 0
        else
            log_err "VERIFY FAILED: Output doesn't have expected resolution"
            rm -f "$outfile"
            return 1
        fi
    else
        log_err "FAILED: $filename (see $logfile)"
        rm -f "$outfile"
        return 1
    fi
}

replace_original() {
    local input="$1"
    local filename base newbase ext outfile dir newpath original_size_mb new_size_mb savings
    filename=$(basename "$input")
    base="${filename%.*}"
    ext="${filename##*.}"
    newbase=$(build_newbase "$base")
    outfile="${TEMP_DIR}/${newbase}.mkv"

    if [[ ! -f "$outfile" ]]; then
        log_warn "No re-encoded file for: $filename"
        return 0
    fi

    original_size_mb=$(get_file_size_mb "$input")
    new_size_mb=$(get_file_size_mb "$outfile")

    if [[ "$DRY_RUN" == true ]]; then
        log "[DRY-RUN] Would replace: $input (${original_size_mb}MB) <- ${outfile} (${new_size_mb}MB)"
        return 0
    fi

    if ! ffprobe -v error -select_streams v:0 -show_entries stream=height "$outfile" 2>/dev/null | grep -q "$TARGET_HEIGHT"; then
        log_err "VERIFY FAILED: Not replacing $filename"
        return 1
    fi

    dir=$(dirname "$input")
    newpath="${dir}/${newbase}.${ext}"
    mv "$input" "${input}.bak"
    mv "$outfile" "$newpath"
    rm -f "${input}.bak"

    savings=0
    [[ "$original_size_mb" -gt 0 ]] && savings=$(( (original_size_mb - new_size_mb) * 100 / original_size_mb ))
    log_ok "Replaced: ${filename} -> ${newbase}.${ext} (${original_size_mb}MB -> ${new_size_mb}MB, ${savings}% saved)"
}

video_exts() {
    local d="$1"
    find "$d" -type f \( -iname "*.mkv" -o -iname "*.mp4" -o -iname "*.avi" -o -iname "*.m4v" -o -iname "*.ts" \) -print0 | sort -Vz
}

# Returns 0 if any file in the dir is taller than the target height
show_has_work() {
    local f h
    while IFS= read -r -d '' f; do
        h=$(get_video_height "$f")
        if [[ -n "$h" && "$h" -gt "$TARGET_HEIGHT" ]]; then
            return 0
        fi
    done < <(video_exts "$1")
    return 1
}

process_show() {
    local show_dir="$1"
    local show_name
    show_name=$(basename "$show_dir")

    log "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
    log "Processing: $show_name"
    log "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"

    local total_files encoded_files skipped_files failed_files total_saved
    total_files=0; encoded_files=0; skipped_files=0; failed_files=0; total_saved=0

    local -a file_list=()
    while IFS= read -r -d '' f; do
        file_list+=("$f")
    done < <(video_exts "$show_dir")

    local total_available=${#file_list[@]}
    log "Found $total_available video files"

    for file in "${file_list[@]}"; do
        total_files=$((total_files + 1))
        local filename
        filename=$(basename "$file")

        log "  [$total_files/$total_available] Processing: $filename"

        if encode_file "$file"; then
            local height
            height=$(get_video_height "$file")
            if [[ -n "$height" && "$height" -gt "$TARGET_HEIGHT" ]]; then
                encoded_files=$((encoded_files + 1))

                if [[ "$DRY_RUN" != true ]]; then
                    local newbase outfile original_size_mb new_size_mb
                    newbase=$(build_newbase "${filename%.*}")
                    outfile="${TEMP_DIR}/${newbase}.mkv"
                    if [[ -f "$outfile" ]]; then
                        original_size_mb=$(get_file_size_mb "$file")
                        new_size_mb=$(get_file_size_mb "$outfile")
                        total_saved=$((total_saved + original_size_mb - new_size_mb))
                    fi
                fi
            else
                skipped_files=$((skipped_files + 1))
            fi
        else
            failed_files=$((failed_files + 1))
        fi

        if [[ "$MAX_FILES" -gt 0 && "$total_files" -ge "$MAX_FILES" ]]; then
            log "Reached max files limit ($MAX_FILES), stopping."
            break
        fi
    done

    log "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
    log "Summary for $show_name:"
    log "  Total files scanned: $total_files"
    log "  Files encoded: $encoded_files"
    log "  Files skipped: $skipped_files"
    log "  Failed: $failed_files"
    if [[ "$DRY_RUN" != true && $total_saved -gt 0 ]]; then
        log_ok "  Space saved: ${total_saved}MB (~$(( total_saved / 1024 ))GB)"
    fi
    log "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
}

process_replacement_phase() {
    local show_dir="$1"
    local show_name
    show_name=$(basename "$show_dir")

    log "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
    log "Replacing originals: $show_name"
    log "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"

    local -a file_list=()
    while IFS= read -r -d '' f; do
        file_list+=("$f")
    done < <(video_exts "$show_dir")

    for file in "${file_list[@]}"; do
        replace_original "$file"
    done
}

process_all() {
    for dir in "${TV_DIR}"/*/; do
        [[ -d "$dir" ]] || continue
        local base_name
        base_name=$(basename "$dir")
        if ! show_has_work "$dir"; then
            log_warn "Nothing to do for: $base_name"
            continue
        fi
        process_show "$dir"
        [[ "$MAX_FILES" -eq 0 ]] && process_replacement_phase "$dir"
    done
}

find_show_dir() {
    for dir in "${TV_DIR}"/*/; do
        [[ -d "$dir" ]] || continue
        if [[ "$(basename "$dir")" == *"$SINGLE_SHOW"* ]]; then
            echo "$dir"
            return 0
        fi
    done
    return 1
}

main() {
    parse_args "$@"
    load_config

    if [[ -z "$TV_DIR" ]]; then
        log_err "TV_DIR not set. Run '$0 --setup' to configure."
        exit 1
    fi

    setup_dirs

    if [[ "$VERIFY_ONLY" == true ]]; then
        log "Verification mode: checking re-encoded files in $TEMP_DIR"
        local count=0 valid=0
        for f in "$TEMP_DIR"/*.mkv; do
            [[ -f "$f" ]] || continue
            count=$((count + 1))
            local height size_mb
            height=$(get_video_height "$f")
            size_mb=$(get_file_size_mb "$f")
            if [[ -n "$height" && "$height" -eq "$TARGET_HEIGHT" ]]; then
                log_ok "$(basename "$f") [${height}p, ${size_mb}MB]"
                valid=$((valid + 1))
            else
                log_err "$(basename "$f") [${height:-UNKNOWN}p, ${size_mb}MB]"
            fi
        done
        log "Verified: $valid/$count files valid"
        return 0
    fi

    log "TV Re-encoder"
    log "Target: >${TARGET_HEIGHT}p -> ${TARGET_HEIGHT}p HEVC VAAPI (QP ${QUALITY})"
    log "TV dir: $TV_DIR"
    log "Temp dir: $TEMP_DIR"
    log "Dry run: $DRY_RUN"
    [[ "$MAX_FILES" -gt 0 ]] && log "Max files: $MAX_FILES"
    log ""

    if [[ -n "$SINGLE_SHOW" ]]; then
        local show_dir
        show_dir=$(find_show_dir)
        if [[ -n "$show_dir" ]]; then
            process_show "$show_dir"
            [[ "$MAX_FILES" -eq 0 ]] && process_replacement_phase "$show_dir"
        else
            log_err "Show not found: $SINGLE_SHOW"
            exit 1
        fi
    else
        process_all
    fi
}

main "$@"
