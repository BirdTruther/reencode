#!/usr/bin/env python3
"""reencode remote worker (v0) -- Windows-friendly.

A tiny script that borrows your PC's GPU for the reencode server:
it polls the dashboard for work, downloads a source file, runs the
ffmpeg command the server hands it, and uploads the result. The server
verifies everything and does the library replace itself.

Safe to kill any time (Ctrl+C): claimed jobs have a lease, so the job
just goes back in the server queue. Handy when it's time to game.

Needs: Python 3.9+ and ffmpeg in PATH with hevc_nvenc
  (e.g. a gyan.dev full build: https://www.gyan.dev/ffmpeg/builds/)

First run asks for the server IP + port and saves them next to this
script in worker_config.json.
"""

import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(HERE, "worker_config.json")
WORK_DIR = os.path.join(HERE, "work")
POLL_IDLE_SECS = 15
HEARTBEAT_SECS = 60


def load_config():
    try:
        with open(CONFIG_PATH, encoding="utf-8") as f:
            cfg = json.load(f)
        if cfg.get("host") and cfg.get("port"):
            return cfg
    except (OSError, ValueError):
        pass
    print("reencode worker setup")
    print("---------------------")
    host = input("reencode server IP (e.g. 192.168.1.10): ").strip()
    port = input("reencode dashboard port [8686]: ").strip() or "8686"
    name = input("worker name [desktop]: ").strip() or "desktop"
    if not host:
        sys.exit("no host given, quitting")
    cfg = {"host": host, "port": int(port), "name": name}
    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=2)
    print(f"saved to {CONFIG_PATH}\n")
    return cfg


def check_ffmpeg():
    ff = shutil.which("ffmpeg")
    if not ff:
        sys.exit("ffmpeg not found in PATH -- install a build with nvenc "
                 "(https://www.gyan.dev/ffmpeg/builds/) and retry")
    return ff


def detect_caps(ff):
    """What this machine can do, as the capability tags the server matches."""
    caps = []
    try:
        enc = subprocess.run([ff, "-hide_banner", "-encoders"],
                             capture_output=True, text=True, timeout=30).stdout
        dec = subprocess.run([ff, "-hide_banner", "-decoders"],
                             capture_output=True, text=True, timeout=30).stdout
    except (OSError, subprocess.TimeoutExpired):
        return "software"
    if re.search(r"\bhevc_nvenc\b", enc):
        caps.append("nvenc")
    if re.search(r"\b(av1_cuvid|h264_cuvid|hevc_cuvid)\b", dec):
        caps.append("cuda")
    if re.search(r"\bav1_cuvid\b", dec):
        caps.append("av1d")
    return ",".join(caps) if caps else "software"


class Server:
    def __init__(self, cfg):
        self.base = f"http://{cfg['host']}:{cfg['port']}"
        self.name = cfg["name"]

    def _req(self, method, path, data=None, ctype=None, timeout=30):
        req = urllib.request.Request(self.base + path, method=method,
                                     data=data)
        if ctype:
            req.add_header("Content-Type", ctype)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.status, r.read()
        except urllib.error.HTTPError as e:
            return e.code, e.read()
        except (urllib.error.URLError, TimeoutError, ConnectionError, OSError) as e:
            raise IOError(f"cannot reach {self.base}: {e}")

    def next_job(self, caps):
        q = urllib.parse.urlencode({"worker": self.name, "caps": caps})
        status, body = self._req("GET", f"/api/worker/next?{q}")
        if status == 204:
            return None
        if status != 200:
            raise IOError(f"next: HTTP {status}")
        return json.loads(body or b"null")

    def download(self, job_id, dest):
        # Streamed: a 4K source can be tens of GB, never hold it in RAM.
        req = urllib.request.Request(self.base + f"/api/worker/file/{job_id}",
                                     method="GET")
        try:
            with urllib.request.urlopen(req, timeout=600) as r, \
                    open(dest, "wb") as f:
                shutil.copyfileobj(r, f, 1024 * 1024)
        except urllib.error.HTTPError as e:
            raise IOError(f"download: HTTP {e.code}")
        except (urllib.error.URLError, TimeoutError, ConnectionError, OSError) as e:
            raise IOError(f"download failed: {e}")

    def heartbeat(self, job_id):
        try:
            self._req("POST", f"/api/worker/heartbeat/{job_id}",
                      data=b"", ctype="application/json", timeout=15)
        except IOError:
            pass

    def upload(self, job_id, path):
        # Streamed with an explicit Content-Length so the server can read
        # exactly the upload (no chunked encoding).
        size = os.path.getsize(path)
        req = urllib.request.Request(
            self.base + f"/api/worker/result/{job_id}", method="POST",
            headers={"Content-Type": "application/octet-stream",
                     "Content-Length": str(size)})
        try:
            with open(path, "rb") as f:
                req.data = f
                with urllib.request.urlopen(req, timeout=3600) as r:
                    body = r.read()
        except urllib.error.HTTPError as e:
            raise IOError(f"upload: HTTP {e.code}")
        except (urllib.error.URLError, TimeoutError, ConnectionError, OSError) as e:
            raise IOError(f"upload failed: {e}")
        return json.loads(body or b"{}")

    def fail(self, job_id, error):
        try:
            self._req("POST", f"/api/worker/fail/{job_id}",
                      data=json.dumps({"error": error}).encode(),
                      ctype="application/json", timeout=15)
        except IOError:
            pass


def parse_progress(line, duration):
    m = re.search(r"time=(\d+):(\d+):([\d.]+)", line)
    if not m or not duration:
        return None
    try:
        secs = int(m.group(1)) * 3600 + int(m.group(2)) * 60 + float(m.group(3))
        return min(100.0, 100.0 * secs / float(duration))
    except (ValueError, ZeroDivisionError):
        return None


def run_job(ff, srv, job, caps):
    jid = job["job_id"]
    os.makedirs(WORK_DIR, exist_ok=True)
    src = os.path.join(WORK_DIR, f"{jid}_in.mkv")
    dst = os.path.join(WORK_DIR, f"{jid}_out.mkv")
    print(f"[{time.strftime('%H:%M:%S')}] job {jid}: {job['filename']} "
          f"({job['size'] // 1024 // 1024}MB) -> {job['out_w']}x{job['out_h']} [{job['action']}]")

    stop = threading.Event()

    def beat():
        while not stop.wait(HEARTBEAT_SECS):
            srv.heartbeat(jid)

    print("  downloading...", flush=True)
    srv.download(jid, src)

    argv = [a.replace("__INPUT__", src).replace("__OUTPUT__", dst)
            for a in job["argv"]]
    argv[0] = ff  # use the ffmpeg we checked, not whatever is first on PATH
    # Drop -stats (it spams the console); keep -progress via stderr parse.
    argv = [a for a in argv if a != "-stats"]

    hb = threading.Thread(target=beat, daemon=True)
    hb.start()
    tail, rc = [], 1
    try:
        proc = subprocess.Popen(argv, stderr=subprocess.PIPE,
                                stdout=subprocess.DEVNULL, text=True,
                                errors="replace")
        last_pct = -1
        for line in proc.stderr:
            tail.append(line)
            tail = tail[-30:]
            pct = parse_progress(line, job.get("duration"))
            if pct is not None and pct - last_pct >= 5:
                last_pct = pct
                print(f"  encoding... {pct:.0f}%", flush=True)
        rc = proc.wait()
    finally:
        stop.set()
    if rc != 0:
        err = "".join(tail[-8:]).strip() or f"ffmpeg exited {rc}"
        print(f"  FAILED: {err.splitlines()[-1] if err else rc}")
        srv.fail(jid, err[-2000:])
        return False

    print("  uploading...", flush=True)
    try:
        res = srv.upload(jid, dst)
    finally:
        for p in (src, dst):
            try:
                os.unlink(p)
            except OSError:
                pass
    if res.get("ok"):
        print(f"  done -- server placed it ({job['action']})")
        return True
    print(f"  server rejected result: {res.get('error')}")
    return False


def main():
    cfg = load_config()
    ff = check_ffmpeg()
    caps = detect_caps(ff)
    srv = Server(cfg)
    print(f"worker '{cfg['name']}' -> {srv.base}  [caps: {caps}]")
    if "nvenc" not in caps:
        print("NOTE: no hevc_nvenc found -- the server will hand out "
              "software-encode jobs (slow). Install an nvenc-capable "
              "ffmpeg build for GPU speed.")
    print("polling for work (Ctrl+C to stop, safe any time)\n")
    try:
        while True:
            try:
                job = srv.next_job(caps)
            except IOError as e:
                print(f"[{time.strftime('%H:%M:%S')}] {e} -- retrying")
                time.sleep(POLL_IDLE_SECS)
                continue
            if not job:
                time.sleep(POLL_IDLE_SECS)
                continue
            try:
                run_job(ff, srv, job, caps)
            except IOError as e:
                print(f"  connection lost mid-job: {e} -- job returns to queue")
                time.sleep(5)
            print()
    except KeyboardInterrupt:
        print("\nstopped. Any claimed job returns to the server queue.")


if __name__ == "__main__":
    main()
