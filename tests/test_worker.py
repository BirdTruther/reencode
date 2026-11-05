#!/usr/bin/env python3
"""Remote worker (v0): --worker-cmd / --worker-place in reencode.sh, and the
dashboard's claim -> encode -> finish loop, end to end.

    python3 tests/test_worker.py    # needs ffmpeg/ffprobe
"""

import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))
os.environ.pop("REENCODE_CONFIG", None)
import dashboard  # noqa: E402
from test_plan import make_video, run_ffmpeg  # noqa: E402


def write_conf(base):
    conf = base / "reencode.conf"
    conf.write_text(f"""LIBRARIES=("{base}/Movies")
LIBRARY_PROFILES=("movies")
LOG_DIR="{base}/logs"
TEMP_DIR="{base}/tmp"
MOVIES_HEIGHT=720
MOVIES_QUALITY=28
MOVIES_KEEP_4K="if-other-version"
ENCODER="software"
""")
    return conf


def worker_cmd(conf, title_dir, path, encoder="nvenc", decode="gpu"):
    env = dict(os.environ, REENCODE_CONFIG=str(conf))
    r = subprocess.run(
        ["bash", str(ROOT / "reencode.sh"), "--worker-cmd", "--dir", str(title_dir),
         "--file", str(path), "--worker-encoder", encoder, "--worker-decode", decode],
        capture_output=True, text=True, env=env)
    try:
        data = json.loads(r.stdout or "{}")
    except ValueError:
        data = {}
    return r.returncode, data


def worker_place(conf, title_dir, src, result):
    env = dict(os.environ, REENCODE_CONFIG=str(conf))
    r = subprocess.run(
        ["bash", str(ROOT / "reencode.sh"), "--worker-place",
         "--dir", str(title_dir), "--file", str(src), "--result", str(result)],
        capture_output=True, text=True, env=env)
    lines = (r.stdout or "").strip().splitlines()
    try:
        data = json.loads(lines[-1]) if lines else {}
    except ValueError:
        data = {}
    return r.returncode, data


class WorkerCmdTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        base = Path(cls.tmp.name)
        make_video(base / "Movies" / "Film" / "Film 1080p.mkv", 1080)
        cls.base = base
        cls.conf = write_conf(base)

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def test_cmd_is_valid_nvenc_json(self):
        d = self.base / "Movies" / "Film"
        rc, data = worker_cmd(self.conf, d, d / "Film 1080p.mkv")
        self.assertEqual(rc, 0)
        self.assertEqual(data["action"], "encode")
        self.assertEqual((data["out_w"], data["out_h"]), (1280, 720))
        self.assertIn("__INPUT__", data["argv"])
        self.assertIn("__OUTPUT__", data["argv"])
        self.assertIn("hevc_nvenc", data["argv"])
        self.assertIn("cuda", data["argv"])  # gpu decode requested
        self.assertIn("scale_cuda=1280:720", data["argv"])

    def test_cmd_cpu_decode_for_software_worker(self):
        d = self.base / "Movies" / "Film"
        rc, data = worker_cmd(self.conf, d, d / "Film 1080p.mkv",
                              encoder="software", decode="cpu")
        self.assertEqual(rc, 0)
        self.assertIn("libx265", data["argv"])
        self.assertNotIn("cuda", data["argv"])

    def test_cmd_no_work_is_an_error(self):
        d = self.base / "Movies" / "Film"
        done = d / "Film 720p.mkv"
        make_video(done, 720)
        try:
            rc, data = worker_cmd(self.conf, d, done)
            self.assertEqual(rc, 2)
            self.assertIn("error", data)
        finally:
            done.unlink()


class WorkerPlaceTest(unittest.TestCase):
    def test_place_encode_replaces_original(self):
        with tempfile.TemporaryDirectory() as t:
            base = Path(t)
            d = base / "Movies" / "Film"
            src = d / "Film 1080p.mkv"
            make_video(src, 1080)
            conf = write_conf(base)
            # A worker-encoded result, tiny on purpose so the savings
            # check in replace_original lets it through.
            result = base / "upload.mkv"
            run_ffmpeg("-i", str(src), "-vf", "scale=1280:720",
                       "-c:v", "libx265", "-preset", "ultrafast", "-crf", "51",
                       str(result))
            rc, data = worker_place(conf, d, src, result)
            self.assertEqual(rc, 0, data)
            self.assertTrue(data.get("ok"), data)
            out = d / "Film 720p.mkv"
            self.assertTrue(out.exists())
            info = dashboard.ffprobe(str(out))
            self.assertEqual((info["width"], info["height"], info["codec"]),
                             (1280, 720, "hevc"))

    def test_place_makecopy_keeps_original(self):
        with tempfile.TemporaryDirectory() as t:
            base = Path(t)
            d = base / "Movies" / "Solo"
            src = d / "Solo 2160p.mkv"
            make_video(src, 2160)
            conf = write_conf(base)
            (base / "title_4k.tsv").write_text(f"copy\t{d}\n")
            result = base / "upload.mkv"
            run_ffmpeg("-i", str(src), "-vf", "scale=1280:720",
                       "-c:v", "libx265", "-preset", "ultrafast", str(result))
            rc, data = worker_place(conf, d, src, result)
            self.assertEqual(rc, 0, data)
            self.assertTrue(data.get("ok"), data)
            self.assertTrue(src.exists(), "4K original must survive")
            self.assertTrue((d / "Solo 720p.mkv").exists())

    def test_place_rejects_bad_result(self):
        with tempfile.TemporaryDirectory() as t:
            base = Path(t)
            d = base / "Movies" / "Film"
            src = d / "Film 1080p.mkv"
            make_video(src, 1080)
            conf = write_conf(base)
            bad = base / "bad.mkv"
            bad.write_bytes(b"not a video")
            rc, data = worker_place(conf, d, src, bad)
            self.assertNotEqual(rc, 0)
            self.assertFalse(data.get("ok"))
            self.assertTrue(src.exists(), "original must be untouched")


class FakeLibrary:
    def __init__(self, titles):
        self.titles = titles
        self.lock = threading.Lock()
        self.scans_requested = []

    def request_scan(self, paths):
        self.scans_requested.extend(paths)


class FakeJobRunner:
    """Duck-typed stand-in for JobRunner: real Job objects, no threads."""

    def __init__(self):
        self.lock = threading.Lock()
        self.queue = []
        self.current = None
        self.paused = False
        self.history = []
        self.started_at = 0  # long ago: no startup grace in tests

    def enqueue(self, title_dir):
        job = dashboard.Job({"path": str(title_dir), "name": title_dir.name,
                             "library": str(title_dir.parent)},
                            dry_run=False)
        with self.lock:
            self.queue.append(job)
        return job

    def _record(self, job):
        self.history.append(job.history_record())


class WorkerProtocolTest(unittest.TestCase):
    """v1: the worker only takes explicitly queued titles -- never a
    library scan. Claim -> run the server's own argv -> finish, end to end."""

    def _make_app(self, base, conf):
        dashboard.set_state_dir(base / "state")
        app = dashboard.App.__new__(dashboard.App)
        old = os.environ.get("REENCODE_CONFIG")
        os.environ["REENCODE_CONFIG"] = str(conf)
        self.addCleanup(os.environ.pop, "REENCODE_CONFIG", None)
        if old is not None:
            self.addCleanup(os.environ.__setitem__, "REENCODE_CONFIG", old)
        app.config = dashboard.load_config()
        app.overrides = dashboard.read_overrides(app.config["overrides_file"])
        app.keep4k_overrides = dashboard.read_4k_overrides(app.config["overrides_4k_file"])
        lib = str(base / "Movies")
        titles = {}
        for title_dir in sorted((base / "Movies").iterdir()):
            if not title_dir.is_dir():
                continue
            files = []
            for p in sorted(title_dir.rglob("*")):
                if p.suffix in (".mkv", ".mp4"):
                    info = dashboard.ffprobe(str(p))
                    # same shape as Library.scan() entries: "rel", not "path"
                    files.append({"name": p.name,
                                  "rel": os.path.relpath(str(p), str(title_dir)),
                                  "height": info["height"], "cls": info["cls"],
                                  "codec": info["codec"],
                                  "size": p.stat().st_size,
                                  "duration": info["duration"] or 0})
            titles[str(title_dir)] = {"path": str(title_dir), "library": lib,
                                      "name": title_dir.name, "files": files}
        app.library = FakeLibrary(titles)
        app.worker_lock = threading.Lock()
        app.workers_seen = {}
        app.jobs = FakeJobRunner()
        return app

    def _ctx(self, *names):
        """Temp library with one 1080p file per title name; returns
        (base, conf, app, {name: title_dir})."""
        t = tempfile.TemporaryDirectory()
        self.addCleanup(t.cleanup)
        base = Path(t.name)
        dirs = {}
        for name in names:
            d = base / "Movies" / name
            make_video(d / f"{name} 1080p.mkv", 1080)
            dirs[name] = d
        conf = write_conf(base)
        return base, conf, self._make_app(base, conf), dirs

    def test_no_queue_no_work(self):
        # THE Doctor Strange regression test: actionable files in the
        # library, but nothing queued -> the worker gets nothing.
        _base, _conf, app, _dirs = self._ctx("Film")
        self.assertIsNone(app.worker_next("w1", ["software"]))

    def test_paused_queue_no_work(self):
        _base, _conf, app, dirs = self._ctx("Film")
        app.jobs.enqueue(dirs["Film"])
        app.jobs.paused = True
        self.assertIsNone(app.worker_next("w1", ["software"]))
        self.assertEqual(app.worker_idle["w1"], "paused")
        app.jobs.paused = False
        self.assertIsNotNone(app.worker_next("w1", ["software"]))

    def test_claim_run_finish_end_to_end(self):
        base, _conf, app, dirs = self._ctx("Film")
        d = dirs["Film"]
        src = d / "Film 1080p.mkv"
        app.jobs.enqueue(d)

        job = app.worker_next("w1", ["software"])
        self.assertIsNotNone(job)
        self.assertIn("claim_token", job)
        self.assertIn("libx265", job["argv"])
        # claimed: nothing left for anyone else
        self.assertIsNone(app.worker_next("w2", ["software"]))

        # the worker runs the server's command verbatim
        out = base / "out.mkv"
        argv = [a.replace("__INPUT__", str(src)).replace("__OUTPUT__", str(out))
                for a in job["argv"]]
        subprocess.run(argv, capture_output=True, check=True)

        res = app.worker_finish(job["job_id"], str(out), job["claim_token"])
        self.assertTrue(res["ok"], res)
        self.assertFalse(src.exists())
        done = d / "Film 720p.mkv"
        self.assertTrue(done.exists())
        info = dashboard.ffprobe(str(done))
        self.assertEqual(info["codec"], "hevc")
        # title exhausted -> retired from the queue, not offered again
        self.assertIsNone(app.worker_next("w1", ["software"]))
        self.assertEqual(app.jobs.queue, [])
        self.assertEqual(len(app.jobs.history), 1)

    def test_release_returns_job_cleanly(self):
        _base, _conf, app, dirs = self._ctx("Film")
        app.jobs.enqueue(dirs["Film"])

        job = app.worker_next("w1", ["software"])
        self.assertIsNotNone(job)
        # Ctrl+C: release with the token -> back in the queue, no failure
        self.assertTrue(app.worker_release(job["job_id"], job["claim_token"]))
        # the file is offered again immediately...
        job2 = app.worker_next("w1", ["software"])
        self.assertIsNotNone(job2)
        self.assertEqual(job2["filename"], job["filename"])
        # ...wrong token cannot release it...
        self.assertFalse(app.worker_release(job2["job_id"], "bogus"))
        # ...but the real token can, and it still counts no failure
        self.assertTrue(app.worker_release(job2["job_id"], job2["claim_token"]))
        job3 = app.worker_next("w2", ["software"])
        self.assertIsNotNone(job3)
        self.assertEqual(job3["filename"], job["filename"])
        jobs = app._load_worker_jobs()
        fails = [j for j in jobs.values() if j["status"] == "failed"]
        self.assertEqual(fails, [])

    def test_heartbeat_carries_progress(self):
        _base, _conf, app, dirs = self._ctx("Film")
        app.jobs.enqueue(dirs["Film"])
        job = app.worker_next("w1", ["software"])
        self.assertTrue(app.worker_heartbeat(job["job_id"], job["claim_token"], 47.5))
        st = app.worker_status()
        self.assertEqual(st["recent"][0]["progress"], 47.5)
        # and it shows up on the queued title for the UI
        merged = {"queue": [{"path": str(dirs["Film"]), "name": "Film"}]}
        app._merge_worker_info(merged)
        row = merged["queue"][0]["workers"][0]
        self.assertEqual(row["worker"], "w1")
        self.assertEqual(row["progress"], 47.5)

    def test_local_runner_skips_worker_title(self):
        _base, _conf, app, dirs = self._ctx("Film", "Other")
        app.jobs.enqueue(dirs["Film"])
        app.jobs.enqueue(dirs["Other"])
        job = app.worker_next("w1", ["software"])
        self.assertIsNotNone(job)
        # local runner must not touch Film while the worker owns a file
        nxt = app._local_next_title()
        self.assertIsNotNone(nxt)
        self.assertEqual(nxt.path, str(dirs["Other"]))

    def test_local_yields_to_idle_worker(self):
        _base, _conf, app, dirs = self._ctx("Film")
        app.jobs.enqueue(dirs["Film"])
        # worker seen recently, idle, capable -> local waits
        app.workers_seen["w1"] = {"caps": ["software"], "last_seen": time.time()}
        self.assertIsNone(app._local_next_title())
        # worker gone quiet -> local takes it
        app.workers_seen["w1"] = {"caps": ["software"],
                                  "last_seen": time.time() - 120}
        nxt = app._local_next_title()
        self.assertIsNotNone(nxt)
        self.assertEqual(nxt.path, str(dirs["Film"]))

    def test_local_takes_file_the_worker_gave_up_on(self):
        _base, _conf, app, dirs = self._ctx("Film")
        app.jobs.enqueue(dirs["Film"])
        app.workers_seen["w1"] = {"caps": ["software"], "last_seen": time.time()}
        for _ in range(3):
            job = app.worker_next("w1", ["software"])
            app.worker_fail(job["job_id"], "boom", job["claim_token"])
        app.workers_seen["w1"] = {"caps": ["software"], "last_seen": time.time()}
        # parked after 3 failures: the idle worker can't claim it, so the
        # local runner must take it instead of waiting forever
        self.assertIsNone(app.worker_next("w1", ["software"]))
        nxt = app._local_next_title()
        self.assertIsNotNone(nxt)
        self.assertEqual(nxt.path, str(dirs["Film"]))

    def test_startup_grace_waits_for_worker_checkin(self):
        _base, _conf, app, dirs = self._ctx("Film")
        app.jobs.enqueue(dirs["Film"])
        # dashboard just started, no worker seen yet -> local waits
        app.jobs.started_at = time.time()
        self.assertIsNone(app._local_next_title())
        # ...but not forever: after the grace, local takes it
        app.jobs.started_at = time.time() - 30
        nxt = app._local_next_title()
        self.assertIsNotNone(nxt)
        self.assertEqual(nxt.path, str(dirs["Film"]))

    def test_local_runner_retires_worker_finished_title(self):
        _base, _conf, app, dirs = self._ctx("Film")
        qj = app.jobs.enqueue(dirs["Film"])
        job = app.worker_next("w1", ["software"])
        # worker did the only file; local runner sees nothing left and
        # retires the title instead of re-taking it
        jobs = app._load_worker_jobs()
        jobs[job["job_id"]]["status"] = "done"
        app._save_worker_jobs(jobs)
        self.assertIsNone(app._local_next_title())
        self.assertEqual(app.jobs.queue, [])
        self.assertEqual(len(app.jobs.history), 1)

    def test_lease_expiry_releases_job(self):
        _base, _conf, app, dirs = self._ctx("Film")
        app.jobs.enqueue(dirs["Film"])
        job = app.worker_next("w1", ["software"])
        self.assertIsNotNone(job)
        self.assertIsNone(app.worker_next("w2", ["software"]))
        # worker vanishes: expire its lease by hand
        jobs = app._load_worker_jobs()
        jobs[job["job_id"]]["lease_expires"] = time.time() - 1
        app._save_worker_jobs(jobs)
        job2 = app.worker_next("w2", ["software"])
        self.assertIsNotNone(job2)
        self.assertEqual(job2["filename"], job["filename"])

    def test_wrong_token_cannot_heartbeat_or_finish(self):
        _base, _conf, app, dirs = self._ctx("Film")
        app.jobs.enqueue(dirs["Film"])
        job = app.worker_next("w1", ["software"])
        self.assertIsNotNone(job)
        self.assertFalse(app.worker_heartbeat(job["job_id"], "bogus"))
        res = app.worker_finish(job["job_id"], "/nonexistent.mkv", "bogus")
        self.assertFalse(res["ok"])
        # the real token still works
        self.assertTrue(app.worker_heartbeat(job["job_id"], job["claim_token"]))

    def test_cancel_drops_worker_claim(self):
        _base, _conf, app, dirs = self._ctx("Film")
        app.jobs.enqueue(dirs["Film"])
        job = app.worker_next("w1", ["software"])
        self.assertIsNotNone(job)
        app.worker_drop_title(str(dirs["Film"]))
        # the worker's next heartbeat reports the job gone
        self.assertFalse(app.worker_heartbeat(job["job_id"], job["claim_token"]))
        # and the file is claimable again once re-queued
        app.jobs.enqueue(dirs["Film"])
        self.assertIsNotNone(app.worker_next("w2", ["software"]))

    def test_file_parked_after_three_failures(self):
        _base, _conf, app, dirs = self._ctx("Film")
        app.jobs.enqueue(dirs["Film"])
        for _ in range(3):
            job = app.worker_next("w1", ["software"])
            self.assertIsNotNone(job)
            self.assertTrue(
                app.worker_fail(job["job_id"], "boom", job["claim_token"]))
        # three strikes: no longer offered, even though nothing succeeded
        self.assertIsNone(app.worker_next("w1", ["software"]))
        self.assertIsNone(app.worker_next("w2", ["nvenc", "cuda"]))

    def test_av1_jobs_need_av1d_cap(self):
        with tempfile.TemporaryDirectory() as t:
            base = Path(t)
            d = base / "Movies" / "AV1Film"
            src = d / "AV1Film 1080p.mkv"
            src.parent.mkdir(parents=True, exist_ok=True)
            # libsvtav1 isn't in every ffmpeg build; libaom-av1 is the fallback
            try:
                run_ffmpeg("-f", "lavfi", "-i", "color=size=1920x1080:duration=0.2",
                           "-c:v", "libsvtav1", "-preset", "10", str(src))
            except subprocess.CalledProcessError:
                try:
                    run_ffmpeg("-f", "lavfi", "-i", "color=size=1920x1080:duration=0.2",
                               "-c:v", "libaom-av1", "-cpu-used", "8", str(src))
                except subprocess.CalledProcessError:
                    self.skipTest("this ffmpeg has no AV1 encoder")
            self.assertEqual(dashboard.ffprobe(str(src))["codec"], "av1")
            conf = write_conf(base)
            app = self._make_app(base, conf)
            app.jobs.enqueue(d)
            # a worker that can't decode AV1 must not get the job...
            self.assertIsNone(app.worker_next("cpuonly", ["software"]))
            # ...but one that can, does
            job = app.worker_next("gpu", ["nvenc", "cuda", "av1d"])
            self.assertIsNotNone(job)
            self.assertEqual(job["filename"], src.name)


class WorkerUploadTest(unittest.TestCase):
    """worker.py's upload: reads WHY the server refused, and succeeds
    normally. Uses tiny fake servers, no ffmpeg needed."""

    @classmethod
    def setUpClass(cls):
        import importlib.util
        spec = importlib.util.spec_from_file_location("worker_mod", ROOT / "worker" / "worker.py")
        cls.w = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cls.w)

    def _serve(self, handler_cls):
        import http.server
        srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler_cls)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        self.addCleanup(srv.shutdown)
        return srv.server_address[1]

    def _file(self, mb):
        t = tempfile.NamedTemporaryFile(delete=False)
        t.write(os.urandom(1024 * 1024) * mb)
        t.close()
        self.addCleanup(os.unlink, t.name)
        return t.name

    def test_upload_ok(self):
        import http.server

        class H(http.server.BaseHTTPRequestHandler):
            def do_POST(self):
                n = int(self.headers["Content-Length"])
                while n > 0:
                    n -= len(self.rfile.read(min(n, 1 << 20)))
                body = b'{"ok": true}'
                self.send_response(200)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *a):
                pass

        port = self._serve(H)
        srv = self.w.Server({"host": "127.0.0.1", "port": port, "name": "t"})
        seen = []
        res = srv.upload("j", self._file(5), "tok", on_progress=lambda n, t: seen.append(n))
        self.assertTrue(res["ok"])
        self.assertEqual(seen[-1], 5 * 1024 * 1024)

    def test_upload_refused_early_reports_server_reason(self):
        import http.server

        class H(http.server.BaseHTTPRequestHandler):
            def do_POST(self):  # refuse without reading the body, then close
                body = b'{"error": "unknown job"}'
                self.send_response(404)
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Connection", "close")
                self.end_headers()
                self.wfile.write(body)
                self.close_connection = True

            def log_message(self, *a):
                pass

        port = self._serve(H)
        srv = self.w.Server({"host": "127.0.0.1", "port": port, "name": "t"})
        with self.assertRaises(self.w.UploadError) as cm:
            srv.upload("j", self._file(60), "bad")
        self.assertFalse(cm.exception.retry)
        self.assertIn("404", str(cm.exception))


class WorkerDisplayBase(unittest.TestCase):
    """A real App with hand-written job records; no encoding, no ffmpeg."""

    def setUp(self):
        t = tempfile.TemporaryDirectory()
        self.addCleanup(t.cleanup)
        dashboard.set_state_dir(Path(t.name))
        app = dashboard.App.__new__(dashboard.App)
        app.library = FakeLibrary({})
        app.worker_lock = threading.Lock()
        app.workers_seen = {}
        app.jobs = FakeJobRunner()
        self.app = app
        self.now = time.time()
        self.jobs = {}

    def add(self, worker, title, name, status="claimed", age=0, progress=None,
            lease_left=300, error=""):
        jid = f"j{len(self.jobs)}"
        self.jobs[jid] = {
            "id": jid, "title": f"/media/TV/{title}", "file": f"/media/TV/{title}/{name}",
            "filename": name, "worker": worker, "caps": ["cuda", "nvenc"],
            "action": "encode", "status": status, "progress": progress,
            "claim_token": "secret-token", "attempts": 1, "error": error,
            "claimed_at": self.now - age, "lease_expires": self.now + lease_left,
        }
        self.app._save_worker_jobs(self.jobs)

    def queue(self, title):
        self.app.jobs.enqueue(Path("/media/TV") / title)

    def merged(self, *titles):
        state = {"queue": [{"path": f"/media/TV/{t}", "name": t, "dry_run": False}
                           for t in titles]}
        self.app._merge_worker_info(state)
        return state


class WorkerDisplayTest(WorkerDisplayBase):
    def test_every_worker_on_a_title_is_listed(self):
        self.add("gaming", "Archer", "e1.mkv", progress=12.0)
        self.add("desktop 2", "Archer", "e2.mkv", progress=68.0)
        state = self.merged("Archer")
        rows = {w["worker"]: w for w in state["queue"][0]["workers"]}
        self.assertEqual(set(rows), {"gaming", "desktop 2"})
        self.assertEqual(rows["gaming"]["filename"], "e1.mkv")
        self.assertEqual(rows["desktop 2"]["progress"], 68.0)
        self.assertEqual({r["worker"] for r in state["remote"]}, {"gaming", "desktop 2"})
        self.assertEqual(state["remote"][0]["title"], "Archer")

    def test_claim_on_unqueued_title_still_counts_as_remote(self):
        self.add("gaming", "Archer", "e1.mkv")
        state = self.merged()
        self.assertEqual(len(state["remote"]), 1)

    def test_preview_row_does_not_show_workers(self):
        self.add("gaming", "Archer", "e1.mkv")
        state = {"queue": [{"path": "/media/TV/Archer", "dry_run": True}]}
        self.app._merge_worker_info(state)
        self.assertEqual(state["queue"][0]["workers"], [])

    def test_expired_lease_is_not_encoding(self):
        self.add("gaming", "Archer", "e1.mkv", progress=40.0, lease_left=-1)
        state = self.merged("Archer")
        self.assertEqual(state["remote"], [])
        self.assertEqual(state["queue"][0]["workers"], [])
        st = self.app.worker_status()
        self.assertEqual(st["claimed"], 0)
        self.assertEqual(st["workers"][0]["state"], "lost")
        self.assertEqual(st["workers"][0]["jobs"], [])
        self.assertTrue(st["recent"][0]["stale"])

    def test_worker_counts_and_current_file(self):
        self.app.workers_seen["gaming"] = {"caps": ["nvenc"], "last_seen": self.now - 3}
        self.add("gaming", "Archer", "e1.mkv", progress=50.0)
        self.add("gaming", "Archer", "e2.mkv", status="done")
        self.add("gaming", "Archer", "e3.mkv", status="done")
        self.add("gaming", "Archer", "e4.mkv", status="failed", error="boom")
        self.add("desktop 2", "Archer", "e5.mkv", status="done")
        self.queue("Archer")
        workers = {w["name"]: w for w in self.app.worker_status()["workers"]}
        g = workers["gaming"]
        self.assertEqual((g["state"], g["done"], g["failed"], g["failing"]),
                         ("encoding", 2, 1, 1))
        self.assertEqual(g["caps"], ["nvenc"])
        self.assertEqual(g["jobs"], [{"filename": "e1.mkv", "title": "Archer",
                                      "progress": 50.0}])
        self.assertEqual((workers["desktop 2"]["done"], workers["desktop 2"]["failing"]),
                         (1, 0))

    def test_busy_worker_survives_dashboard_restart(self):
        # workers_seen starts empty; only the job file knows this worker.
        self.add("gaming", "Archer", "e1.mkv", lease_left=dashboard.WORKER_LEASE_SECS - 20)
        w = self.app.worker_status()["workers"][0]
        self.assertEqual((w["name"], w["state"]), ("gaming", "encoding"))
        self.assertEqual(w["caps"], ["cuda", "nvenc"])
        self.assertLess(abs(w["last_seen_secs_ago"] - 20), 3)

    def test_idle_and_offline(self):
        self.app.workers_seen["on"] = {"caps": [], "last_seen": self.now - 5}
        self.app.workers_seen["off"] = {"caps": [], "last_seen": self.now - 500}
        self.app._worker_idle("on", "paused")
        self.app._worker_idle("off", "paused")
        workers = {w["name"]: w for w in self.app.worker_status()["workers"]}
        self.assertEqual((workers["on"]["state"], workers["on"]["idle_reason"]),
                         ("idle", "paused"))
        self.assertEqual((workers["off"]["state"], workers["off"]["idle_reason"]),
                         ("offline", ""))

    def test_failure_outcomes(self):
        self.queue("Q")
        # retried fine later
        self.add("w", "Q", "fixed.mkv", status="failed", error="replace_original failed")
        self.add("w", "Q", "fixed.mkv", status="done")
        # being retried right now
        self.add("w", "Q", "retry.mkv", status="failed")
        self.add("w", "Q", "retry.mkv")
        # one failure, still queued
        self.add("w", "Q", "wait.mkv", status="failed", error="ffmpeg died")
        # three failures: parked
        for _ in range(3):
            self.add("w", "Q", "park.mkv", status="failed")
        # title no longer queued
        self.add("w", "Gone", "gone.mkv", status="failed")
        st = self.app.worker_status()
        by_file = {f["filename"]: f["outcome"] for f in st["failures"]}
        self.assertEqual(by_file, {"fixed.mkv": "fixed", "retry.mkv": "retrying",
                                   "wait.mkv": "waiting", "park.mkv": "parked",
                                   "gone.mkv": "not_queued"})
        self.assertEqual(st["failed"], 7)
        self.assertEqual(st["failing"], 4)  # wait + 3x park
        # what's still a problem sorts first
        self.assertEqual({f["outcome"] for f in st["failures"][:4]}, {"waiting", "parked"})
        reason = [f for f in st["failures"] if f["filename"] == "wait.mkv"][0]["error"]
        self.assertEqual(reason, "ffmpeg died")

    def test_old_failures_outlast_recent_cutoff(self):
        self.queue("Q")
        self.add("w", "Q", "old.mkv", status="failed", age=9000, error="boom")
        for i in range(20):
            self.add("w", "Q", f"new{i}.mkv", status="done", age=i)
        st = self.app.worker_status()
        self.assertNotIn("old.mkv", [j["filename"] for j in st["recent"]])
        self.assertEqual([f["filename"] for f in st["failures"]], ["old.mkv"])

    def test_open_failure_is_not_crowded_out_of_the_list(self):
        self.queue("Q")
        self.add("w", "Q", "open.mkv", status="failed", age=9000)
        for i in range(60):
            self.add("w", "Gone", f"g{i}.mkv", status="failed", age=i)
        st = self.app.worker_status()
        self.assertEqual(len(st["failures"]), 50)
        self.assertEqual(st["failures"][0]["filename"], "open.mkv")

    def test_status_keeps_paths_and_tokens_private(self):
        # /api/worker/status needs no login.
        self.add("w", "Archer", "e1.mkv", progress=1.0)
        self.add("w", "Archer", "e2.mkv", status="failed")
        body = json.dumps(self.app.worker_status())
        self.assertNotIn("/media/TV", body)
        self.assertNotIn("secret-token", body)


RENDER_HARNESS = r"""
const fs = require("fs"), vm = require("vm");
const { html, state, status } = JSON.parse(fs.readFileSync(0, "utf8"));
const script = html.match(/<script>([\s\S]*)<\/script>/)[1];
// A stand-in for any DOM element: remembers what's set, anything else is
// another stand-in that can also be called.
function el() {
  const props = { innerHTML: "", textContent: "", value: "", checked: false, hidden: false };
  return new Proxy(function () {}, {
    get: (_t, k) => {
      if (k === Symbol.toPrimitive) return () => "";
      if (!(k in props)) props[k] = el();
      return props[k];
    },
    set: (_t, k, v) => { props[k] = v; return true; },
    apply: () => undefined,
  });
}
const els = {};
const document = el();
document.hidden = false;
document.querySelector = s => els[s] || (els[s] = el());
document.querySelectorAll = () => [];
const respond = body => ({ status: 200, ok: true, json: async () => body });
const ctx = vm.createContext({
  document, console, Date, Math, JSON, Intl, Set, Map, Object, String, Promise,
  localStorage: { getItem: () => null, setItem() {} },
  location: {}, confirm: () => true,
  setTimeout: () => 0, clearTimeout() {}, setInterval: () => 0,
  fetch: async path => respond(path === "/api/state" ? state : status),
});
vm.runInContext(script, ctx);
setTimeout(() => {
  const out = {};
  for (const id of ["#nowCard", "#queueList", "#workerList", "#workerFails", "#workerCount", "#banners"])
    out[id] = els[id] ? els[id].innerHTML : "";
  process.stdout.write(JSON.stringify(out));
}, 50);
"""


@unittest.skipUnless(shutil.which("node"), "needs node")
class DashboardRenderTest(WorkerDisplayBase):
    """Runs the page's own script against real API output."""

    def render(self, *titles, current=None):
        jobs = self.merged(*titles)
        jobs.update(current=current, history=[], paused=False)
        for q in jobs["queue"]:
            q.update(id=q["name"], library="/media/TV")
        state = {
            "config": {"libraries": ["/media/TV"], "library_profiles": ["tv"],
                       "profiles": {"tv": {"height": 720}, "movies": {"height": 720}},
                       "encoder": "nvenc", "encode_hours": ""},
            "stats": {"library_bytes": 1, "files": 1, "to_encode_files": 1,
                      "to_encode_bytes": 1, "est_savings_bytes": 1, "size_ratio": 0.6,
                      "saved_mb": 0},
            "scan": {"running": False, "last": None}, "auth_required": False,
            "external": [], "eta": {"per_job": {}}, "jobs": jobs,
            "titles": [{"path": "/media/TV/Other", "name": "Other", "library": "/media/TV",
                        "above": 2, "at": 0, "files": 2, "size": 5, "above_size": 5,
                        "status": "ready"}],
        }
        payload = {"html": (ROOT / "web" / "index.html").read_text(encoding="utf-8"),
                   "state": state, "status": self.app.worker_status()}
        r = subprocess.run(["node", "-e", RENDER_HARNESS], input=json.dumps(payload),
                           capture_output=True, text=True, timeout=30)
        self.assertEqual(r.returncode, 0, r.stderr)
        return json.loads(r.stdout)

    def test_status_card_shows_remote_encodes(self):
        self.add("gaming", "Archer", "e1.mkv", progress=12.0)
        self.add("desktop 2", "Archer", "e2.mkv", progress=68.0)
        self.queue("Archer")
        now = self.render("Archer")["#nowCard"]
        self.assertNotIn("Nothing encoding", now)
        self.assertNotIn("files to shrink", now)
        self.assertIn("2 workers", now)
        for text in ("gaming", "desktop 2", "e1.mkv", "e2.mkv", "12%", "68%"):
            self.assertIn(text, now)

    def test_status_card_idle_without_claims(self):
        self.assertIn("Nothing encoding", self.render()["#nowCard"])
        self.add("gaming", "Archer", "e1.mkv", lease_left=-1)  # dead, not encoding
        self.assertIn("Nothing encoding", self.render()["#nowCard"])

    def test_queue_row_shows_every_worker(self):
        self.add("gaming", "Archer", "e1.mkv", progress=12.0)
        self.add("desktop 2", "Archer", "e2.mkv", progress=68.0)
        self.queue("Archer")
        row = self.render("Archer")["#queueList"]
        for text in ("gaming 12%", "desktop 2 68%", "e1.mkv", "e2.mkv"):
            self.assertIn(text, row)

    def test_workers_section_and_failures(self):
        self.queue("Q")
        self.add("gaming", "Q", "e1.mkv", progress=50.0)
        self.add("gaming", "Q", "bad.mkv", status="failed", error="ffmpeg died")
        self.add("gaming", "Q", "ok.mkv", status="failed", error="replace_original failed")
        self.add("gaming", "Q", "ok.mkv", status="done")
        out = self.render("Q")
        self.assertIn("gaming", out["#workerList"])
        self.assertIn("nvenc", out["#workerList"])
        self.assertIn("e1.mkv", out["#workerList"])
        self.assertIn("1 done", out["#workerList"])
        self.assertIn("2 failed", out["#workerList"])
        self.assertIn("Still failing", out["#workerFails"])
        self.assertIn("ffmpeg died", out["#workerFails"])
        self.assertIn("Retried OK", out["#workerFails"])
        self.assertIn("1 still failing", out["#workerCount"])

    def test_banner_only_when_something_is_still_failing(self):
        self.queue("Q")
        self.add("gaming", "Q", "ok.mkv", status="failed", error="x")
        self.add("gaming", "Q", "ok.mkv", status="done")
        self.assertNotIn("still failing", self.render("Q")["#banners"])
        self.add("gaming", "Q", "bad.mkv", status="failed", error="x")
        self.assertIn("still failing", self.render("Q")["#banners"])

    def test_worker_supplied_text_is_escaped(self):
        # Worker names come from an unauthenticated query parameter.
        evil = "<img src=x onerror=alert(1)>"
        self.queue("Q")
        self.add(evil, "Q", evil, progress=5.0)
        self.add(evil, "Q", "bad.mkv", status="failed", error=evil)
        out = self.render("Q")
        for key in ("#nowCard", "#queueList", "#workerList", "#workerFails"):
            self.assertNotIn("<img", out[key], key)
            self.assertIn("&lt;img", out[key], key)


if __name__ == "__main__":
    unittest.main(verbosity=2)
