#!/usr/bin/env python3
"""reencode remote worker (v1) -- Windows-friendly.

A tiny script that borrows your PC's GPU for the reencode server:
it polls the dashboard for work, downloads a source file, runs the
ffmpeg command the server hands it, and uploads the result. The server
verifies everything and does the library replace itself.

v1: the worker ONLY takes jobs you explicitly queue in the dashboard
(it never grabs library files on its own). Safe to kill any time
(Ctrl+C): the job is handed straight back to the queue, no harm done.
Handy when it's time to game.

Needs: Python 3.9+. ffmpeg is downloaded automatically on first run
if it isn't already in PATH.

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
import traceback
import http.client
import urllib.error
import urllib.parse
import urllib.request
import zipfile

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


FFMPEG_URL = "https://www.gyan.dev/ffmpeg/builds/ffmpeg-release-essentials.zip"
LOCAL_FFMPEG = os.path.join(HERE, "ffmpeg", "ffmpeg.exe")


def download_ffmpeg():
    """Download gyan.dev's Windows build and keep just ffmpeg.exe next to us."""
    os.makedirs(os.path.dirname(LOCAL_FFMPEG), exist_ok=True)
    zpath = LOCAL_FFMPEG + ".zip"
    print("ffmpeg not found -- downloading it (~100 MB, one time)...", flush=True)
    last = [-1]

    def progress(blocks, bsize, total):
        if total > 0:
            pct = min(100, blocks * bsize * 100 // total)
            if pct // 10 != last[0]:
                last[0] = pct // 10
                print(f"  {pct}%", flush=True)

    urllib.request.urlretrieve(FFMPEG_URL, zpath, progress)
    with zipfile.ZipFile(zpath) as z:
        member = next((n for n in z.namelist()
                       if n.replace("\\", "/").endswith("/bin/ffmpeg.exe")), None)
        if not member:
            raise IOError("ffmpeg.exe not found in the downloaded zip")
        with z.open(member) as src, open(LOCAL_FFMPEG, "wb") as dst:
            shutil.copyfileobj(src, dst)
    os.unlink(zpath)
    print(f"saved {LOCAL_FFMPEG}\n")


def check_ffmpeg():
    ff = shutil.which("ffmpeg")
    if ff:
        return ff, "PATH"
    if not os.path.isfile(LOCAL_FFMPEG):
        try:
            download_ffmpeg()
        except (OSError, zipfile.BadZipFile) as e:
            sys.exit(f"ffmpeg download failed: {e}")
    return LOCAL_FFMPEG, "auto-downloaded"


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


def fmt_mb(n):
    return f"{n / 1024 / 1024:,.0f} MB"


def fmt_eta(secs):
    if secs is None or secs < 0:
        return "--:--"
    m, s = divmod(int(secs), 60)
    h, m = divmod(m, 60)
    return f"{h:d}:{m:02d}:{s:02d}" if h else f"{m:02d}:{s:02d}"


class StatusLine:
    """One live-updating console line.

    On a real terminal it rewrites in place (carriage return); when
    output is redirected it prints a calm periodic line instead."""

    def __init__(self):
        self._tty = sys.stdout.isatty()
        self._last_print = 0
        self._pad = 0

    def show(self, text):
        if self._tty:
            self._pad = max(self._pad, len(text))
            sys.stdout.write("\r" + text.ljust(self._pad))
            sys.stdout.flush()
        elif time.time() - self._last_print > 60:
            self._last_print = time.time()
            print(text, flush=True)

    def end(self):
        if self._tty:
            sys.stdout.write("\r" + " " * self._pad + "\r")
            sys.stdout.flush()
        self._pad = 0


def banner(cfg, caps, ff_src):
    print()
    print("  🎬✨🎬✨🎬✨🎬✨🎬✨🎬✨🎬✨🎬")
    print("        reencode worker v1")
    print("  🎬✨🎬✨🎬✨🎬✨🎬✨🎬✨🎬✨🎬")
    print(f"  worker '{cfg['name']}'  →  http://{cfg['host']}:{cfg['port']}")
    print(f"  can do: {caps}   ·   ffmpeg: {ff_src}")
    print()
    print("  I only take jobs you queue in the dashboard —")
    print("  never anything on my own.")
    print("  Press Ctrl+C any time: I'll hand my job straight")
    print("  back to the queue, no harm done. 🎮")
    print()
    if "nvenc" not in caps:
        print("  ⚠️  no hevc_nvenc found here, so I'll get slower")
        print("     software-encode jobs. For GPU speed, use an")
        print("     nvenc-capable ffmpeg build.")
        print()


class UploadError(Exception):
    def __init__(self, msg, retry=False):
        super().__init__(msg)
        self.retry = retry


def xfer_text(icon, n, total, t0):
    """'📥 42% · 90 MB / 217 MB · 85.1 MB/s · ETA 00:02' for a transfer."""
    el = max(time.time() - t0, 0.001)
    rate = n / el
    eta = (total - n) / rate if rate > 0 and total else None
    pct = f"{100.0 * n / total:.0f}% · " if total else ""
    of = f"{fmt_mb(n)} / {fmt_mb(total)}" if total else f"{fmt_mb(n)}"
    return f"  {icon} {pct}{of} · {rate / 1024 / 1024:.1f} MB/s · ETA {fmt_eta(eta)}"


class Server:
    def __init__(self, cfg):
        self.base = f"http://{cfg['host']}:{cfg['port']}"
        self.name = cfg["name"]
        self.host, self.port = cfg["host"], int(cfg["port"])
        self.last_headers = {}
        self.idle_reason = ""

    def _req(self, method, path, data=None, ctype=None, timeout=30):
        req = urllib.request.Request(self.base + path, method=method,
                                     data=data)
        if ctype:
            req.add_header("Content-Type", ctype)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                self.last_headers = r.headers
                return r.status, r.read()
        except urllib.error.HTTPError as e:
            self.last_headers = e.headers
            return e.code, e.read()
        except (urllib.error.URLError, TimeoutError, ConnectionError, OSError) as e:
            raise IOError(f"cannot reach {self.base}: {e}")

    def next_job(self, caps):
        q = urllib.parse.urlencode({"worker": self.name, "caps": caps})
        status, body = self._req("GET", f"/api/worker/next?{q}")
        if status == 204:
            self.idle_reason = self.last_headers.get("X-Idle-Reason", "")
            return None
        if status != 200:
            raise IOError(f"next: HTTP {status}")
        return json.loads(body or b"null")

    def download(self, job_id, dest, total=0, on_progress=None):
        # Streamed: a 4K source can be tens of GB, never hold it in RAM.
        req = urllib.request.Request(self.base + f"/api/worker/file/{job_id}",
                                     method="GET")
        try:
            with urllib.request.urlopen(req, timeout=600) as r, \
                    open(dest, "wb") as f:
                if not total:
                    try:
                        total = int(r.headers.get("Content-Length") or 0)
                    except (TypeError, ValueError):
                        total = 0
                n = 0
                while True:
                    chunk = r.read(1024 * 1024)
                    if not chunk:
                        break
                    f.write(chunk)
                    n += len(chunk)
                    if on_progress:
                        on_progress(n, total)
        except urllib.error.HTTPError as e:
            raise IOError(f"download: HTTP {e.code}")
        except (urllib.error.URLError, TimeoutError, ConnectionError, OSError) as e:
            raise IOError(f"download failed: {e}")

    def heartbeat(self, job_id, token, progress=None):
        """Returns True while the server still wants this job done.

        False means the job is gone (cancelled/released/expired) -- the
        worker should stop what it's doing and move on. Network trouble
        is NOT a False: we only abandon on the server's explicit word."""
        try:
            s, body = self._req("POST", f"/api/worker/heartbeat/{job_id}",
                                data=json.dumps({"token": token,
                                                 "progress": progress}).encode(),
                                ctype="application/json", timeout=15)
        except IOError:
            return True
        if s != 200:
            return True
        try:
            return bool(json.loads(body or b"{}").get("ok", True))
        except ValueError:
            return True

    def upload(self, job_id, path, token, on_progress=None):
        """POST the encoded file. Streamed in 1 MB blocks over a plain
        http.client connection so that, if the connection dies, we can
        still read WHY from the server (it often answers and closes
        before the body is sent). Raises UploadError(msg, retry=bool)."""
        size = os.path.getsize(path)
        qs = urllib.parse.urlencode({"token": token})
        conn = http.client.HTTPConnection(self.host, self.port, timeout=300)
        sent = 0
        try:
            conn.putrequest("POST", f"/api/worker/result/{job_id}?{qs}")
            conn.putheader("Content-Type", "application/octet-stream")
            conn.putheader("Content-Length", str(size))
            conn.endheaders()
            try:
                with open(path, "rb") as f:
                    while True:
                        block = f.read(1024 * 1024)
                        if not block:
                            break
                        conn.send(block)
                        sent += len(block)
                        if on_progress:
                            on_progress(sent, size)
            except (OSError, http.client.HTTPException) as e:
                # Connection died mid-send. Did the server say why?
                try:
                    r = conn.getresponse()
                    msg = r.read().decode("utf-8", "replace")[:200]
                    raise UploadError(f"the server refused it (HTTP {r.status}: {msg})",
                                      retry=False)
                except (OSError, http.client.HTTPException):
                    raise UploadError(
                        f"the connection dropped after {fmt_mb(sent)} of "
                        f"{fmt_mb(size)} ({e.__class__.__name__}). A firewall, "
                        f"antivirus or Wi-Fi hiccup is the usual suspect",
                        retry=True)
            conn.sock.settimeout(3600)  # the server verifies before answering
            r = conn.getresponse()
            body = r.read()
            if r.status != 200:
                raise UploadError(f"the server answered HTTP {r.status}",
                                  retry=r.status >= 500)
            return json.loads(body or b"{}")
        except (OSError, http.client.HTTPException) as e:
            raise UploadError(f"upload failed: {e}", retry=True)
        finally:
            conn.close()

    def fail(self, job_id, error, token):
        try:
            self._req("POST", f"/api/worker/fail/{job_id}",
                      data=json.dumps({"error": error, "token": token}).encode(),
                      ctype="application/json", timeout=15)
        except IOError:
            pass

    def release(self, job_id, token):
        """Hand the job straight back to the queue (Ctrl+C). Not a failure."""
        try:
            s, body = self._req("POST", f"/api/worker/release/{job_id}",
                                data=json.dumps({"token": token}).encode(),
                                ctype="application/json", timeout=15)
            return s == 200 and json.loads(body or b"{}").get("ok", False)
        except (IOError, ValueError):
            return False


def parse_progress(line, duration):
    """(percent, speed_x) from an ffmpeg stderr line; Nones when unknown."""
    pct, speed = None, None
    m = re.search(r"time=(\d+):(\d+):([\d.]+)", line)
    if m and duration:
        try:
            secs = int(m.group(1)) * 3600 + int(m.group(2)) * 60 + float(m.group(3))
            pct = min(100.0, 100.0 * secs / float(duration))
        except (ValueError, ZeroDivisionError):
            pass
    ms = re.search(r"speed=\s*([\d.]+)x", line)
    if ms:
        try:
            speed = float(ms.group(1))
        except ValueError:
            pass
    return pct, speed


def run_job(ff, srv, job, st, session):
    """Run one claimed job. Returns True (done), False (failed), or
    "gone" (the server cancelled it mid-job)."""
    jid = job["job_id"]
    token = job["claim_token"]
    name = job["filename"]
    total_mb = job["size"] / 1024 / 1024
    os.makedirs(WORK_DIR, exist_ok=True)
    src = os.path.join(WORK_DIR, f"{jid}_in.mkv")
    dst = os.path.join(WORK_DIR, f"{jid}_out.mkv")

    print(f"\n  ━━━ 🎯 new job ━━━")
    print(f"  📄 {name}")
    print(f"  {fmt_mb(job['size'])}  →  {job['out_w']}x{job['out_h']} HEVC")

    stop = threading.Event()
    gone = threading.Event()
    prog = {"pct": None}

    def beat():
        while not stop.wait(HEARTBEAT_SECS):
            if not srv.heartbeat(jid, token, prog["pct"]):
                gone.set()
                return

    # The heartbeat covers the whole job (download, encode AND upload) so
    # the lease can't expire mid-transfer on a big file.
    threading.Thread(target=beat, daemon=True).start()
    proc = None
    try:
        print(f"  📥 downloading…")
        t0 = time.time()
        srv.download(jid, src, total=job["size"],
                     on_progress=lambda n, t: st.show(xfer_text("📥", n, t, t0)))
        st.end()
        print(f"  ✅ downloaded in {fmt_eta(time.time() - t0)}")
        if gone.is_set():
            return "gone"

        argv = [a.replace("__INPUT__", src).replace("__OUTPUT__", dst)
                for a in job["argv"]]
        argv[0] = ff  # use the ffmpeg we checked, not whatever is first on PATH
        # Drop -stats (it spams the console); progress is parsed from stderr.
        argv = [a for a in argv if a != "-stats"]

        print(f"  🔨 encoding…")
        t0 = time.time()
        tail = []
        proc = subprocess.Popen(argv, stderr=subprocess.PIPE,
                                stdout=subprocess.DEVNULL, text=True,
                                errors="replace")
        for line in proc.stderr:
            if gone.is_set():
                proc.terminate()
                try:
                    proc.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    proc.kill()
                proc = None
                st.end()
                return "gone"
            tail.append(line)
            tail = tail[-30:]
            pct, speed = parse_progress(line, job.get("duration"))
            if pct is not None:
                prog["pct"] = pct
            if pct is not None or speed is not None:
                eta = None
                el = time.time() - t0
                if pct and pct > 1:
                    eta = el * (100 - pct) / pct
                st.show(f"  🔨 {pct:.0f}% · {speed:.1f}x speed · ETA {fmt_eta(eta)}"
                        if pct is not None and speed else
                        f"  🔨 warming up…")
        rc = proc.wait()
        proc = None
        st.end()
        if gone.is_set():
            return "gone"
        if rc != 0:
            err = "".join(tail[-8:]).strip() or f"ffmpeg exited {rc}"
            last = err.splitlines()[-1] if err else str(rc)
            print(f"  ❌ ffmpeg choked on this file.")
            print(f"     its last words: {last[:160]}")
            print(f"     telling the server — the file stays queued for another try.")
            srv.fail(jid, err[-2000:], token)
            session["failed"] += 1
            return False

        print(f"  ✅ encoded in {fmt_eta(time.time() - t0)}")
        print(f"  📤 uploading {fmt_mb(os.path.getsize(dst))} back to the server…")
        res = None
        for attempt in (1, 2, 3):
            t0 = time.time()
            try:
                res = srv.upload(jid, dst, token,
                                 on_progress=lambda n, t: st.show(
                                     xfer_text("📤", n, t, t0)))
                break
            except UploadError as e:
                st.end()
                print(f"  ⚠️ upload problem: {e}")
                if not e.retry or attempt == 3:
                    print(f"     giving up on this one — telling the server.")
                    srv.fail(jid, f"upload: {e}", token)
                    session["failed"] += 1
                    return False
                print(f"     trying again ({attempt + 1}/3)…")
                time.sleep(3)
        st.end()
        if res.get("ok"):
            session["done"] += 1
            print(f"  🎉 done! the server is placing it now.")
            print(f"     session: ✅ {session['done']} done · ❌ {session['failed']} failed")
            return True
        print(f"  ⚠️ the server didn't take it: {res.get('error') or 'unknown reason'}")
        print(f"     the file stays queued — nothing lost.")
        session["failed"] += 1
        return False
    except KeyboardInterrupt:
        st.end()
        if proc and proc.poll() is None:
            print(f"\n  🛑 stopping ffmpeg…")
            proc.terminate()
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()
        raise
    finally:
        stop.set()
        for p in (src, dst):
            try:
                os.unlink(p)
            except OSError:
                pass


def main():
    cfg = load_config()
    ff, ff_src = check_ffmpeg()
    caps = detect_caps(ff)
    srv = Server(cfg)
    st = StatusLine()
    session = {"done": 0, "failed": 0}
    banner(cfg, caps, ff_src)

    current = {}
    try:
        while True:
            try:
                job = srv.next_job(caps)
            except IOError as e:
                st.show(f"  🔌 can't reach the server ({e}). is it running? retrying…")
                time.sleep(POLL_IDLE_SECS)
                continue
            if not job:
                why = {"paused": "the queue is paused — press ▶ Start in the dashboard",
                       "empty": "nothing is queued — add a title in the dashboard",
                       "nothing": "queued titles have nothing I can take right now"
                       }.get(getattr(srv, "idle_reason", ""), "nothing queued")
                st.show(f"  💤 {why}…   "
                        f"✅ {session['done']} done · ❌ {session['failed']} failed")
                time.sleep(POLL_IDLE_SECS)
                continue
            st.end()
            current["jid"] = job["job_id"]
            current["token"] = job["claim_token"]
            try:
                outcome = run_job(ff, srv, job, st, session)
                current.clear()
            except KeyboardInterrupt:
                raise  # current stays set: the handler below releases it
            except Exception as e:  # never let one job kill the loop
                current.clear()
                print(f"  ❌ unexpected problem: {e}")
                traceback.print_exc()
                srv.fail(job["job_id"], f"worker error: {e}", job["claim_token"])
                outcome = False
                session["failed"] += 1
            if outcome == "gone":
                print(f"  ⚠️ the server cancelled that job (maybe removed from the queue).")
                print(f"     moving on — nothing lost.")
            print()
    except KeyboardInterrupt:
        st.end()
        jid, token = current.get("jid"), current.get("token")
        if jid:
            print(f"\n  👋 handing my job straight back to the queue…")
            if srv.release(jid, token):
                print(f"  ✅ released — it'll be right where you left it.")
            else:
                print(f"  ⚠️ couldn't reach the server; the job frees itself in ~10 min.")
        print(f"\n  👋 bye! this session: ✅ {session['done']} done · "
              f"❌ {session['failed']} failed")


def run():
    """Run main(), but keep the window open on an error so a double-click
    launch doesn't flash the message and vanish."""
    try:
        main()
        return
    except SystemExit as e:
        if e.code in (0, None):
            return
        print(f"\n{e.code}")
    except Exception:
        traceback.print_exc()
    if sys.stdin and sys.stdin.isatty():
        input("\nPress Enter to close...")
    sys.exit(1)


if __name__ == "__main__":
    run()
