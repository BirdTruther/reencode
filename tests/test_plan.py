#!/usr/bin/env python3
"""Checks that the dashboard (Python) and reencode.sh (bash) agree on what to do
with every file, across profiles, overrides and multi-version folders.

    python3 tests/test_plan.py        # needs ffmpeg/ffprobe
"""

import os
import subprocess
import sys
import tempfile
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


if __name__ == "__main__":
    unittest.main(verbosity=2)
