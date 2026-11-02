# reencode remote worker (v1)

Borrow a spare PC's GPU/CPU for the reencode server. The worker polls the
dashboard for work, encodes one file at a time with the exact ffmpeg
command the server hands it, and uploads the result. The server verifies
everything and swaps it into the library itself.

**It only takes jobs you queue in the dashboard** -- never anything on
its own. Queue titles like normal, and the worker picks them up (or the
server encodes locally if no worker is around).

**Safe to stop any time** (Ctrl+C): the job is handed straight back to
the queue -- not counted as a failure. Handy on a gaming rig.

## Setup (Windows)

1. Install Python 3.9+ from python.org (tick "Add python.exe to PATH").
2. Copy this `worker` folder somewhere (e.g. `C:\reencode-worker`).
3. Run `python worker.py`. It asks for the server IP + port once and
   saves them in `worker_config.json` next to the script.

That's it. If ffmpeg isn't in PATH, the worker downloads a Windows
build automatically on first run (~100 MB, one time). Leave the window
open while you want to help; close it (or Ctrl+C) when you want your
GPU back.

Check your GPU is being used: the banner at startup lists what the
worker `can do` -- look for `nvenc`.

## What it does

- Advertises what the machine can do (`nvenc`, `cuda`, `av1d`, ...). The
  server matches jobs to capabilities -- e.g. AV1 sources go to a worker
  whose GPU can decode AV1.
- Downloads the source over HTTP, runs the server's ffmpeg command with
  `__INPUT__`/`__OUTPUT__` swapped for local temp paths, uploads the
  result, heartbeats the whole time (download, encode AND upload) so its
  lease can't expire mid-transfer, and reports encode percent so the
  dashboard can show progress.
- If the job disappears server-side (you removed the title from the
  queue), the worker notices on its next heartbeat and moves on.
- Never touches your library. It only ever writes to its own `work/`
  folder, which is cleaned after each job.

## Notes / v1 limits

- LAN only, no login on the worker endpoints (same trust model as the
  dashboard itself, which is local-only).
- One job at a time, queue order. A file the worker fails 3 times is
  parked for workers (the server will still try it locally).
- The queue's ⏸ Pause button stops new pickups for workers and the
  local runner alike; the running job finishes.
