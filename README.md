# TV Re-encoder

Downscales your TV library to 720p HEVC using hardware encoding (VAAPI) to save disk space. Originals are only replaced after every episode encodes successfully.

## Requirements

- `ffmpeg`/`ffprobe` with VAAPI support
- An Intel/AMD GPU with VAAPI (NVIDIA needs a tweak — see below)
- `bash`

## Quick start

```bash
git clone <https://github.com/BirdTruther/reencode>
cd reencode
./encodetv
```

First run auto-generates `reencode.conf` (gitignored), detecting your TV directory and GPU device. Edit that file to change paths, target height, or quality, or run `./reencode.sh --setup` for a guided prompt.

## Usage

```bash
./encodetv              # pick a show from an interactive menu (cached scan, fast)
./reencode.sh --all     # process every show in the library
./reencode.sh --show "South Park"   # one show
./reencode.sh --dry-run --show "X"  # preview, nothing gets encoded
```

Optional: symlink `encodetv` into your PATH so you can run it from anywhere.

```bash
ln -s "$PWD/encodetv" ~/bin/encodetv
```

## How it works

- Only files taller than `TARGET_HEIGHT` (default 720) are touched. Already-720p files are skipped.
- Encodes go to `TEMP_DIR` first. Originals are swapped only after **all** episodes are done and verified.
- Output naming: whatever resolution marker is in the filename gets replaced (`1080p` → `720p`); if there's no marker, ` 720p` is appended. Works on any naming scheme.

## NVIDIA / non-VAAPI

The script uses `hevc_vaapi`. For NVIDIA you'd swap the ffmpeg call to `hevc_nvenc` with `-cq` instead of `-qp` — send a PR or open an issue if you want it supported properly.
