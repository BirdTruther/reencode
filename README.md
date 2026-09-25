# Reencode

Downscales your TV shows and movies to 720p HEVC using hardware encoding to save disk space. Comes with a web dashboard, so you can queue, pause and watch encodes from any browser.

Originals are only replaced after every episode of a title encodes **and** verifies (right resolution, full length). An interrupted encode never touches your library.

![Dashboard](docs/dashboard.png)

## Install

Pick one. All three give you the dashboard on port **8686**, and none needs a separate web server.

### Docker (easiest)

The image is published as `ghcr.io/birdtruther/reencode:latest` (amd64 + arm64), so you don't need to clone or build anything. Copy [`docker-compose.yml`](docker-compose.yml), edit the paths (your TV/Movies folders, a scratch folder, `PUID`/`PGID` = the owner of your media, your timezone), then:

```bash
docker compose up -d
docker logs reencode        # shows the generated password
```

**Cosmos, Portainer, Unraid, etc.:** paste the compose file into their compose/stack import, or create a container by hand with:

| Setting | Value |
|---|---|
| Image | `ghcr.io/birdtruther/reencode:latest` |
| Port | `8686` |
| Device | `/dev/dri` (Intel/AMD GPU) |
| Env | `PUID`, `PGID` (owner of your media, see `id`), `TZ` (e.g. `America/New_York`) |
| Volumes | `/config` (settings, keep it), `/transcode` (scratch space), your libraries under `/media/...` (e.g. `/media/TV`, `/media/Movies`) |

The image includes ffmpeg and the AMD and Intel GPU drivers. It runs as `PUID`/`PGID` so replaced files keep the right owner, and it joins the GPU's group automatically. Settings, history and logs live in `./config`.

To build the image yourself instead, clone the repo and swap the `image:` line in the compose file for `build: .`.

### As a service (no Docker)

Needs `ffmpeg` and `python3` (3.8+) on the machine. The dashboard uses only the Python standard library, so there's nothing to `pip install`.

```bash
git clone https://github.com/BirdTruther/reencode
cd reencode
sudo ./install-service.sh          # runs as you, starts now and on every boot
```

It prints the address and password when it's done. Use `--port 9000` or `--user plex` to change things, `--print` to see the systemd unit without installing, and `--uninstall` to remove it.

### Just run it

```bash
./dashboard.py                     # Ctrl+C to stop
```

The first run auto-generates `reencode.conf` (gitignored), detecting your TV/Movies folders and GPU. Change anything from the dashboard's **Settings**, by editing the file, or with `./reencode.sh --setup`.

## Security

- **A password is on by default.** When the dashboard is reachable from other machines, a random password is generated on first run, printed, and saved to `.dashboard_password` next to `reencode.conf` (readable only by its owner). The username can be anything. To choose your own, set `REENCODE_DASHBOARD_PASSWORD`.
- After 10 wrong passwords, that IP is locked out for 10 minutes.
- `--host 127.0.0.1` limits it to the machine itself, which needs no password.
- `--no-password` turns the password off, e.g. if a reverse proxy in front already handles logins.

**Reaching it from outside your home:** don't just port-forward 8686. In order of preference:

1. **Use a VPN** such as [Tailscale](https://tailscale.com) or WireGuard. Nothing is exposed to the internet at all.
2. **Use a reverse proxy with HTTPS.** For example, with [Caddy](https://caddyserver.com), `reencode.example.com { reverse_proxy localhost:8686 }` gets a certificate automatically.
3. **Use the built-in HTTPS:** `--tls-cert fullchain.pem --tls-key privkey.pem`, or the `REENCODE_DASHBOARD_TLS_CERT`/`_KEY` environment variables.

Without HTTPS, the password travels in plain text, which is fine on your own network but not over the internet.

## Web dashboard

- **Library view** of every show and movie: how many files are already at the target resolution, size, estimated savings and status. Click a title to see each file's resolution, codec and length.
- **One-click Encode** or **Preview** (dry run) per title, or "Encode everything that needs it".
- **Queue**: jobs run one at a time (one GPU), and you can reorder or remove them.
- **Live progress**: per-episode percentage, fps, speed, time left, and a live log.
- **Pause / Resume** any time, and **Stop** safely: the partial file is thrown away and originals are untouched.
- **Encoding hours** (e.g. `01:00-08:00`): encodes pause outside the window and pick up automatically, so they stay out of the way while people are watching.
- **History**: space saved per job, failures, and full logs.
- **Settings** editor for libraries, resolution, encoder, quality, GPU decoding and hours.
- Works on phones, and supports light and dark mode.
- If an encode started from the terminal is already running, queued jobs wait for it instead of fighting over the GPU.

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
- With `vaapi`/`nvenc`, video is decoded, scaled and encoded entirely on the GPU, which is faster and leaves the CPU free. If the GPU can't decode a particular file (old or unusual codecs), that file is retried with CPU decoding automatically. Set `HW_DECODE="no"` to always decode on the CPU.
- **All** audio tracks, subtitles and attachments are kept. MP4 text subtitles are converted to SRT so they fit in MKV.
- Encodes are written to `TEMP_DIR` as `.part` files and only kept once they check out: correct height, and the same length as the source. Truncated or leftover files are thrown away and re-encoded.
- Originals are swapped only after **all** of a title's episodes are done and verified. The new file is copied next to the original first, and the original is deleted only once the copy is in place, so a full disk can't lose an episode.
- If an encode ends up *bigger* than the original, the original is kept and that file is skipped on future runs.
- Output is always `.mkv`. Whatever resolution marker is in the filename gets replaced (`1080p`, `2160p`, `4K` → `720p`); if there's no marker, ` 720p` is appended. Matching subtitle/nfo sidecar files (`Show.S01E01.1080p.en.srt`) are renamed to match.

## Encoders

Set `ENCODER` in `reencode.conf` (or in dashboard Settings):

| `ENCODER`  | Hardware                                   | `QUALITY` means | Good starting value |
|------------|--------------------------------------------|-----------------|---------------------|
| `vaapi`    | AMD Radeon (RX 400 and newer), Intel iGPU/Arc | `-qp`        | 28–32               |
| `nvenc`    | NVIDIA GeForce/Quadro (GTX 950 and newer)  | `-cq`           | 24–28               |
| `software` | Any CPU (libx265, slow)                    | `-crf`          | 24–28               |

AMD cards up to the RX 500 series (Polaris) encode 8-bit HEVC only. Reencode always outputs 8-bit on VAAPI, so this just works.

## Troubleshooting

- **Check your GPU can encode HEVC:** run `vainfo` (or `docker exec reencode vainfo`) and look for `VAProfileHEVCMain : VAEntrypointEncSlice`. If it's missing, install your distro's VA driver (`mesa-va-drivers` for AMD, `intel-media-va-driver` for Intel) or pick another encoder.
- **"Permission denied" on `/dev/dri`:** the user running the dashboard needs to be in the `video` and `render` groups (`sudo usermod -aG video,render $USER`, then log out and back in). `install-service.sh` and the Docker image handle this for you.
- **A file failed:** the dashboard's history shows the reason, and the full ffmpeg output is in `LOG_DIR/<file name>.log`.
