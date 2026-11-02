#!/usr/bin/env python3
"""Remote worker (v0): --worker-cmd / --worker-place in reencode.sh, and the
dashboard's claim -> encode -> finish loop, end to end.

    python3 tests/test_worker.py    # needs ffmpeg/ffprobe
"""

import json
import os
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


class WorkerProtocolTest(unittest.TestCase):
    """Dashboard claim -> run the server's own argv -> finish, end to end."""

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
        return app

    def test_claim_run_finish_end_to_end(self):
        with tempfile.TemporaryDirectory() as t:
            base = Path(t)
            d = base / "Movies" / "Film"
            src = d / "Film 1080p.mkv"
            make_video(src, 1080)
            conf = write_conf(base)
            app = self._make_app(base, conf)

            job = app.worker_next("w1", ["software"])
            self.assertIsNotNone(job)
            self.assertIn("libx265", job["argv"])
            # claimed: nothing left for anyone else
            self.assertIsNone(app.worker_next("w2", ["software"]))

            # the worker runs the server's command verbatim
            out = base / "out.mkv"
            argv = [a.replace("__INPUT__", str(src)).replace("__OUTPUT__", str(out))
                    for a in job["argv"]]
            subprocess.run(argv, capture_output=True, check=True)

            res = app.worker_finish(job["job_id"], str(out))
            self.assertTrue(res["ok"], res)
            self.assertFalse(src.exists())
            done = d / "Film 720p.mkv"
            self.assertTrue(done.exists())
            info = dashboard.ffprobe(str(done))
            self.assertEqual(info["codec"], "hevc")
            # and it's not offered again
            self.assertIsNone(app.worker_next("w1", ["software"]))

    def test_lease_expiry_releases_job(self):
        with tempfile.TemporaryDirectory() as t:
            base = Path(t)
            d = base / "Movies" / "Film"
            make_video(d / "Film 1080p.mkv", 1080)
            conf = write_conf(base)
            app = self._make_app(base, conf)

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

    def test_av1_jobs_need_av1d_cap(self):
        with tempfile.TemporaryDirectory() as t:
            base = Path(t)
            d = base / "Movies" / "AV1Film"
            src = d / "AV1Film 1080p.mkv"
            src.parent.mkdir(parents=True, exist_ok=True)
            run_ffmpeg("-f", "lavfi", "-i", "color=size=1920x1080:duration=0.2",
                       "-c:v", "libsvtav1", "-preset", "10", str(src))
            self.assertEqual(dashboard.ffprobe(str(src))["codec"], "av1")
            conf = write_conf(base)
            app = self._make_app(base, conf)
            # a worker that can't decode AV1 must not get the job...
            self.assertIsNone(app.worker_next("cpuonly", ["software"]))
            # ...but one that can, does
            job = app.worker_next("gpu", ["nvenc", "cuda", "av1d"])
            self.assertIsNotNone(job)
            self.assertEqual(job["filename"], src.name)


if __name__ == "__main__":
    unittest.main(verbosity=2)
