#!/bin/bash
# Re-encode TV shows and movies down to HEVC (hardware or software) to save space.
# Usage: ./reencode.sh [--show "Name"] [--all] [--dry-run] [--max N] [--verify-only] [--setup]

set -uo pipefail

# shellcheck source=common.sh
source "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")/common.sh"

DRY_RUN=false
ALL=false
SINGLE_SHOW=""
SHOW_DIR=""
VERIFY_ONLY=false
FORCE=false
ALLOW_FAILURES=false
SETUP=false
MAX_FILES=0

# Partial output currently being written; removed if we're interrupted.
CURRENT_PART=""

usage() {
    cat <<EOF
Usage: $0 [OPTIONS]

Options:
  --all               Process every show in the library (default when no --show)
  --show "Name"       Process only the matching show/movie folder (exact name wins, else substring)
  --dir PATH          Process exactly this show/movie folder
  --dry-run           Preview without encoding
  --verify-only       Check re-encoded files in temp dir
  --force             Re-encode even if temp file exists
  --allow-failures    Replace the episodes that succeeded even if others failed
  --max N             Stop after N files (testing; originals are not replaced)
  --setup             Re-run first-time config wizard
  --help              Show this help

Config is auto-generated at ${CONFIG_PATH} on first run. Edit it to change
library path, target height, quality, encoder, etc.
EOF
    exit 0
}

parse_args() {
    while [[ $# -gt 0 ]]; do
        case "$1" in
            --dry-run) DRY_RUN=true; shift ;;
            --all) ALL=true; shift ;;
            --show|--dir|--max)
                if [[ $# -lt 2 ]]; then
                    log_err "$1 needs a value"
                    exit 1
                fi
                case "$1" in
                    --show) SINGLE_SHOW="$2" ;;
                    --dir) SHOW_DIR="$2" ;;
                    --max) MAX_FILES="$2" ;;
                esac
                shift 2 ;;
            --verify-only) VERIFY_ONLY=true; shift ;;
            --force) FORCE=true; shift ;;
            --allow-failures) ALLOW_FAILURES=true; shift ;;
            --setup) SETUP=true; shift ;;
            --help|-h) usage ;;
            *) log_err "Unknown option: $1"; usage ;;
        esac
    done
}

run_setup() {
    local va ans
    va=$(detect_vaapi || echo "")
    [[ ${#LIBRARIES[@]} -eq 0 ]] && mapfile -t LIBRARIES < <(detect_libraries)
    [[ -z "$VAAPI_DEVICE" ]] && VAAPI_DEVICE="$va"
    echo ""
    echo "Reencode setup (Enter keeps the current value)"
    echo "────────────────────────────────────────────"
    echo "Libraries are folders of show/movie folders (e.g. /mnt/media/TV /mnt/media/Movies)."
    read -rp "Libraries, separated by ';' [${LIBRARIES[*]:-none detected}]: " ans
    [[ -n "$ans" ]] && IFS=';' read -ra LIBRARIES <<< "$ans"
    read -rp "Log dir [${LOG_DIR}]: " ans
    [[ -n "$ans" ]] && LOG_DIR="$ans"
    read -rp "Temp dir [${TEMP_DIR}]: " ans
    [[ -n "$ans" ]] && TEMP_DIR="$ans"
    read -rp "Target height [${TARGET_HEIGHT}]: " ans
    [[ -n "$ans" ]] && TARGET_HEIGHT="$ans"
    read -rp "Encoder: vaapi, nvenc or software [${ENCODER}]: " ans
    [[ -n "$ans" ]] && ENCODER="$ans"
    read -rp "Quality [${QUALITY}]: " ans
    [[ -n "$ans" ]] && QUALITY="$ans"
    if [[ "$ENCODER" == "vaapi" ]]; then
        read -rp "VAAPI device [${VAAPI_DEVICE:-auto}]: " ans
        [[ -n "$ans" ]] && VAAPI_DEVICE="$ans"
    fi

    write_config
    log_ok "Config written to $CONFIG_PATH"
    exit 0
}

setup_dirs() {
    mkdir -p "$TEMP_DIR" "$LOG_DIR"
}

get_video_height() {
    ffprobe -v error -select_streams v:0 -show_entries stream=height -of csv=p=0 "$1" 2>/dev/null | head -1
}

get_video_codec() {
    ffprobe -v error -select_streams v:0 -show_entries stream=codec_name -of csv=p=0 "$1" 2>/dev/null | head -1
}

get_duration() {
    ffprobe -v error -show_entries format=duration -of csv=p=0 "$1" 2>/dev/null | head -1 | grep -E '^[0-9.]+$'
}

get_file_size_mb() {
    local size_bytes
    size_bytes=$(stat -c%s "$1" 2>/dev/null || echo 0)
    echo $(( size_bytes / 1024 / 1024 ))
}

# Sets ENC_PRE (before -i), ENC_VF (filter chain) and ENC_V (video codec args)
# for CPU decoding, and GPU_PRE/GPU_VF for decoding + scaling on the GPU too.
setup_encoder() {
    GPU_PRE=()
    GPU_VF=""
    case "$ENCODER" in
        vaapi)
            ENC_PRE=(-vaapi_device "$VAAPI_DEVICE")
            ENC_VF="scale=-2:${TARGET_HEIGHT},format=nv12,hwupload"
            GPU_PRE=(-hwaccel vaapi -hwaccel_device "$VAAPI_DEVICE" -hwaccel_output_format vaapi)
            GPU_VF="scale_vaapi=w=-2:h=${TARGET_HEIGHT}:format=nv12"
            ENC_V=(-c:v hevc_vaapi -qp "$QUALITY")
            ENC_NAME=hevc_vaapi ;;
        nvenc)
            ENC_PRE=()
            ENC_VF="scale=-2:${TARGET_HEIGHT}"
            GPU_PRE=(-hwaccel cuda -hwaccel_output_format cuda)
            GPU_VF="scale_cuda=-2:${TARGET_HEIGHT}"
            ENC_V=(-c:v hevc_nvenc -preset p5 -rc vbr -cq "$QUALITY" -b:v 0)
            ENC_NAME=hevc_nvenc ;;
        software)
            ENC_PRE=()
            ENC_VF="scale=-2:${TARGET_HEIGHT}"
            ENC_V=(-c:v libx265 -preset medium -crf "$QUALITY" -x265-params log-level=error)
            ENC_NAME=libx265 ;;
        *)
            log_err "Unknown ENCODER '$ENCODER' (use vaapi, nvenc or software)"
            exit 1 ;;
    esac
}

check_encoder() {
    if ! command -v ffmpeg >/dev/null || ! command -v ffprobe >/dev/null; then
        log_err "ffmpeg/ffprobe not found in PATH"
        exit 1
    fi
    local encoders
    encoders=$(ffmpeg -hide_banner -encoders 2>/dev/null)
    if ! grep -qw -- "$ENC_NAME" <<< "$encoders"; then
        log_err "Your ffmpeg has no $ENC_NAME encoder (ENCODER=$ENCODER)"
        exit 1
    fi
    if [[ "$ENCODER" == "vaapi" && ! -e "$VAAPI_DEVICE" ]]; then
        log_err "VAAPI device not found: '${VAAPI_DEVICE}'. Set VAAPI_DEVICE or ENCODER in $CONFIG_PATH"
        exit 1
    fi
}

# Confirm an encode is complete: right height and same length as the source.
verify_output() {
    local src="$1" out="$2" h sd od
    h=$(get_video_height "$out")
    if [[ "$h" != "$TARGET_HEIGHT" ]]; then
        log_err "VERIFY FAILED: $(basename "$out") is ${h:-?}p, expected ${TARGET_HEIGHT}p"
        return 1
    fi
    od=$(get_duration "$out")
    if [[ -z "$od" ]]; then
        log_err "VERIFY FAILED: can't read duration of $(basename "$out")"
        return 1
    fi
    sd=$(get_duration "$src")
    if [[ -n "$sd" ]] && ! awk -v a="$sd" -v b="$od" \
        'BEGIN { d = a - b; if (d < 0) d = -d; exit !(d <= 2 || d <= a * 0.01) }'; then
        log_err "VERIFY FAILED: $(basename "$out") is ${od}s long, source is ${sd}s (truncated?)"
        return 1
    fi
    return 0
}

NO_SAVINGS_LIST() { echo "${LOG_DIR}/.no_savings"; }

is_no_savings() {
    [[ -f "$(NO_SAVINGS_LIST)" ]] && grep -Fxq -- "$1" "$(NO_SAVINGS_LIST)"
}

# Returns 0 = encoded (or ready in temp), 1 = failed, 2 = skipped.
encode_file() {
    local input="$1"
    local filename base newbase ext outfile part logfile height codec original_size_mb duration
    filename=$(basename "$input")
    base="${filename%.*}"
    ext="${filename##*.}"
    newbase=$(build_newbase "$base")
    outfile="${TEMP_DIR}/${newbase}.mkv"
    part="${outfile}.part"
    logfile="${LOG_DIR}/${base}.log"
    original_size_mb=$(get_file_size_mb "$input")

    height=$(get_video_height "$input")
    if [[ -z "$height" || "$height" -le "$TARGET_HEIGHT" ]]; then
        log_warn "SKIP (already <= ${TARGET_HEIGHT}p): $filename [${height:-?}p, ${original_size_mb}MB]"
        return 2
    fi

    if is_no_savings "$input"; then
        log_warn "SKIP (re-encoding didn't save space last time): $filename"
        return 2
    fi

    codec=$(get_video_codec "$input")

    if [[ "$DRY_RUN" == true ]]; then
        local target_size_mb=$(( original_size_mb * 60 / 100 ))
        log "[DRY-RUN] Would encode: $filename [${height}p, ${codec}, ${original_size_mb}MB] -> ~${target_size_mb}MB"
        return 0
    fi

    if [[ -f "$outfile" && "$FORCE" != true ]]; then
        if verify_output "$input" "$outfile"; then
            log_warn "SKIP (already encoded): $filename -> exists in temp"
            return 0
        fi
        log_warn "Discarding bad temp file, re-encoding: $outfile"
        rm -f "$outfile"
    fi

    duration=$(get_duration "$input")
    log "Encoding: $filename [${height}p, ${codec}, ${original_size_mb}MB]"
    log "  Duration: ${duration:-?}s"
    log "  Output: $outfile"

    # mp4 text subs (mov_text) can't be copied into mkv; convert them to srt.
    local -a sub_args=()
    case "${ext,,}" in
        mp4|m4v) sub_args=(-c:s srt) ;;
    esac

    local -a progress_args=()
    [[ -n "${REENCODE_PROGRESS:-}" ]] && progress_args=(-progress "$REENCODE_PROGRESS")

    # Try decoding on the GPU first; some sources (old codecs, odd profiles)
    # can't be, so fall back to CPU decoding for just that file.
    local -a modes=(cpu) pre=()
    local mode vf ok=false start_time
    [[ "$HW_DECODE" != "no" && -n "$GPU_VF" ]] && modes=(gpu cpu)

    start_time=$(date +%s)
    CURRENT_PART="$part"
    : > "$logfile"
    for mode in "${modes[@]}"; do
        if [[ "$mode" == gpu ]]; then
            pre=("${GPU_PRE[@]}"); vf="$GPU_VF"
        else
            pre=("${ENC_PRE[@]}"); vf="$ENC_VF"
        fi
        log "  Decode: ${mode^^}"
        rm -f "$part"
        [[ -n "${REENCODE_PROGRESS:-}" ]] && rm -f "$REENCODE_PROGRESS"
        echo "=== ${mode} decode ===" >> "$logfile"
        if ffmpeg -nostdin -hide_banner -loglevel warning -stats -y \
            "${pre[@]}" \
            -i "$input" \
            -map 0:v:0 -map '0:a?' -map '0:s?' -map '0:t?' \
            -c copy \
            -vf "$vf" \
            "${ENC_V[@]}" \
            "${sub_args[@]}" \
            -max_muxing_queue_size 1024 \
            "${progress_args[@]}" \
            -f matroska "$part" 2>>"$logfile"; then
            ok=true
            break
        fi
        [[ "$mode" == gpu ]] && log_warn "  GPU decode failed for this file, retrying with CPU decode"
    done

    if [[ "$ok" == true ]]; then

        if ! verify_output "$input" "$part"; then
            rm -f "$part"
            CURRENT_PART=""
            return 1
        fi
        mv -f "$part" "$outfile"
        CURRENT_PART=""

        local elapsed new_size_mb savings=0
        elapsed=$(( $(date +%s) - start_time ))
        new_size_mb=$(get_file_size_mb "$outfile")
        [[ "$original_size_mb" -gt 0 ]] && savings=$(( (original_size_mb - new_size_mb) * 100 / original_size_mb ))

        log_ok "Encoded: $filename -> ${new_size_mb}MB (${savings}% saved, ${elapsed}s)"
        log_ok "Verified: $(basename "$outfile") is ${TARGET_HEIGHT}p, full length"
        return 0
    else
        log_err "FAILED: $filename (see $logfile)"
        # Show the most telling lines from the last attempt.
        awk '/^=== .* decode ===$/ { buf = ""; next } { buf = buf $0 "\n" } END { printf "%s", buf }' "$logfile" 2>/dev/null \
            | sed 's/\r/\n/g' | grep -iE 'cannot|error|invalid|not supported|unsupported|no such|failed|denied' \
            | grep -v '^frame=' | head -n 3 | while IFS= read -r l; do log_err "  $l"; done
        rm -f "$part"
        CURRENT_PART=""
        return 1
    fi
}

# Rename sidecar files (subtitles, nfo) that share the video's name, so players
# still match them to the renamed video.
rename_sidecars() {
    local dir="$1" base="$2" newbase="$3" f suffix
    for f in "${dir}/${base}".*; do
        [[ -f "$f" ]] || continue
        suffix="${f#"${dir}/${base}"}"
        case "${suffix##*.}" in
            srt|ass|ssa|sub|idx|vtt|sup|nfo|SRT|ASS|SSA|SUB|IDX|VTT|SUP|NFO) ;;
            *) continue ;;
        esac
        [[ -e "${dir}/${newbase}${suffix}" ]] && continue
        mv -- "$f" "${dir}/${newbase}${suffix}" && log "  Renamed sidecar: $(basename "$f")"
    done
}

replace_original() {
    local input="$1"
    local filename base newbase outfile dir newpath tmpdest original_size_mb new_size_mb savings
    filename=$(basename "$input")
    base="${filename%.*}"
    newbase=$(build_newbase "$base")
    outfile="${TEMP_DIR}/${newbase}.mkv"

    if [[ ! -f "$outfile" ]]; then
        return 0
    fi

    original_size_mb=$(get_file_size_mb "$input")
    new_size_mb=$(get_file_size_mb "$outfile")

    if [[ "$DRY_RUN" == true ]]; then
        log "[DRY-RUN] Would replace: $input (${original_size_mb}MB) <- ${outfile} (${new_size_mb}MB)"
        return 0
    fi

    if ! verify_output "$input" "$outfile"; then
        log_err "Not replacing $filename"
        return 1
    fi

    if [[ $(stat -c%s "$outfile") -ge $(stat -c%s "$input") ]]; then
        log_warn "No savings for $filename (${original_size_mb}MB -> ${new_size_mb}MB), keeping original"
        echo "$input" >> "$(NO_SAVINGS_LIST)"
        rm -f "$outfile"
        return 0
    fi

    dir=$(dirname "$input")
    newpath="${dir}/${newbase}.mkv"
    if [[ -e "$newpath" && "$newpath" != "$input" ]]; then
        log_err "Not replacing $filename: $newpath already exists"
        return 1
    fi

    # Copy next to the original first (TEMP_DIR is often another disk), then
    # rename into place. The original is only deleted once the new file is there.
    tmpdest="${dir}/.${newbase}.mkv.part"
    CURRENT_PART="$tmpdest"
    if ! cp -- "$outfile" "$tmpdest" || [[ $(stat -c%s "$tmpdest") != $(stat -c%s "$outfile") ]]; then
        log_err "Copy to library failed for $filename (disk full?), original untouched"
        rm -f "$tmpdest"
        CURRENT_PART=""
        return 1
    fi
    if ! mv -f -- "$tmpdest" "$newpath"; then
        log_err "Rename failed for $filename, original untouched"
        rm -f "$tmpdest"
        CURRENT_PART=""
        return 1
    fi
    CURRENT_PART=""
    [[ "$newpath" != "$input" ]] && rm -f -- "$input"
    rm -f -- "$outfile"
    rename_sidecars "$dir" "$base" "$newbase"

    savings=0
    [[ "$original_size_mb" -gt 0 ]] && savings=$(( (original_size_mb - new_size_mb) * 100 / original_size_mb ))
    log_ok "Replaced: ${filename} -> ${newbase}.mkv (${original_size_mb}MB -> ${new_size_mb}MB, ${savings}% saved)"
}

video_exts() {
    find "$1" -type f "${VIDEO_FIND_EXPR[@]}" ! -name '.*' -print0 | sort -Vz
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

# Returns 1 if any episode failed.
process_show() {
    local show_dir="$1"
    local show_name
    show_name=$(basename "$show_dir")

    log "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
    log "Processing: $show_name"
    log "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"

    local total_files=0 encoded_files=0 skipped_files=0 failed_files=0 total_saved=0

    local -a file_list=()
    while IFS= read -r -d '' f; do
        file_list+=("$f")
    done < <(video_exts "$show_dir")

    local total_available=${#file_list[@]}
    log "Found $total_available video files"

    local file filename rc newbase outfile
    for file in "${file_list[@]}"; do
        total_files=$((total_files + 1))
        filename=$(basename "$file")

        log "  [$total_files/$total_available] Processing: $filename"

        encode_file "$file"
        rc=$?
        case "$rc" in
            0)
                encoded_files=$((encoded_files + 1))
                if [[ "$DRY_RUN" != true ]]; then
                    newbase=$(build_newbase "${filename%.*}")
                    outfile="${TEMP_DIR}/${newbase}.mkv"
                    if [[ -f "$outfile" ]]; then
                        total_saved=$((total_saved + $(get_file_size_mb "$file") - $(get_file_size_mb "$outfile")))
                    fi
                fi ;;
            2) skipped_files=$((skipped_files + 1)) ;;
            *) failed_files=$((failed_files + 1)) ;;
        esac

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

    [[ "$failed_files" -eq 0 ]]
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

    local file failed=0
    for file in "${file_list[@]}"; do
        replace_original "$file" || failed=$((failed + 1))
    done
    [[ "$failed" -eq 0 ]]
}

# Encode a show, then swap in the new files if every episode succeeded.
run_show() {
    local dir="$1" ok=true
    process_show "$dir" || ok=false
    [[ "$MAX_FILES" -gt 0 ]] && return 0
    if [[ "$ok" == true || "$ALLOW_FAILURES" == true ]]; then
        process_replacement_phase "$dir" || ok=false
    else
        log_warn "Not replacing originals for $(basename "$dir"): some episodes failed."
        log_warn "  Finished encodes stay in $TEMP_DIR and are reused next run."
        log_warn "  Use --allow-failures to replace the ones that worked."
    fi
    [[ "$ok" == true ]]
}

process_all() {
    local dir base_name rc=0
    while IFS= read -r -d '' dir; do
        base_name=$(basename "$dir")
        if ! show_has_work "$dir"; then
            log_warn "Nothing to do for: $base_name"
            continue
        fi
        run_show "$dir" || rc=1
    done < <(list_titles)
    return "$rc"
}

find_show_dir() {
    local dir lib
    for lib in "${LIBRARIES[@]}"; do
        if [[ -d "${lib%/}/${SINGLE_SHOW}" ]]; then
            echo "${lib%/}/${SINGLE_SHOW}"
            return 0
        fi
    done
    while IFS= read -r -d '' dir; do
        if [[ "$(basename "$dir")" == *"$SINGLE_SHOW"* ]]; then
            echo "$dir"
            return 0
        fi
    done < <(list_titles)
    return 1
}

on_interrupt() {
    [[ -n "$CURRENT_PART" ]] && rm -f "$CURRENT_PART"
    log_warn "Interrupted - partial output removed, originals untouched."
    exit 130
}

main() {
    parse_args "$@"
    load_config
    [[ "$SETUP" == true ]] && run_setup

    if [[ ${#LIBRARIES[@]} -eq 0 && -z "$SHOW_DIR" ]]; then
        log_err "No libraries configured. Run '$0 --setup' or edit $CONFIG_PATH."
        exit 1
    fi

    setup_dirs
    trap on_interrupt INT TERM

    if [[ "$VERIFY_ONLY" == true ]]; then
        log "Verification mode: checking re-encoded files in $TEMP_DIR"
        local count=0 valid=0 f height size_mb
        for f in "$TEMP_DIR"/*.mkv; do
            [[ -f "$f" ]] || continue
            count=$((count + 1))
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

    setup_encoder
    [[ "$DRY_RUN" == true ]] || check_encoder

    log "TV Re-encoder"
    log "Target: >${TARGET_HEIGHT}p -> ${TARGET_HEIGHT}p HEVC ${ENC_NAME} (quality ${QUALITY}, GPU decode: ${HW_DECODE})"
    log "Libraries: ${LIBRARIES[*]}"
    log "Temp dir: $TEMP_DIR"
    log "Dry run: $DRY_RUN"
    [[ "$MAX_FILES" -gt 0 ]] && log "Max files: $MAX_FILES"
    log ""

    if [[ -n "$SHOW_DIR" ]]; then
        if [[ ! -d "$SHOW_DIR" ]]; then
            log_err "Not a directory: $SHOW_DIR"
            exit 1
        fi
        run_show "${SHOW_DIR%/}"
    elif [[ -n "$SINGLE_SHOW" ]]; then
        local show_dir
        show_dir=$(find_show_dir)
        if [[ -n "$show_dir" ]]; then
            run_show "$show_dir"
        else
            log_err "Show not found: $SINGLE_SHOW"
            exit 1
        fi
    else
        process_all
    fi
}

main "$@"
