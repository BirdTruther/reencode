#!/usr/bin/env python3
"""Web dashboard for reencode.sh.

Browse your libraries, queue shows/movies, and watch encodes live from a
browser. Python 3.8+ standard library only; encoding is still done by
reencode.sh, one title at a time.

    ./dashboard.py                      # http://<this machine>:8686
    ./dashboard.py --port 9000 --host 127.0.0.1
    REENCODE_DASHBOARD_PASSWORD=secret ./dashboard.py
"""

import argparse
import base64
import collections
import hmac
import json
import os
import re
import signal
import subprocess
import sys
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

ROOT = Path(__file__).resolve().parent
SCRIPT = ROOT / "reencode.sh"
COMMON = ROOT / "common.sh"
INDEX_HTML = ROOT / "web" / "index.html"
PROBE_CACHE = ROOT / ".dashboard_cache.json"
HISTORY_FILE = ROOT / ".dashboard_history.json"

VIDEO_EXTS = {".mkv", ".mp4", ".avi", ".m4v", ".ts"}
ENCODERS = ("vaapi", "nvenc", "software")
ANSI = re.compile(r"\x1b\[[0-9;]*m")
# Characters that would break out of a double-quoted value in reencode.conf.
UNSAFE_PATH = re.compile(r'["$`\\\x00-\x1f]')
DEFAULT_SIZE_RATIO = 0.6  # new/original size guess until real results come in
RESCAN_EVERY = 15 * 60    # pick up changes made outside the dashboard (seconds)


def now():
    return time.time()


# ── Config ─────────────────────────────────────────────────────────────────

def load_config():
    """Read reencode.conf through common.sh so bash and Python agree on it."""
    script = (
        'source "$1"; load_config >/dev/null 2>&1; '
        'printf "%s\\0" "$CONFIG_PATH" "$LOG_DIR" "$TEMP_DIR" "$TARGET_HEIGHT" '
        '"$QUALITY" "$ENCODER" "$VAAPI_DEVICE"; '
        'printf "%s\\0" "${LIBRARIES[@]}"'
    )
    out = subprocess.run(
        ["bash", "-c", script, "_", str(COMMON)],
        capture_output=True, check=True, stdin=subprocess.DEVNULL,
    ).stdout.decode("utf-8", "replace")
    parts = out.split("\0")[:-1]
    keys = ["config_path", "log_dir", "temp_dir", "target_height", "quality", "encoder", "vaapi_device"]
    cfg = dict(zip(keys, parts[:7]))
    cfg["libraries"] = [p for p in parts[7:] if p]
    for k in ("target_height", "quality"):
        try:
            cfg[k] = int(cfg[k])
        except (KeyError, ValueError):
            cfg[k] = 720 if k == "target_height" else 32
    return cfg


def validate_settings(data):
    """Return (clean_settings, errors)."""
    errors = {}
    clean = {}

    def path(key, required=True, must_exist=False):
        v = data.get(key, "")
        if not isinstance(v, str):
            errors[key] = "Must be text"
            return None
        v = v.strip()
        if not v:
            if required:
                errors[key] = "Required"
            return v
        if not v.startswith("/"):
            errors[key] = "Use an absolute path (starting with /)"
        elif UNSAFE_PATH.search(v):
            errors[key] = 'Paths can\'t contain " $ ` \\ or control characters'
        elif must_exist and not os.path.isdir(v):
            errors[key] = f"Folder not found: {v}"
        return v.rstrip("/") or "/"

    libs = data.get("libraries", [])
    if isinstance(libs, str):
        libs = libs.splitlines()
    if not isinstance(libs, list):
        libs = []
    clean_libs = []
    for lib in libs:
        if not isinstance(lib, str) or not lib.strip():
            continue
        lib = lib.strip()
        if not lib.startswith("/") or UNSAFE_PATH.search(lib):
            errors["libraries"] = f"Invalid path: {lib}"
        elif not os.path.isdir(lib):
            errors["libraries"] = f"Folder not found: {lib}"
        clean_libs.append(lib.rstrip("/") or "/")
    if not clean_libs:
        errors.setdefault("libraries", "Add at least one library folder")
    clean["libraries"] = clean_libs

    clean["log_dir"] = path("log_dir")
    clean["temp_dir"] = path("temp_dir")
    dev = path("vaapi_device", required=False)
    clean["vaapi_device"] = dev or ""

    for key, lo, hi in (("target_height", 144, 4320), ("quality", 0, 51)):
        try:
            n = int(data.get(key))
            if not lo <= n <= hi:
                raise ValueError
            clean[key] = n
        except (TypeError, ValueError):
            errors[key] = f"Whole number between {lo} and {hi}"

    enc = data.get("encoder")
    if enc not in ENCODERS:
        errors["encoder"] = "Pick one of: " + ", ".join(ENCODERS)
    clean["encoder"] = enc
    return clean, errors


def save_config(s):
    script = (
        'source "$1"; load_config >/dev/null 2>&1; shift; '
        'LIBRARIES=("$@"); LOG_DIR=$R_LOG_DIR; TEMP_DIR=$R_TEMP_DIR; '
        'TARGET_HEIGHT=$R_TARGET_HEIGHT; QUALITY=$R_QUALITY; ENCODER=$R_ENCODER; '
        'VAAPI_DEVICE=$R_VAAPI_DEVICE; write_config'
    )
    env = dict(os.environ)
    env.update({
        "R_LOG_DIR": s["log_dir"], "R_TEMP_DIR": s["temp_dir"],
        "R_TARGET_HEIGHT": str(s["target_height"]), "R_QUALITY": str(s["quality"]),
        "R_ENCODER": s["encoder"], "R_VAAPI_DEVICE": s["vaapi_device"],
    })
    subprocess.run(
        ["bash", "-c", script, "_", str(COMMON), *s["libraries"]],
        env=env, check=True, capture_output=True, stdin=subprocess.DEVNULL,
    )


# ── Library scanning ───────────────────────────────────────────────────────

def ffprobe(path):
    try:
        out = subprocess.run(
            ["ffprobe", "-v", "error", "-select_streams", "v:0",
             "-show_entries", "stream=height,codec_name:format=duration",
             "-of", "json", path],
            capture_output=True, timeout=120, stdin=subprocess.DEVNULL,
        ).stdout
        data = json.loads(out or b"{}")
    except (subprocess.SubprocessError, ValueError, OSError):
        return {"height": None, "codec": None, "duration": None}
    stream = (data.get("streams") or [{}])[0]
    try:
        duration = float(data.get("format", {}).get("duration"))
    except (TypeError, ValueError):
        duration = None
    return {"height": stream.get("height"), "codec": stream.get("codec_name"), "duration": duration}


class Library:
    """Probes every video once and caches height/codec/duration by path+size+mtime."""

    def __init__(self, app):
        self.app = app
        self.lock = threading.Lock()
        self.titles = {}  # title path -> {name, library, path, files: [...]}
        self.scanning = False
        self.scan_done = 0
        self.scan_total = 0
        self.last_scan = None
        self.pending = None  # None, or set of title paths (empty = full)
        try:
            self.cache = json.loads(PROBE_CACHE.read_text())
        except (OSError, ValueError):
            self.cache = {}
        threading.Thread(target=self._periodic, daemon=True).start()

    def _periodic(self):
        while True:
            time.sleep(RESCAN_EVERY)
            self.request_scan()

    def request_scan(self, only=None):
        with self.lock:
            if self.pending is None:
                self.pending = set(only or [])
            elif not only or not self.pending:
                self.pending = set()
            else:
                self.pending |= set(only)
            if self.scanning:
                return
            self.scanning = True
        threading.Thread(target=self._scan_loop, daemon=True).start()

    def _scan_loop(self):
        while True:
            with self.lock:
                only, self.pending = self.pending, None
                if only is None:
                    self.scanning = False
                    return
            try:
                self._scan(only)
            except Exception as e:  # keep the dashboard alive on odd filesystems
                print(f"scan error: {e}", file=sys.stderr)

    def _list_titles(self):
        titles = []
        for lib in self.app.config["libraries"]:
            try:
                entries = sorted(os.scandir(lib), key=lambda e: e.name.lower())
            except OSError:
                continue
            for e in entries:
                if e.is_dir() and not e.name.startswith("."):
                    titles.append((lib, e.path))
        return titles

    def _scan(self, only):
        titles = self._list_titles()
        if only:
            titles = [t for t in titles if t[1] in only]
        found = {}
        for lib, tpath in titles:
            files = []
            for dirpath, dirnames, filenames in os.walk(tpath):
                dirnames[:] = [d for d in dirnames if not d.startswith(".")]
                for fn in filenames:
                    if fn.startswith(".") or os.path.splitext(fn)[1].lower() not in VIDEO_EXTS:
                        continue
                    full = os.path.join(dirpath, fn)
                    try:
                        st = os.stat(full)
                    except OSError:
                        continue
                    files.append((full, st.st_size, int(st.st_mtime)))
            found[tpath] = (lib, files)

        todo = []
        for lib, files in found.values():
            for full, size, mtime in files:
                c = self.cache.get(full)
                if not c or c.get("size") != size or c.get("mtime") != mtime:
                    todo.append((full, size, mtime))

        with self.lock:
            self.scan_done, self.scan_total = 0, len(todo)

        def probe(item):
            full, size, mtime = item
            info = ffprobe(full)
            info.update(size=size, mtime=mtime)
            with self.lock:
                self.cache[full] = info
                self.scan_done += 1

        with ThreadPoolExecutor(max_workers=4) as pool:
            list(pool.map(probe, todo))

        with self.lock:
            if not only:
                self.titles = {}
                live = {f[0] for _, files in found.values() for f in files}
                self.cache = {k: v for k, v in self.cache.items() if k in live}
            for tpath, (lib, files) in found.items():
                entries = []
                for full, size, _ in sorted(files, key=lambda f: natural_key(f[0])):
                    c = self.cache.get(full, {})
                    entries.append({
                        "name": os.path.basename(full),
                        "rel": os.path.relpath(full, tpath),
                        "size": size,
                        "height": c.get("height"),
                        "codec": c.get("codec"),
                        "duration": c.get("duration"),
                    })
                self.titles[tpath] = {
                    "name": os.path.basename(tpath), "library": lib,
                    "path": tpath, "files": entries,
                }
            # Titles that vanished (e.g. renamed folder) during a partial rescan.
            if only:
                for tpath in only:
                    if tpath not in found:
                        self.titles.pop(tpath, None)
            self.last_scan = now()
            cache_copy = dict(self.cache)
        tmp = PROBE_CACHE.with_suffix(".tmp")
        try:
            tmp.write_text(json.dumps(cache_copy))
            tmp.replace(PROBE_CACHE)
        except OSError:
            pass

    def summaries(self, target):
        out = []
        with self.lock:
            titles = list(self.titles.values())
        for t in titles:
            above = at = below = unknown = 0
            size = above_size = 0
            for f in t["files"]:
                size += f["size"]
                h = f["height"]
                if not h:
                    unknown += 1
                elif h > target:
                    above += 1
                    above_size += f["size"]
                elif h == target:
                    at += 1
                else:
                    below += 1
            if not t["files"]:
                continue
            if above == 0:
                status = "done" if at else "n/a"
            else:
                status = "partial" if at else "ready"
            out.append({
                "name": t["name"], "path": t["path"], "library": t["library"],
                "files": len(t["files"]), "above": above, "at": at, "below": below,
                "unknown": unknown, "size": size, "above_size": above_size, "status": status,
            })
        return out

    def title(self, path):
        with self.lock:
            return self.titles.get(path)


def natural_key(s):
    return [int(p) if p.isdigit() else p.lower() for p in re.split(r"(\d+)", s)]


# ── Jobs ───────────────────────────────────────────────────────────────────

RE_EPISODE = re.compile(r"\[(\d+)/(\d+)\] Processing: (.*)$")
RE_DURATION = re.compile(r"Duration: ([\d.]+)s")
RE_REPLACED = re.compile(r"Replaced: .* \((\d+)MB -> (\d+)MB")
RE_ENCODED = re.compile(r"OK Encoded: .* -> (\d+)MB")
RE_FAILED = re.compile(r"ERR FAILED: (.*?)(?: \(see|$)")


class Job:
    def __init__(self, title, dry_run):
        self.id = uuid.uuid4().hex[:10]
        self.path = title["path"]
        self.name = title["name"]
        self.library = title["library"]
        self.dry_run = dry_run
        self.status = "queued"
        self.created = now()
        self.started = self.finished = None
        self.log = collections.deque(maxlen=3000)
        self.seq = 0
        self.proc = None
        self.cancel_requested = False
        self.episode = 0
        self.episodes = 0
        self.file = ""
        self.file_started = None
        self.duration = None
        self.out_time = 0.0
        self.fps = None
        self.speed = None
        self.replaced = 0
        self.orig_mb = 0
        self.new_mb = 0
        self.encoded = 0
        self.failures = []
        self.returncode = None

    def add_line(self, line):
        self.seq += 1
        self.log.append((self.seq, line))
        m = RE_EPISODE.search(line)
        if m:
            self.episode, self.episodes, self.file = int(m[1]), int(m[2]), m[3]
            self.duration, self.out_time, self.fps, self.speed = None, 0.0, None, None
            self.file_started = now()
            return
        m = RE_DURATION.search(line)
        if m:
            self.duration = float(m[1])
            return
        m = RE_REPLACED.search(line)
        if m:
            self.replaced += 1
            self.orig_mb += int(m[1])
            self.new_mb += int(m[2])
            return
        if RE_ENCODED.search(line):
            self.encoded += 1
            return
        m = RE_FAILED.search(line)
        if m:
            self.failures.append(m[1])

    def public(self):
        pct = None
        if self.duration and self.status == "running":
            pct = max(0.0, min(1.0, self.out_time / self.duration))
        eta = None
        if pct and self.file_started and pct > 0.01:
            elapsed = now() - self.file_started
            eta = elapsed / pct - elapsed
        return {
            "id": self.id, "path": self.path, "name": self.name, "library": self.library,
            "dry_run": self.dry_run, "status": self.status, "created": self.created,
            "started": self.started, "finished": self.finished, "episode": self.episode,
            "episodes": self.episodes, "file": self.file, "file_pct": pct, "file_eta": eta,
            "fps": self.fps, "speed": self.speed, "replaced": self.replaced,
            "saved_mb": self.orig_mb - self.new_mb, "orig_mb": self.orig_mb,
            "new_mb": self.new_mb, "encoded": self.encoded, "failures": self.failures[-20:],
            "returncode": self.returncode, "log_seq": self.seq,
        }

    def history_record(self):
        rec = self.public()
        rec.pop("log_seq", None)
        rec["log_tail"] = [l for _, l in list(self.log)[-400:]]
        return rec


class JobRunner:
    def __init__(self, app):
        self.app = app
        self.lock = threading.Lock()
        self.wake = threading.Event()
        self.queue = []
        self.current = None
        self.waiting_external = False
        try:
            self.history = json.loads(HISTORY_FILE.read_text())
        except (OSError, ValueError):
            self.history = []
        threading.Thread(target=self._worker, daemon=True).start()

    def enqueue(self, title, dry_run=False):
        with self.lock:
            for j in self.queue + ([self.current] if self.current else []):
                if j.path == title["path"] and j.dry_run == dry_run:
                    return j, False
            job = Job(title, dry_run)
            self.queue.append(job)
        self.wake.set()
        return job, True

    def cancel(self, job_id):
        with self.lock:
            for j in self.queue:
                if j.id == job_id:
                    self.queue.remove(j)
                    j.status, j.finished = "cancelled", now()
                    self._record(j)
                    return True
            j = self.current
            if j and j.id == job_id and j.proc:
                j.cancel_requested = True
                try:
                    os.killpg(j.proc.pid, signal.SIGTERM)
                except ProcessLookupError:
                    pass
                return True
        return False

    def move(self, job_id, delta):
        with self.lock:
            for i, j in enumerate(self.queue):
                if j.id == job_id:
                    k = max(0, min(len(self.queue) - 1, i + delta))
                    self.queue.insert(k, self.queue.pop(i))
                    return True
        return False

    def find(self, job_id):
        with self.lock:
            if self.current and self.current.id == job_id:
                return self.current
            for j in self.queue:
                if j.id == job_id:
                    return j
        return None

    def history_entry(self, job_id):
        with self.lock:
            for h in self.history:
                if h["id"] == job_id:
                    return h
        return None

    def _record(self, job):
        # caller holds self.lock
        self.history.insert(0, job.history_record())
        del self.history[200:]
        tmp = HISTORY_FILE.with_suffix(".tmp")
        try:
            tmp.write_text(json.dumps(self.history))
            tmp.replace(HISTORY_FILE)
        except OSError:
            pass

    def size_ratio(self):
        with self.lock:
            orig = sum(h.get("orig_mb", 0) for h in self.history if not h.get("dry_run"))
            new = sum(h.get("new_mb", 0) for h in self.history if not h.get("dry_run"))
        if orig >= 1024:  # need a GB of real results before trusting it
            return new / orig
        return DEFAULT_SIZE_RATIO

    def _worker(self):
        while True:
            self.wake.wait(5)
            self.wake.clear()
            while True:
                with self.lock:
                    if not self.queue:
                        break
                # Don't fight a terminal-started encode for the GPU.
                if external_encodes():
                    self.waiting_external = True
                    time.sleep(5)
                    continue
                self.waiting_external = False
                with self.lock:
                    if not self.queue:
                        break
                    job = self.queue.pop(0)
                    self.current = job
                try:
                    self._run(job)
                except Exception as e:
                    job.add_line(f"dashboard error: {e}")
                    job.status = "failed"
                with self.lock:
                    job.finished = job.finished or now()
                    self.current = None
                    self._record(job)
                self.app.library.request_scan([job.path])

    def _run(self, job):
        cfg = self.app.config
        job.status, job.started = "running", now()
        os.makedirs(cfg["temp_dir"], exist_ok=True)
        os.makedirs(cfg["log_dir"], exist_ok=True)
        progress = os.path.join(cfg["temp_dir"], f".progress-{job.id}")
        logname = job.name.replace(" ", "_").replace("!", "") + ".log"
        env = dict(os.environ, REENCODE_PROGRESS=progress)
        cmd = ["bash", str(SCRIPT), "--dir", job.path] + (["--dry-run"] if job.dry_run else [])

        job.proc = subprocess.Popen(
            cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
            start_new_session=True, env=env,
        )
        stop = threading.Event()
        threading.Thread(target=self._poll_progress, args=(job, progress, stop), daemon=True).start()
        try:
            with open(os.path.join(cfg["log_dir"], logname), "w", encoding="utf-8") as lf:
                for raw in job.proc.stdout:
                    line = ANSI.sub("", raw.decode("utf-8", "replace")).rstrip("\n")
                    lf.write(line + "\n")
                    lf.flush()
                    job.add_line(line)
            job.returncode = job.proc.wait()
        finally:
            stop.set()
            try:
                os.remove(progress)
            except OSError:
                pass
        if job.cancel_requested:
            job.status = "cancelled"
        elif job.returncode == 0:
            job.status = "done"
        else:
            job.status = "failed"
        job.finished = now()

    @staticmethod
    def _poll_progress(job, path, stop):
        while not stop.wait(1):
            try:
                with open(path, "rb") as f:
                    f.seek(max(0, os.path.getsize(path) - 4096))
                    tail = f.read().decode("utf-8", "replace")
            except OSError:
                continue
            block = tail.rsplit("progress=", 2)
            vals = {}
            for line in (block[-2] if len(block) > 1 else tail).splitlines():
                if "=" in line:
                    k, v = line.split("=", 1)
                    vals[k.strip()] = v.strip()
            try:
                if vals.get("out_time_us", "N/A") != "N/A":
                    job.out_time = int(vals["out_time_us"]) / 1e6
                job.fps = float(vals["fps"]) if vals.get("fps") else None
                sp = vals.get("speed", "").rstrip("x")
                job.speed = float(sp) if sp and sp != "N/A" else None
            except ValueError:
                pass

    def state(self):
        with self.lock:
            return {
                "current": self.current.public() if self.current else None,
                "queue": [j.public() for j in self.queue],
                "history": [{k: v for k, v in h.items() if k != "log_tail"} for h in self.history[:50]],
                "waiting_external": self.waiting_external,
            }

    def saved_total_mb(self):
        with self.lock:
            return sum(h.get("saved_mb", 0) for h in self.history if not h.get("dry_run"))


def external_encodes():
    """reencode.sh runs not started by this dashboard (e.g. from encodetv)."""
    job = _APP.jobs.current if _APP else None
    own = job.proc.pid if job and job.proc else None
    found = {}
    try:
        pids = [p for p in os.listdir("/proc") if p.isdigit()]
    except OSError:
        return []
    for pid in pids:
        try:
            with open(f"/proc/{pid}/cmdline", "rb") as f:
                argv = f.read().split(b"\0")
            if len(argv) < 2 or not argv[1].endswith(b"reencode.sh"):
                continue
            with open(f"/proc/{pid}/stat", "rb") as f:
                stat = f.read()
            sid = int(stat[stat.rindex(b")") + 2:].split()[3])
        except (OSError, ValueError, IndexError):
            continue
        if sid == own:
            continue
        found.setdefault(sid, " ".join(a.decode("utf-8", "replace") for a in argv[1:] if a))
    return list(found.values())


_APP = None


# ── App / HTTP ─────────────────────────────────────────────────────────────

class App:
    def __init__(self, password):
        self.password = password
        self.config = load_config()
        self.library = Library(self)
        self.jobs = JobRunner(self)

    def reload_config(self):
        self.config = load_config()

    def state(self):
        cfg = self.config
        target = cfg["target_height"]
        titles = self.library.summaries(target)
        ratio = self.jobs.size_ratio()
        to_encode = sum(t["above_size"] for t in titles)
        lib = self.library
        with lib.lock:
            scan = {"running": lib.scanning, "done": lib.scan_done, "total": lib.scan_total,
                    "last": lib.last_scan}
        return {
            "config": cfg,
            "scan": scan,
            "titles": titles,
            "stats": {
                "library_bytes": sum(t["size"] for t in titles),
                "files": sum(t["files"] for t in titles),
                "to_encode_files": sum(t["above"] for t in titles),
                "to_encode_bytes": to_encode,
                "est_savings_bytes": int(to_encode * (1 - ratio)),
                "size_ratio": ratio,
                "saved_mb": self.jobs.saved_total_mb(),
            },
            "jobs": self.jobs.state(),
            "external": external_encodes(),
            "time": now(),
        }


class Handler(BaseHTTPRequestHandler):
    server_version = "reencode-dashboard"
    app = None

    def log_message(self, fmt, *args):
        pass

    # helpers
    def _send(self, code, body, ctype="application/json"):
        if not isinstance(body, bytes):
            body = json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.end_headers()
        self.wfile.write(body)

    def _authed(self):
        pw = self.app.password
        if not pw:
            return True
        h = self.headers.get("Authorization", "")
        if h.startswith("Basic "):
            try:
                _, _, given = base64.b64decode(h[6:]).decode().partition(":")
                if hmac.compare_digest(given.encode(), pw.encode()):
                    return True
            except ValueError:
                pass
        self.send_response(401)
        self.send_header("WWW-Authenticate", 'Basic realm="reencode"')
        self.send_header("Content-Length", "0")
        self.end_headers()
        return False

    def _json_body(self):
        # Requiring a JSON content type plus a same-origin check stops other
        # websites from driving the dashboard through your browser.
        if self.headers.get("Content-Type", "").split(";")[0].strip() != "application/json":
            return None
        origin = self.headers.get("Origin")
        if origin and urlparse(origin).netloc != self.headers.get("Host"):
            return None
        n = int(self.headers.get("Content-Length") or 0)
        if n > 1_000_000:
            return None
        try:
            data = json.loads(self.rfile.read(n) or b"{}")
        except ValueError:
            return None
        return data if isinstance(data, dict) else None

    def do_GET(self):
        if not self._authed():
            return
        url = urlparse(self.path)
        q = parse_qs(url.query)
        parts = url.path.strip("/").split("/")
        app = self.app

        if url.path in ("/", "/index.html"):
            try:
                return self._send(200, INDEX_HTML.read_bytes(), "text/html; charset=utf-8")
            except OSError:
                return self._send(500, {"error": "web/index.html missing"})
        if url.path == "/api/state":
            return self._send(200, app.state())
        if url.path == "/api/title":
            t = app.library.title(q.get("path", [""])[0])
            if not t:
                return self._send(404, {"error": "Not found"})
            return self._send(200, t)
        if url.path == "/api/settings":
            return self._send(200, app.config)
        if len(parts) == 4 and parts[:2] == ["api", "jobs"] and parts[3] == "log":
            since = int((q.get("since") or ["0"])[0] or 0)
            job = app.jobs.find(parts[2])
            if job:
                lines = [(s, l) for s, l in list(job.log) if s > since]
                return self._send(200, {"seq": job.seq, "lines": [l for _, l in lines], "live": True})
            h = app.jobs.history_entry(parts[2])
            if h:
                return self._send(200, {"seq": 0, "lines": h.get("log_tail", []), "live": False})
            return self._send(404, {"error": "Not found"})
        return self._send(404, {"error": "Not found"})

    def do_POST(self):
        if not self._authed():
            return
        data = self._json_body()
        if data is None:
            return self._send(400, {"error": "Bad request"})
        url = urlparse(self.path)
        parts = url.path.strip("/").split("/")
        app = self.app

        if url.path == "/api/jobs":
            titles = data.get("paths") or [data.get("path")]
            dry = bool(data.get("dry_run"))
            added = 0
            for p in titles:
                t = app.library.title(p) if isinstance(p, str) else None
                if not t:
                    return self._send(404, {"error": f"Unknown title: {p}"})
                _, new = app.jobs.enqueue(t, dry)
                added += new
            return self._send(200, {"added": added})
        if len(parts) == 4 and parts[:2] == ["api", "jobs"] and parts[3] == "cancel":
            ok = app.jobs.cancel(parts[2])
            return self._send(200 if ok else 404, {"ok": ok})
        if len(parts) == 4 and parts[:2] == ["api", "jobs"] and parts[3] in ("up", "down"):
            ok = app.jobs.move(parts[2], -1 if parts[3] == "up" else 1)
            return self._send(200 if ok else 404, {"ok": ok})
        if url.path == "/api/scan":
            app.library.request_scan()
            return self._send(200, {"ok": True})
        if url.path == "/api/settings":
            clean, errors = validate_settings(data)
            if errors:
                return self._send(422, {"errors": errors})
            try:
                save_config(clean)
                app.reload_config()
            except subprocess.CalledProcessError as e:
                return self._send(500, {"error": e.stderr.decode("utf-8", "replace")})
            app.library.request_scan()
            return self._send(200, app.config)
        return self._send(404, {"error": "Not found"})


def main():
    global _APP
    ap = argparse.ArgumentParser(description="Web dashboard for reencode.sh")
    ap.add_argument("--host", default=os.environ.get("REENCODE_DASHBOARD_HOST", "0.0.0.0"),
                    help="address to listen on (default 0.0.0.0 = whole LAN; 127.0.0.1 = this machine only)")
    ap.add_argument("--port", type=int, default=int(os.environ.get("REENCODE_DASHBOARD_PORT", 8686)))
    ap.add_argument("--password", default=os.environ.get("REENCODE_DASHBOARD_PASSWORD", ""),
                    help="require this password (any username) via HTTP basic auth")
    args = ap.parse_args()

    _APP = App(args.password)
    Handler.app = _APP
    _APP.library.request_scan()

    server = ThreadingHTTPServer((args.host, args.port), Handler)
    server.daemon_threads = True
    shown = "localhost" if args.host in ("0.0.0.0", "") else args.host
    print(f"reencode dashboard on http://{shown}:{args.port}  (config: {_APP.config['config_path']})")
    if args.host in ("0.0.0.0", "", "::") and not args.password:
        print("  Listening on all interfaces with no password; set REENCODE_DASHBOARD_PASSWORD "
              "or use --host 127.0.0.1 if this machine isn't on a trusted network.")
    if not _APP.config["libraries"]:
        print("  No libraries configured yet - open the dashboard and use Settings.")

    def shutdown(*_):
        raise KeyboardInterrupt

    signal.signal(signal.SIGTERM, shutdown)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        # Stop a running encode cleanly; reencode.sh removes its partial output.
        job = _APP.jobs.current
        if job and job.proc and job.proc.poll() is None:
            print("Stopping current encode...")
            job.cancel_requested = True
            try:
                os.killpg(job.proc.pid, signal.SIGTERM)
                job.proc.wait(timeout=30)
            except (ProcessLookupError, subprocess.TimeoutExpired):
                pass


if __name__ == "__main__":
    main()
