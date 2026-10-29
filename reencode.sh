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
CLEAR_NO_SAVINGS=false
FORCE=false
ALLOW_FAILURES=false
SETUP=false
PLAN_ONLY=false
MAX_FILES=0

# Circuit breaker state for the whole batch (not per title): consecutive
# failures, the error signature of the last one, and whether it tripped.
# Override the trip count with REENCODE_MAX_CONSEC_FAILURES if needed.
CONSEC_FAIL=0
CONSEC_SIG=""
CIRCUIT_TRIPPED=false
CIRCUIT_MAX="${REENCODE_MAX_CONSEC_FAILURES:-5}"
LAST_FAIL_SIG=""

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
  --plan              Print what would happen to each file (action, height, reason, path)
  --verify-only       Check re-encoded files in temp dir
  --clear-no-savings  Forget files that once didn't save space, then exit
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
            --plan) PLAN_ONLY=true; DRY_RUN=true; shift ;;
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
            --clear-no-savings) CLEAR_NO_SAVINGS=true; shift ;;
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
    if [[ -n "$ans" ]]; then
        IFS=';' read -ra LIBRARIES <<< "$ans"
        LIBRARY_PROFILES=()   # re-guess TV/Movies from the folder names
    fi
    read -rp "Log dir [${LOG_DIR}]: " ans
    [[ -n "$ans" ]] && LOG_DIR="$ans"
    read -rp "Temp dir [${TEMP_DIR}]: " ans
    [[ -n "$ans" ]] && TEMP_DIR="$ans"
    read -rp "TV resolution [${TV_HEIGHT}]: " ans
    [[ -n "$ans" ]] && TV_HEIGHT="$ans"
    read -rp "Movies resolution [${MOVIES_HEIGHT}]: " ans
    [[ -n "$ans" ]] && MOVIES_HEIGHT="$ans"
    read -rp "Encoder: vaapi, nvenc or software [${ENCODER}]: " ans
    [[ -n "$ans" ]] && ENCODER="$ans"
    read -rp "Quality [${TV_QUALITY}]: " ans
    [[ -n "$ans" ]] && TV_QUALITY="$ans" && MOVIES_QUALITY="$ans"
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

get_video_codec() {
    ffprobe -v error -select_streams "${2:-v:0}" -show_entries stream=codec_name -of csv=p=0 "$1" 2>/dev/null | head -1
}

get_duration() {
    ffprobe -v error -show_entries format=duration -of csv=p=0 "$1" 2>/dev/null | head -1 | grep -E '^[0-9.]+$'
}

get_file_size_mb() {
    local size_bytes
    size_bytes=$(stat -c%s "$1" 2>/dev/null || echo 0)
    echo $(( size_bytes / 1024 / 1024 ))
}

# Sets ENC_PRE (before -i) and ENC_V (video codec args) for CPU decoding, and
# GPU_PRE for decoding on the GPU too. Scale filters are per file: set_filters.
setup_encoder() {
    GPU_PRE=()
    case "$ENCODER" in
        vaapi)
            ENC_PRE=(-vaapi_device "$VAAPI_DEVICE")
            GPU_PRE=(-hwaccel vaapi -hwaccel_device "$VAAPI_DEVICE" -hwaccel_output_format vaapi)
            # -profile:v main is load-bearing, not cosmetic: without it
            # ffmpeg picks the profile from the filter output format, so a
            # 10-bit source used to become Main10 and fail on GPUs that
            # can decode Main10 but not encode it (Polaris/RX 580).
            # HDR note: this is NOT a proper HDR->SDR conversion. For SDR
            # 10-bit sources (most x265 10-bit packs, just used for less
            # banding) dropping to 8-bit is fine. For true HDR10/Dolby
            # Vision, there is no tonemapping here, so colours will not
            # be faithfully converted to SDR -- keep those 4K files, or
            # run a separate CPU tonemap pass (e.g. libplacebo), which
            # is out of scope for this VAAPI batch path.
            ENC_V=(-c:v hevc_vaapi -profile:v main -qp "$QUALITY")
            ENC_NAME=hevc_vaapi ;;
        nvenc)
            ENC_PRE=()
            GPU_PRE=(-hwaccel cuda -hwaccel_output_format cuda)
            ENC_V=(-c:v hevc_nvenc -preset p5 -rc vbr -cq "$QUALITY" -b:v 0)
            ENC_NAME=hevc_nvenc ;;
        software)
            ENC_PRE=()
            ENC_V=(-c:v libx265 -preset medium -crf "$QUALITY" -x265-params log-level=error)
            ENC_NAME=libx265 ;;
        *)
            log_err "Unknown ENCODER '$ENCODER' (use vaapi, nvenc or software)"
            exit 1 ;;
    esac
}

# Scale filters live in common.sh (set_filters), so tests can call them
# without running reencode.sh.
# Size a source should be shrunk to under the current profile (sets OUT_W, OUT_H).
output_dims() {
    probe_video "$1" || return 1
    read -r OUT_W OUT_H < <(target_dims "$V_W" "$V_H" "$V_DW" "$TARGET_HEIGHT")
}

# True if a per-file ffmpeg log shows the ENCODER failing to open, as
# opposed to the DECODER failing on the source. Only encoder failures
# match: a decode failure must still fall back to CPU decode.
is_encoder_failure() {
    grep -qiE 'no usable encoding entrypoint|error while opening encoder|could not open encoder' "$1"
}

# One-line error signature for a failed file's log, for the circuit
# breaker: failures only count as "the same" if this matches.
failure_signature() {
    local f="$1" line
    if grep -qi 'no usable encoding entrypoint' "$f"; then
        echo "vaapi-no-encoding-entrypoint"
    elif grep -qiE 'error while opening encoder|could not open encoder' "$f"; then
        echo "encoder-open-failed"
    else
        line=$(sed 's/\r/\n/g' "$f" 2>/dev/null \
            | grep -iE 'error|invalid|not supported|unsupported' | grep -v '^frame=' | head -n 1)
        [[ -n "$line" ]] && echo "other:${line:0:80}" || echo "unknown"
    fi
}

# Pre-flight: one 1-second encode with exactly the settings the batch
# will use (8-bit nv12, HEVC Main on the VAAPI device). If this fails,
# the batch would fail on every file, so abort before touching any of
# them. vainfo is only a hint in the error message, not the check itself:
# its output format varies, and a test encode is what actually proves
# the encoder opens.
preflight_vaapi_encode() {
    if ffmpeg -nostdin -hide_banner -loglevel error -y \
        "${ENC_PRE[@]}" \
        -f lavfi -i "testsrc=duration=1:size=1280x720:rate=30" \
        -vf "format=nv12,hwupload" \
        "${ENC_V[@]}" \
        -f null - 2>/dev/null; then
        return 0
    fi
    log_err "Pre-flight check FAILED: hevc_vaapi (HEVC Main, 8-bit) cannot encode on '$VAAPI_DEVICE'."
    log_err "  This batch would fail on every file. Check 'vainfo --display drm --device $VAAPI_DEVICE':"
    log_err "  VAProfileHEVCMain needs a VAEntrypointEncSlice entry. If it only lists VLD, this GPU"
    log_err "  cannot encode HEVC at all; set ENCODER=software in $CONFIG_PATH instead."
    log_err "  Aborting before any file is processed."
    return 1
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
    if [[ "$ENCODER" == "vaapi" ]]; then
        preflight_vaapi_encode || exit 1
    fi
}

# Confirm an encode is complete: right height and same length as the source.
verify_output() {
    local src="$1" out="$2" sd od
    if ! output_dims "$src"; then
        log_err "VERIFY FAILED: can't read the source video $(basename "$src")"
        return 1
    fi
    if ! probe_video "$out" || [ "${V_W:-0}" -lt $((OUT_W - 2)) ] || [ "$V_W" -gt $((OUT_W + 2)) ] \
        || [ "$V_H" -lt $((OUT_H - 2)) ] || [ "$V_H" -gt $((OUT_H + 2)) ]; then
        log_err "VERIFY FAILED: $(basename "$out") is ${V_W:-?}x${V_H:-?}, expected ${OUT_W}x${OUT_H}"
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

# No-savings helpers live in common.sh (keyed by encoder/quality/target).
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

    height=${HEIGHTS["$input"]:-}
    local action="${PLAN["$input"]:-}"
    if [[ "$action" != encode && "$action" != makecopy ]]; then
        local res="?"
        [[ -n "$height" ]] && res="$(res_label "${CLASSES["$input"]}") ${WIDTHS["$input"]}x${height}"
        log_warn "SKIP (${WHY["$input"]:-nothing to do}): $filename [${res}, ${original_size_mb}MB]"
        return 2
    fi

    # The no-savings list is about shrinking in place; a companion copy is
    # additive, so it doesn't apply to makecopy.
    if [[ "$action" == encode ]] && is_no_savings "$input"; then
        log_warn "SKIP (re-encoding didn't save space last time): $filename"
        return 2
    fi

    codec=$(get_video_codec "$input" "${STREAMS["$input"]:-v:0}")

    if [[ "$DRY_RUN" == true ]]; then
        if [[ "$action" == makecopy ]]; then
            log "[DRY-RUN] Would keep 4K and make ${TARGET_HEIGHT}p copy: $filename [${height}p, ${codec}, ${original_size_mb}MB]"
        else
            local target_size_mb=$(( original_size_mb * 60 / 100 ))
            log "[DRY-RUN] Would encode: $filename [${height}p, ${codec}, ${original_size_mb}MB] -> ~${target_size_mb}MB"
        fi
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

    if ! output_dims "$input"; then
        log_err "FAILED: can't read the video in $filename"
        return 1
    fi
    local stream=${STREAMS["$input"]:-$V_INDEX}
    set_filters "$OUT_W" "$OUT_H" "$input" "$stream"

    duration=$(get_duration "$input")
    log "Encoding: $filename [$(res_label "${CLASSES["$input"]:-$height}") ${WIDTHS["$input"]:-?}x${height}, ${codec}, ${original_size_mb}MB] -> ${OUT_W}x${OUT_H}"
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
            -map "0:${stream}" -map '0:a?' -map '0:s?' -map '0:t?' \
            -c:a copy -c:s copy -c:t copy \
            -vf "$vf" \
            "${ENC_V[@]}" \
            "${sub_args[@]}" \
            -max_muxing_queue_size 1024 \
            "${progress_args[@]}" \
            -f matroska "$part" 2>>"$logfile"; then
            ok=true
            break
        fi
        if [[ "$mode" == gpu ]]; then
            # The encoder is identical for GPU and CPU decode, so an
            # encoder-open failure (e.g. no Main10 entrypoint) will fail
            # exactly the same way on CPU. Retrying it only wastes a
            # second full attempt and hides the real error behind a
            # misleading "GPU decode failed" message. Only retry when
            # the failure was actually on the decode side.
            if is_encoder_failure "$logfile"; then
                log_err "  Encoder failed to open (not a decode problem), not retrying with CPU decode"
                break
            fi
            log_warn "  GPU decode failed for this file, retrying with CPU decode"
        fi
    done

    if [[ "$ok" == true ]]; then

        if ! verify_output "$input" "$part"; then
            rm -f "$part"
            CURRENT_PART=""
            return 1
        fi
        mv -f "$part" "$outfile"
        CURRENT_PART=""

        if [[ "$action" == makecopy ]]; then
            place_copy "$input" "$outfile" || return 1
            return 0
        fi

        local elapsed new_size_mb savings=0
        elapsed=$(( $(date +%s) - start_time ))
        new_size_mb=$(get_file_size_mb "$outfile")
        [[ "$original_size_mb" -gt 0 ]] && savings=$(( (original_size_mb - new_size_mb) * 100 / original_size_mb ))

        log_ok "Encoded: $filename -> ${new_size_mb}MB (${savings}% saved, ${elapsed}s)"
        log_ok "Verified: $(basename "$outfile") is ${OUT_W}x${OUT_H}, full length"
        return 0
    else
        LAST_FAIL_SIG=$(failure_signature "$logfile")
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

# Duplicate sidecar files (subtitles, nfo) for a new companion copy, so
# players still match them to the copy's name. Unlike rename_sidecars,
# the originals stay where they are.
copy_sidecars() {
    local dir="$1" base="$2" newbase="$3" f suffix
    for f in "${dir}/${base}".*; do
        [[ -f "$f" ]] || continue
        suffix="${f#"${dir}/${base}"}"
        case "${suffix##*.}" in
            srt|ass|ssa|sub|idx|vtt|sup|nfo|SRT|ASS|SSA|SUB|IDX|VTT|SUP|NFO) ;;
            *) continue ;;
        esac
        [[ -e "${dir}/${newbase}${suffix}" ]] && continue
        cp -- "$f" "${dir}/${newbase}${suffix}" && log "  Copied sidecar: $(basename "$f")"
    done
}

# Put an encoded companion copy next to the original (makecopy): the
# original is kept, unlike replace_original() which deletes it.
# $1 = original file, $2 = encoded temp file (already verified).
place_copy() {
    local input="$1" outfile="$2"
    local filename base newbase dir newpath tmpdest
    filename=$(basename "$input")
    base="${filename%.*}"
    newbase=$(build_newbase "$base")
    dir=$(dirname "$input")
    newpath="${dir}/${newbase}.mkv"

    if [[ -e "$newpath" ]]; then
        log_warn "Copy already exists for $filename, keeping original and discarding temp file"
        rm -f -- "$outfile"
        return 0
    fi

    # Copy next to the original first, then rename into place, so an
    # interrupted run never leaves a half-written file behind.
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
    rm -f -- "$outfile"
    copy_sidecars "$dir" "$base" "$newbase"

    log_ok "Kept original and added copy: ${filename} + ${newbase}.mkv"
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
        record_no_savings "$input"
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

title_files() {
    TITLE_FILES=()
    local f
    while IFS= read -r -d '' f; do
        TITLE_FILES+=("$f")
    done < <(video_exts "$1")
}

print_plan() {
    local f
    for f in "${TITLE_FILES[@]}"; do
        local res="?"
        [[ -n "${HEIGHTS["$f"]}" ]] && res="$(res_label "${CLASSES["$f"]}") ${WIDTHS["$f"]}x${HEIGHTS["$f"]}"
        printf '%s\t%s\t%s\t%s\n' "${PLAN["$f"]}" "$res" "${WHY["$f"]}" "$f"
    done
}

# Returns 1 if any episode failed. Sets SHOW_TODO to the number of files to
# shrink or copy.
process_show() {
    local show_dir="$1"
    local show_name f
    show_name=$(basename "$show_dir")

    title_files "$show_dir"
    plan_title "${TITLE_FILES[@]}"
    SHOW_TODO=0
    for f in "${TITLE_FILES[@]}"; do
        [[ "${PLAN["$f"]}" == encode || "${PLAN["$f"]}" == makecopy ]] && SHOW_TODO=$((SHOW_TODO + 1))
    done
    if [[ "$SHOW_TODO" -eq 0 ]]; then
        log "Nothing to shrink in: $show_name"
        return 0
    fi

    log "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
    log "Processing: $show_name"
    log "  Profile: ${PROFILE_NAME} -> ${TARGET_HEIGHT}p, quality ${QUALITY}, keep 4K: ${KEEP_4K}"
    log "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"

    local total_files=0 encoded_files=0 skipped_files=0 failed_files=0 total_saved=0
    local -a file_list=("${TITLE_FILES[@]}")

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
                CONSEC_FAIL=0; CONSEC_SIG=""
                if [[ "$DRY_RUN" != true ]]; then
                    newbase=$(build_newbase "${filename%.*}")
                    outfile="${TEMP_DIR}/${newbase}.mkv"
                    if [[ -f "$outfile" ]]; then
                        total_saved=$((total_saved + $(get_file_size_mb "$file") - $(get_file_size_mb "$outfile")))
                    fi
                fi ;;
            2) skipped_files=$((skipped_files + 1)) ;;
            *)
                failed_files=$((failed_files + 1))
                # Circuit breaker: a repeated identical failure (e.g. an
                # unsupported encoder profile) means the rest of the
                # batch will fail the same way. Stop instead of looping
                # through hundreds of files. Different errors reset it.
                if [[ "$LAST_FAIL_SIG" == "$CONSEC_SIG" ]]; then
                    CONSEC_FAIL=$((CONSEC_FAIL + 1))
                else
                    CONSEC_SIG="$LAST_FAIL_SIG"; CONSEC_FAIL=1
                fi
                if [[ "$CONSEC_FAIL" -ge "$CIRCUIT_MAX" ]]; then
                    CIRCUIT_TRIPPED=true
                    log_err "Circuit breaker: $CONSEC_FAIL consecutive failures with the same error ($CONSEC_SIG), aborting batch."
                    break
                fi ;;
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

    # Only files this run planned to shrink. Another version of the same video
    # would map to the same temp file, and must never be replaced by it.
    local file failed=0
    for file in "${TITLE_FILES[@]}"; do
        [[ "${PLAN["$file"]}" == encode ]] || continue
        replace_original "$file" || failed=$((failed + 1))
    done
    [[ "$failed" -eq 0 ]]
}

# Encode a show, then swap in the new files if every episode succeeded.
run_show() {
    local dir="${1%/}" ok=true
    apply_profile "$dir"
    setup_encoder
    if [[ "$PLAN_ONLY" == true ]]; then
        title_files "$dir"
        plan_title "${TITLE_FILES[@]}"
        print_plan
        return 0
    fi
    if [[ "$TITLE_SKIP" == true ]]; then
        log "Skipping $(basename "$dir"): set to never shrink"
        return 0
    fi
    process_show "$dir" || ok=false
    [[ "$SHOW_TODO" -eq 0 || "$MAX_FILES" -gt 0 ]] && return 0
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
    local dir rc=0
    while IFS= read -r -d '' dir; do
        run_show "$dir" || rc=1
        [[ "$CIRCUIT_TRIPPED" == true ]] && break
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

    if [[ "$CLEAR_NO_SAVINGS" == true ]]; then
        clear_no_savings
        log_ok "Cleared no-savings list ($(NO_SAVINGS_LIST))"
        return 0
    fi

    if [[ "$VERIFY_ONLY" == true ]]; then
        log "Verification mode: checking re-encoded files in $TEMP_DIR"
        # Valid targets are resolution *classes* (like Plex), not stored
        # heights: a correct 720p widescreen file is 1280x534, class 720.
        # Include per-title override heights, which are also valid targets.
        local -a valid_classes=("$TV_HEIGHT" "$MOVIES_HEIGHT")
        if [[ -f "${OVERRIDES_FILE:-}" ]]; then
            local ov_val ov_path
            while IFS=$'\t' read -r ov_val ov_path; do
                [[ "$ov_val" =~ ^[0-9]+$ ]] && valid_classes+=("$ov_val")
            done < "$OVERRIDES_FILE"
        fi
        local count=0 valid=0 f cls size_mb ok
        for f in "$TEMP_DIR"/*.mkv; do
            [[ -f "$f" ]] || continue
            count=$((count + 1))
            size_mb=$(get_file_size_mb "$f")
            ok=false
            if probe_video "$f"; then
                cls=$V_CLASS
                local vc
                for vc in "${valid_classes[@]}"; do
                    [[ "$cls" -eq "$vc" ]] && ok=true
                done
            fi
            if [[ "$ok" == true ]]; then
                log_ok "$(basename "$f") [$(res_label "$cls"), ${size_mb}MB]"
                valid=$((valid + 1))
            else
                log_err "$(basename "$f") [${cls:-UNKNOWN}p, ${size_mb}MB]"
            fi
        done
        log "Verified: $valid/$count files valid"
        return 0
    fi

    setup_encoder
    [[ "$DRY_RUN" == true ]] || check_encoder

    [[ "$PLAN_ONLY" == true ]] || {
    log "Re-encoder: HEVC ${ENC_NAME}, GPU decode: ${HW_DECODE}"
    log "Profiles: TV -> ${TV_HEIGHT}p (quality ${TV_QUALITY}, keep 4K: ${TV_KEEP_4K}), Movies -> ${MOVIES_HEIGHT}p (quality ${MOVIES_QUALITY}, keep 4K: ${MOVIES_KEEP_4K})"
    log "Libraries: ${LIBRARIES[*]}"
    log "Temp dir: $TEMP_DIR"
    log "Dry run: $DRY_RUN"
    [[ "$MAX_FILES" -gt 0 ]] && log "Max files: $MAX_FILES"
    log ""
    }

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

# Only run when executed directly, not when sourced (e.g. by tests
# checking is_encoder_failure / failure_signature).
if [[ "${BASH_SOURCE[0]}" == "${0}" ]]; then
    main "$@"
fi
