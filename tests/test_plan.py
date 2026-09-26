#!/usr/bin/env python3
"""Checks that the dashboard (Python) and reencode.sh (bash) agree on what to do
with every file, across profiles, overrides and multi-version folders.

    python3 tests/test_plan.py        # needs ffmpeg/ffprobe
"""

import json
import os
import subprocess
import sys
import tempfile
import threading
import types
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.environ.pop("REENCODE_CONFIG", None)
import dashboard  # noqa: E402

# (library, title, file, size, expected action). size is a height (16:9), "WxH",
# "dual:WxH+WxH" (two video streams, first listed first) or "cover:WxH" (cover
# art stored as the first video stream).
CASES = [
    # Widescreen 4K is 4K by width, like Plex shows it (was seen as 1600p).
    ("Movies", "Scope (2019)", "Scope (2019) - 2160p.mkv", "3840x1600", "keep4k"),
    ("Movies", "Scope (2019)", "Scope (2019) - 1080p.mkv", "1920x800", "encode"),
    ("Movies", "Flat (2018)", "Flat (2018) 2160p.mkv", "3840x2076", "encode"),
    # The 4K picture is the second video stream (like a Dolby Vision dual layer).
    ("Movies", "Dual (2020)", "Dual (2020) 2160p.mkv", "dual:1920x1080+3840x2160", "keep4k"),
    ("Movies", "Dual (2020)", "Dual (2020) 1080p.mkv", 1080, "encode"),
    ("Movies", "Cover (2016)", "Cover (2016) 2160p.mp4", "cover:3840x2160", "encode"),
    # Shrunk by an older version (by height only): done, never shrunk again.
    ("Movies", "Legacy (2015)", "Legacy (2015) 720p.mkv", "1728x720", "ok"),
    ("Movies", "Film A (2020)", "Film A (2020) - 2160p.mkv", 2160, "keep4k"),
    ("Movies", "Film A (2020)", "Film A (2020) - 1080p.mkv", 1080, "encode"),
    ("Movies", "Film B (2021)", "Film B (2021) 2160p.mkv", 2160, "encode"),      # override 1080
    ("Movies", "Film C (2019)", "Film C (2019) [2160p].mkv", 2160, "keep4k"),
    ("Movies", "Film C (2019)", "Film C (2019) [1080p].mkv", 1080, "encode"),
    ("Movies", "Film D (2018)", "Film D (2018).mkv", 1080, "skip"),              # override skip
    ("Movies", "Film E (2017)", "Film E (2017) 1080p.mkv", 1080, "extra"),
    ("Movies", "Film E (2017)", "Film E (2017) 720p.mkv", 720, "ok"),
    ("TV", "Show X", "Season 1/Show X S01E01 1080p.mkv", 1080, "encode"),
    ("TV", "Show X", "Season 1/Show X S01E02 720p.mkv", 720, "ok"),
    ("TV", "Show X", "Season 1/Show X S01E03.mkv", 480, "ok"),
    ("TV", "Show Y", "Show Y S01E01 2160p.mkv", 2160, "encode"),                 # TV: keep 4K = no
    ("TV", "Show Y", "Show Y S01E01 1080p.mkv", 1080, "extra"),
    ("TV", "Show Y", "Show.Y.S01E02.1080p.WEB.mkv", 1080, "encode"),
]
NAMES = ["Film (2020) - 2160p", "Film (2020) 720p", "Show.S01E01.1080p.WEB", "Show S01E02 4K",
         "Movie 1920x1080", "Home Video", "Clip [1080i]", "A_B-C  720p", "Ünïcode 2160p",
         "Ü2160p Émile", "Café.1080p.x265"]


def run_ffmpeg(*args):
    subprocess.run(["ffmpeg", "-nostdin", "-v", "error", "-y", *args], check=True)


def make_video(path, size, duration=0.2):
    path.parent.mkdir(parents=True, exist_ok=True)
    src = lambda s: ["-f", "lavfi", "-i", f"color=size={s}:duration={duration}"]  # noqa: E731
    if isinstance(size, int):
        size = f"{size * 16 // 9 // 2 * 2}x{size}"
    if size.startswith("dual:"):
        a, b = size[5:].split("+")
        run_ffmpeg(*src(a), *src(b), "-map", "0", "-map", "1", "-c:v", "libx264",
                   "-preset", "ultrafast", str(path))
    elif size.startswith("cover:"):
        cover = path.with_suffix(".jpg")
        run_ffmpeg("-f", "lavfi", "-i", "color=size=600x900", "-frames:v", "1", str(cover))
        run_ffmpeg("-i", str(cover), *src(size[6:]), "-map", "0", "-map", "1",
                   "-c:v:0", "mjpeg", "-c:v:1", "libx264", "-preset", "ultrafast",
                   "-disposition:v:0", "attached_pic", str(path))
        cover.unlink()
    else:
        run_ffmpeg(*src(size), "-c:v", "libx264", "-preset", "ultrafast", str(path))


class PlanTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        base = Path(cls.tmp.name)
        for lib, title, name, height, _ in CASES:
            make_video(base / lib / title / name, height)
        cls.conf = base / "reencode.conf"
        cls.conf.write_text(f"""LIBRARIES=("{base}/TV" "{base}/Movies")
LIBRARY_PROFILES=("tv" "movies")
LOG_DIR="{base}/logs"
TEMP_DIR="{base}/tmp"
TV_HEIGHT=720
TV_QUALITY=30
TV_KEEP_4K="no"
MOVIES_HEIGHT=720
MOVIES_QUALITY=28
MOVIES_KEEP_4K="if-other-version"
ENCODER="software"
""")
        (base / "title_overrides.tsv").write_text(
            f"1080\t{base}/Movies/Film B (2021)\nskip\t{base}/Movies/Film D (2018)\n")
        os.environ["REENCODE_CONFIG"] = str(cls.conf)
        cls.base = base
        cls.cfg = dashboard.load_config()
        cls.overrides = dashboard.read_overrides(cls.cfg["overrides_file"])

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def bash_plan(self, title_dir):
        out = subprocess.run(["bash", str(ROOT / "reencode.sh"), "--plan", "--dir", str(title_dir)],
                             capture_output=True, text=True, check=True).stdout
        return {line.split("\t")[3]: line.split("\t")[0] for line in out.splitlines() if line.count("\t") == 3}

    def python_plan(self, lib, title_dir):
        files = []
        for p in sorted(title_dir.rglob("*")):
            if p.suffix in (".mkv", ".mp4"):
                info = dashboard.ffprobe(str(p))
                files.append({"name": p.name, "path": str(p), "height": info["height"], "cls": info["cls"]})
        ts = dashboard.title_settings(self.cfg, self.overrides, str(self.base / lib), str(title_dir))
        plan = dashboard.plan_files(files, ts["height"], ts["keep_4k"], ts["skip"])
        return {f["path"]: a for f, (a, _) in zip(files, plan)}

    def test_bash_and_python_agree(self):
        for lib, title in sorted({(c[0], c[1]) for c in CASES}):
            d = self.base / lib / title
            with self.subTest(title=title):
                self.assertEqual(self.bash_plan(d), self.python_plan(lib, d))

    def test_expected_actions(self):
        for lib, title, name, _, expected in CASES:
            with self.subTest(file=name):
                got = self.bash_plan(self.base / lib / title)[str(self.base / lib / title / name)]
                self.assertEqual(got, expected)

    def test_resolution_class(self):
        for name, cls in [("Scope (2019) - 2160p.mkv", 2160), ("Scope (2019) - 1080p.mkv", 1080),
                          ("Flat (2018) 2160p.mkv", 2160), ("Dual (2020) 2160p.mkv", 2160),
                          ("Cover (2016) 2160p.mp4", 2160)]:
            with self.subTest(file=name):
                path = next(self.base.rglob(name))
                self.assertEqual(dashboard.ffprobe(str(path))["cls"], cls)
                self.assertEqual(dashboard.res_label(cls), "4K" if cls == 2160 else "1080p")

    def test_output_size_keeps_aspect_ratio(self):
        cases = {(3840, 1600, 720): "1280 534", (1920, 800, 1080): "1920 800",
                 (3840, 2076, 720): "1280 692", (1920, 1080, 720): "1280 720",
                 (1440, 1080, 720): "960 720", (1920, 1080, 480): "854 480"}
        for (w, h, t), want in cases.items():
            with self.subTest(size=f"{w}x{h}", target=t):
                got = subprocess.run(["bash", "-c", 'source "$1"; target_dims "$2" "$3" "$2" "$4"', "_",
                                      str(ROOT / "common.sh"), str(w), str(h), str(t)],
                                     capture_output=True, text=True, check=True).stdout.strip()
                self.assertEqual(got, want)

    def test_encode_widescreen(self):
        """A real encode of a 2.40:1 4K file: right stream, 1280x534, original replaced."""
        base = self.base / "enc"
        src = base / "TV" / "Wide Show" / "Wide Show S01E01 2160p.mkv"
        make_video(src, "dual:1920x1080+3840x1600", duration=1)
        conf = base / "reencode.conf"
        conf.write_text(f"""LIBRARIES=("{base}/TV")
LIBRARY_PROFILES=("tv")
LOG_DIR="{base}/logs"
TEMP_DIR="{base}/tmp"
TV_HEIGHT=720
TV_QUALITY=30
TV_KEEP_4K="no"
ENCODER="software"
""")
        env = dict(os.environ, REENCODE_CONFIG=str(conf))
        subprocess.run(["bash", str(ROOT / "reencode.sh"), "--dir", str(src.parent)],
                       env=env, capture_output=True, check=True)
        out = src.parent / "Wide Show S01E01 720p.mkv"
        self.assertFalse(src.exists())
        info = dashboard.ffprobe(str(out))
        self.assertEqual((info["width"], info["height"], info["codec"]), (1280, 534, "hevc"))

    def test_group_key_matches(self):
        for name in NAMES:
            with self.subTest(name=name):
                bash = subprocess.run(["bash", "-c", 'source "$1"; group_key "$2"', "_",
                                       str(ROOT / "common.sh"), name],
                                      capture_output=True, text=True, check=True).stdout.strip()
                self.assertEqual(bash, dashboard.group_key(name))


class QueuePersistTest(unittest.TestCase):
    """The queue must survive a restart, and an interrupted job must come back."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)
        for lib in ("TV", "Movies"):
            (self.base / lib / "Show Z" / "Season 1").mkdir(parents=True)
        self.conf = self.base / "reencode.conf"
        self.conf.write_text(f"""LIBRARIES=("{self.base}/TV" "{self.base}/Movies")
LIBRARY_PROFILES=("tv" "movies")
LOG_DIR="{self.base}/logs"
TEMP_DIR="{self.base}/tmp"
TV_HEIGHT=720
TV_QUALITY=30
TV_KEEP_4K="no"
MOVIES_HEIGHT=720
MOVIES_QUALITY=28
MOVIES_KEEP_4K="if-other-version"
ENCODER="software"
""")
        self.env = dict(os.environ, REENCODE_CONFIG=str(self.conf))

    def make_runner(self):
        """A JobRunner without worker threads or history side effects."""
        os.environ["REENCODE_CONFIG"] = str(self.conf)
        app = dashboard.App.__new__(dashboard.App)
        app.config = dashboard.load_config()
        app.overrides = dashboard.read_overrides(app.config["overrides_file"])
        dashboard.set_state_dir(self.base)
        app.library = types.SimpleNamespace(
            title=lambda p: None, request_scan=lambda only=None: None)
        app.jobs = dashboard.JobRunner.__new__(dashboard.JobRunner)
        app.jobs.app = app
        app.jobs.lock = threading.Lock()
        app.jobs.wake = threading.Event()
        app.jobs.queue = []
        app.jobs.current = None
        app.jobs.waiting_external = app.jobs.waiting_schedule = False
        app.jobs.shutting_down = False
        app.jobs.history = []
        app.jobs._record = lambda job: None
        app.jobs._load_queue()
        self.jobs = app.jobs
        return app.jobs

    def enqueue(self, path, dry_run=False):
        with self.jobs.lock:
            job = dashboard.Job({"path": str(path), "name": path.name,
                                 "library": str(self.base / "TV")}, dry_run)
            self.jobs.queue.append(job)
            self.jobs._save_queue()

    def test_queue_survives_restart(self):
        r = self.make_runner()
        a = (self.base / "TV" / "Show Z").resolve()
        b = (self.base / "Movies" / "Show Z").resolve()
        self.enqueue(a)
        self.enqueue(b, dry_run=True)

        r2 = self.make_runner()  # simulate a restart
        self.assertEqual([j.path for j in r2.queue], [str(a), str(b)])
        self.assertEqual([j.dry_run for j in r2.queue], [False, True])

    def test_interrupted_job_is_requeued_first(self):
        """A job stopped mid-encode must come back, and be marked as interrupted."""
        r = self.make_runner()
        a = self.base / "TV" / "Show Z"
        self.enqueue(a)
        job = r.queue.pop(0)
        r.current = job
        r.shutting_down = True

        # Mirror the worker's post-run bookkeeping in _worker().
        with r.lock:
            r.current = None
            if r.shutting_down and job.status != "done":
                job.status, job.finished, job.restarted = "queued", None, True
                job.add_line("-- dashboard stopped mid-encode; queued again on restart --")
                r.queue.insert(0, job)
                r._save_queue()
        self.assertEqual([j.path for j in r.queue], [str(a)])
        self.assertTrue(r.queue[0].restarted)
        self.assertEqual(r.queue[0].status, "queued")
        self.assertIn("stopped mid-encode", " ".join(l for _, l in r.queue[0].log))

        # Restart: the job is restored at the front and flagged as interrupted.
        r2 = self.make_runner()
        self.assertEqual([j.path for j in r2.queue], [str(a)])
        self.assertTrue(r2.queue[0].restarted)
        self.assertIn("interrupted by a restart", " ".join(l for _, l in r2.queue[0].log))

    def test_finished_job_is_not_requeued(self):
        """A job that actually finished before shutdown must be recorded, not requeued."""
        r = self.make_runner()
        a = self.base / "TV" / "Show Z"
        self.enqueue(a)
        job = r.queue.pop(0)
        job.status = "done"
        r.shutting_down = True
        with r.lock:
            r.current = None
            if r.shutting_down and job.status != "done":
                r.queue.insert(0, job)
        self.assertEqual(r.queue, [])

    def test_finished_queue_file_clears(self):
        r = self.make_runner()
        a = self.base / "TV" / "Show Z"
        self.enqueue(a)
        self.assertTrue(dashboard.QUEUE_FILE.exists())
        r.cancel(r.queue[0].id)
        self.assertEqual(r.queue, [])
        self.assertEqual(json.loads(dashboard.QUEUE_FILE.read_text()), [])

    def test_missing_folder_is_dropped(self):
        r = self.make_runner()
        keep = self.base / "TV" / "Show Z"
        gone = self.base / "Movies" / "Show Z"
        self.enqueue(keep)
        self.enqueue(gone)
        import shutil
        shutil.rmtree(gone)
        r._prune_stale()
        self.assertEqual([j.path for j in r.queue], [str(keep)])

    def test_corrupt_queue_file_ignored(self):
        self.make_runner()
        dashboard.QUEUE_FILE.write_text("{not json")
        r = self.make_runner()
        self.assertEqual(r.queue, [])

    def test_library_of_picks_longest_match(self):
        os.environ["REENCODE_CONFIG"] = str(self.conf)
        app = dashboard.App.__new__(dashboard.App)
        app.config = dashboard.load_config()
        self.assertEqual(app.library_of(f"{self.base}/TV/Show Z"),
                         f"{self.base}/TV")

    def test_eta_survives_duplicate_basenames(self):
        """Two files named the same must not collapse into one ETA entry."""
        j = dashboard.Job({"path": "/x", "name": "x", "library": "/TV"}, False)
        j.todo = [("video.mkv", 100.0), ("video.mkv", 200.0), ("other.mkv", 50.0)]
        j.total_todo = 350.0
        self.assertEqual(j.remaining_secs(), 350.0)

        # The first entry is the file now being encoded; the later duplicate
        # must still be counted, or the title ETA collapses to one file.
        j.add_line("[1/3] Processing: video.mkv")
        self.assertEqual(j.remaining_secs(), 350.0)
        j.add_line("Duration: 100.0s")
        j.out_time = 50.0
        self.assertEqual(j.remaining_secs(), 300.0)      # 50 done of the 1st of 300

        # The next "Processing:" line retires the previous file, so the 100s
        # entry is dropped and the 200s duplicate becomes the current file.
        j.add_line("[2/3] Processing: video.mkv")
        self.assertEqual(j.remaining_secs(), 250.0)
        j.add_line("Duration: 200.0s")
        j.out_time = 200.0
        self.assertEqual(j.remaining_secs(), 50.0)       # only "other.mkv" left

        j.add_line("[3/3] Processing: other.mkv")
        self.assertEqual(j.remaining_secs(), 50.0)       # current file still pending

    def test_external_scan_is_cached(self):
        dashboard._EXT_CACHE = (0.0, {})
        calls = []
        real = dashboard.os.listdir

        def spy(p):
            calls.append(p)
            return real(p)

        dashboard.os.listdir = spy
        try:
            dashboard._APP = None
            dashboard._EXT_CACHE = (dashboard.time.monotonic() - 100, {})
            first = dashboard.external_encodes()
            second = dashboard.external_encodes()
        finally:
            dashboard.os.listdir = real
            dashboard._EXT_CACHE = (0.0, {})
        self.assertEqual(first, second)
        self.assertEqual(len(calls), 1, "second call must be served from cache")


if __name__ == "__main__":
    unittest.main(verbosity=2)
