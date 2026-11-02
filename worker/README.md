# reencode remote worker (v0)

Borrow a spare PC's GPU/CPU for the reencode server. The worker polls the
dashboard for work, encodes one file at a time with the exact ffmpeg
command the server hands it, and uploads the result. The server verifies
everything and swaps it into the library itself.

**Safe to stop any time** (Ctrl+C): jobs have a lease, so a killed worker's
job just goes back in the server queue. Handy on a gaming rig.

## Setup (Windows)

1. Install Python 3.9+ from python.org (tick "Add python.exe to PATH").
2. Install an ffmpeg build with nvenc, e.g. the
   [gyan.dev full build](https://www.gyan.dev/ffmpeg/builds/).
   Extract it and add its `bin` folder to PATH.
   Check: `ffmpeg -hide_banner -encoders | findstr hevc_nvenc`
3. Copy this `worker` folder somewhere (e.g. `C:\reencode-worker`).
4. Run `python worker.py`. It asks for the server IP + port once and
   saves them in `worker_config.json` next to the script.

That's it -- it starts polling. Leave the window open while you want to
help; close it when you want your GPU back.

## What it does

- Advertises what the machine can do (`nvenc`, `cuda`, `av1d`, ...). The
  server matches jobs to capabilities -- e.g. AV1 sources go to a worker
  whose GPU can decode AV1.
- Downloads the source over HTTP, runs the server's ffmpeg command with
  `__INPUT__`/`__OUTPUT__` swapped for local temp paths, uploads the
  result, heartbeats while encoding so its lease doesn't expire.
- Never touches your library. It only ever writes to its own `work/`
  folder, which is cleaned after each job.

## Notes / v0 limits

- LAN only, no login on the worker endpoints (same trust model as the
  dashboard itself, which is local-only).
- One job at a time, first-come order. If the worker's ffmpeg fails on a
  file, the job is released back to the queue and the error shows in the
  dashboard's Workers card.
- Don't run a local dashboard "Encode" on the same title at the same
  time as a worker -- v0 doesn't deconflict them.
