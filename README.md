# Reencode

Downscales your TV shows and movies to 720p HEVC using hardware encoding to save disk space. Comes with a web dashboard, so you can queue and watch encodes from any browser on your network.

Originals are only replaced after every episode of a title encodes **and** verifies (right resolution, full length). An interrupted encode never touches your library.

## Requirements

- `ffmpeg`/`ffprobe` (with VAAPI, NVENC or libx265 support)
- A GPU: Intel/AMD via VAAPI (e.g. an RX 580, needs `mesa-va-drivers`), NVIDIA via NVENC, or no GPU at all with the slow software encoder
- `bash`, and `python3` (3.8+) for the dashboard. It uses only the standard library, so there's nothing to `pip install`.

## Quick start

```bash
git clone https://github.com/BirdTruther/reencode
cd reencode
./dashboard.py
```

Then open `http://<your-server>:8686` in a browser.

The first run auto-generates `reencode.conf` (gitignored), detecting your TV/Movies folders and GPU. Change anything from the dashboard's **Settings**, by editing the file, or with `./reencode.sh --setup`.

## Web dashboard

![Dashboard](docs/dashboard.png)

- **Library view** of every show and movie, showing how many files are already at the target resolution, the size, estimated savings and status. Click a title to see each file's resolution, codec and length.
- **One-click Encode** or **Preview** (dry run) per title, or "Encode everything that needs it".
- **Queue**: jobs run one at a time (one GPU), and you can reorder or remove them.
- **Live progress**: per-episode percentage, fps, speed, time left, and a live log.
- **Stop** any encode safely: the partial file is thrown away and originals are untouched.
- **History**: space saved per job, failures, and full logs.
- **Settings** editor for libraries, target resolution, encoder and quality.
- Works on phones, and supports light and dark mode.

```bash
./dashboard.py                              # listen on all interfaces, port 8686
./dashboard.py --host 127.0.0.1 --port 9000 # this machine only
REENCODE_DASHBOARD_PASSWORD=secret ./dashboard.py   # require a password (any username)
```

By default the dashboard listens on your whole LAN with no password. Set `REENCODE_DASHBOARD_PASSWORD` if other people use your network, and don't expose it to the internet.

### Run it as a service

`/etc/systemd/system/reencode-dashboard.service`:

```ini
[Unit]
Description=Reencode dashboard
After=network-online.target

[Service]
User=you
ExecStart=/home/you/reencode/dashboard.py
Environment=REENCODE_DASHBOARD_PASSWORD=change-me
Restart=on-failure

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl daemon-reload && sudo systemctl enable --now reencode-dashboard
```

Run it as a user that can read/write your media and access the GPU (usually in the `video`/`render` groups).

## Libraries (TV, movies, anything)

`LIBRARIES` in `reencode.conf` lists folders of *titles*, where each title is a folder:

```
/mnt/media/TV/Show Name/Season 1/Show.S01E01.1080p.mkv
/mnt/media/Movies/Heat (1995)/Heat (1995) 2160p.mkv
```

That's the layout Plex, Jellyfin, Sonarr and Radarr use by default. Files sitting loose in the library root (not in a folder) are ignored.

## Command line

The dashboard is optional. Everything still works from the terminal:

```bash
./encodetv                            # pick a title from an interactive menu (cached scan, fast)
./reencode.sh --all                   # process every title in every library
./reencode.sh --show "South Park"     # one title (exact folder name wins, else substring)
./reencode.sh --dir "/mnt/media/Movies/Heat (1995)"
./reencode.sh --dry-run --show "X"    # preview, nothing gets encoded
./reencode.sh --allow-failures --show "X"   # replace the episodes that worked even if some failed
```

Optional: symlink `encodetv` into your PATH so you can run it from anywhere.

```bash
ln -s "$PWD/encodetv" ~/bin/encodetv
```

## How it works

- Only files taller than `TARGET_HEIGHT` (default 720) are touched. Files already at or below it are skipped.
- **All** audio tracks, subtitles and attachments are kept. MP4 text subtitles are converted to SRT so they fit in MKV.
- Encodes are written to `TEMP_DIR` as `.part` files and only kept once they check out: correct height, and the same length as the source. Truncated or leftover files are thrown away and re-encoded.
- Originals are swapped only after **all** of a title's episodes are done and verified. The new file is copied next to the original first, and the original is deleted only once the copy is in place, so a full disk can't lose an episode.
- If an encode ends up *bigger* than the original, the original is kept and that file is skipped on future runs.
- Output is always `.mkv`. Whatever resolution marker is in the filename gets replaced (`1080p`, `2160p`, `4K` → `720p`); if there's no marker, ` 720p` is appended. Matching subtitle/nfo sidecar files (`Show.S01E01.1080p.en.srt`) are renamed to match.

## Encoders

Set `ENCODER` in `reencode.conf` (or in dashboard Settings):

| `ENCODER`  | Hardware                     | `QUALITY` means | Good starting value |
|------------|------------------------------|-----------------|---------------------|
| `vaapi`    | Intel iGPU/Arc, AMD Radeon   | `-qp`           | 28–32               |
| `nvenc`    | NVIDIA GeForce/Quadro        | `-cq`           | 24–28               |
| `software` | Any CPU (libx265, slow)      | `-crf`          | 24–28               |
