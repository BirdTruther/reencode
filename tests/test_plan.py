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
        cls.keep4k_overrides = dashboard.read_4k_overrides(cls.cfg["overrides_4k_file"])

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
        ts = dashboard.title_settings(self.cfg, self.overrides, str(self.base / lib), str(title_dir),
                                      self.keep4k_overrides)
        plan = dashboard.plan_files(files, ts["height"], ts["keep_4k"], ts["skip"], ts["profile"])
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


class PerTitle4KTest(unittest.TestCase):
    """Per-title 4K settings beat the profile, in both planners.

    - Solo 4K with a profile that says "always keep" + per-title "shrink"
      must encode (previously: keep4k, 0 files to do, title vanished).
    - A pair with a profile that says "shrink 4K too" + per-title "keep"
      must keep the 4K copy and still shrink the 1080p copy.
    - Differently-named copies in a movie folder (2160p WEB-DL vs
      1080p BluRay) count as versions of each other, so only the
      1080p copy is shrunk and the two never collide on one output name.
    """

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        base = Path(cls.tmp.name)
        make_video(base / "Movies" / "Solo (2022)" / "Solo (2022) 2160p.mkv", 2160)
        make_video(base / "Movies" / "Pair (2021)" / "Pair (2021) 2160p.mkv", 2160)
        make_video(base / "Movies" / "Pair (2021)" / "Pair (2021) 1080p.mkv", 1080)
        make_video(base / "Movies" / "Suffix (2020)" / "Suffix (2020) 2160p WEB-DL.mkv", 2160)
        make_video(base / "Movies" / "Suffix (2020)" / "Suffix (2020) 1080p BluRay.mkv", 1080)
        cls.base = base
        (base / "title_4k.tsv").write_text(
            f"shrink\t{base}/Movies/Solo (2022)\nkeep\t{base}/Movies/Pair (2021)\n")
        cls.confs = {}
        for name, keep, height in (("keep_yes", "yes", 1080), ("keep_no", "no", 720),
                                   ("keep_other", "if-other-version", 720)):
            conf = base / f"{name}.conf"
            conf.write_text(f"""LIBRARIES=("{base}/Movies")
LIBRARY_PROFILES=("movies")
LOG_DIR="{base}/logs"
TEMP_DIR="{base}/tmp"
MOVIES_HEIGHT={height}
MOVIES_QUALITY=28
MOVIES_KEEP_4K="{keep}"
ENCODER="software"
""")
            cls.confs[name] = conf

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def _cfg(self, name):
        os.environ["REENCODE_CONFIG"] = str(self.confs[name])
        cfg = dashboard.load_config()
        return cfg, dashboard.read_overrides(cfg["overrides_file"]), \
            dashboard.read_4k_overrides(cfg["overrides_4k_file"])

    def _bash_plan(self, title_dir, conf):
        env = dict(os.environ, REENCODE_CONFIG=str(conf))
        out = subprocess.run(["bash", str(ROOT / "reencode.sh"), "--plan", "--dir", str(title_dir)],
                             capture_output=True, text=True, check=True, env=env).stdout
        return {line.split("\t")[3]: line.split("\t")[0] for line in out.splitlines() if line.count("\t") == 3}

    def _python_plan(self, title_dir, cfg, overrides, keep4k):
        files = []
        for p in sorted(title_dir.rglob("*")):
            if p.suffix in (".mkv", ".mp4"):
                info = dashboard.ffprobe(str(p))
                files.append({"name": p.name, "path": str(p), "height": info["height"], "cls": info["cls"]})
        ts = dashboard.title_settings(cfg, overrides, str(self.base / "Movies"), str(title_dir), keep4k)
        plan = dashboard.plan_files(files, ts["height"], ts["keep_4k"], ts["skip"], ts["profile"])
        return {f["path"]: a for f, (a, _) in zip(files, plan)}

    def _check(self, name, title, expected):
        cfg, overrides, keep4k = self._cfg(name)
        d = self.base / "Movies" / title
        want = {str(d / n): a for n, a in expected.items()}
        self.assertEqual(self._bash_plan(d, self.confs[name]), want)
        self.assertEqual(self._python_plan(d, cfg, overrides, keep4k), want)

    def test_solo_4k_shrink_beats_always_keep(self):
        self._check("keep_yes", "Solo (2022)", {"Solo (2022) 2160p.mkv": "encode"})

    def test_pair_keep_beats_shrink_4k(self):
        self._check("keep_no", "Pair (2021)", {
            "Pair (2021) 2160p.mkv": "keep4k", "Pair (2021) 1080p.mkv": "encode"})

    def test_suffix_copies_are_versions_of_each_other(self):
        self._check("keep_other", "Suffix (2020)", {
            "Suffix (2020) 2160p WEB-DL.mkv": "keep4k",
            "Suffix (2020) 1080p BluRay.mkv": "encode"})


class Keep4KCopyTest(unittest.TestCase):
    """Per-title "copy" 4K mode: keep the 4K original and make a smaller
    companion copy next to it, in both planners and end to end."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        base = Path(cls.tmp.name)
        make_video(base / "Movies" / "CopySolo (2024)" / "CopySolo (2024) 2160p.mkv", 2160)
        make_video(base / "Movies" / "CopyPair (2023)" / "CopyPair (2023) 2160p.mkv", 2160)
        make_video(base / "Movies" / "CopyPair (2023)" / "CopyPair (2023) 1080p.mkv", 1080)
        make_video(base / "Movies" / "CopyE2E (2025)" / "CopyE2E (2025) 2160p.mkv", 2160)
        (base / "Movies" / "CopyE2E (2025)" / "CopyE2E (2025) 2160p.srt").write_text(
            "1\n00:00:00,000 --> 00:00:01,000\ntest\n")
        cls.base = base
        (base / "title_4k.tsv").write_text(
            f"copy\t{base}/Movies/CopySolo (2024)\n"
            f"copy\t{base}/Movies/CopyPair (2023)\n"
            f"copy\t{base}/Movies/CopyE2E (2025)\n")
        cls.conf = base / "reencode.conf"
        cls.conf.write_text(f"""LIBRARIES=("{base}/Movies")
LIBRARY_PROFILES=("movies")
LOG_DIR="{base}/logs"
TEMP_DIR="{base}/tmp"
MOVIES_HEIGHT=720
MOVIES_QUALITY=28
MOVIES_KEEP_4K="if-other-version"
ENCODER="software"
""")

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def _cfg(self):
        os.environ["REENCODE_CONFIG"] = str(self.conf)
        cfg = dashboard.load_config()
        return cfg, dashboard.read_overrides(cfg["overrides_file"]), \
            dashboard.read_4k_overrides(cfg["overrides_4k_file"])

    def _bash_plan(self, title_dir):
        env = dict(os.environ, REENCODE_CONFIG=str(self.conf))
        out = subprocess.run(["bash", str(ROOT / "reencode.sh"), "--plan", "--dir", str(title_dir)],
                             capture_output=True, text=True, check=True, env=env).stdout
        return {line.split("\t")[3]: line.split("\t")[0] for line in out.splitlines() if line.count("\t") == 3}

    def _python_plan(self, title_dir):
        cfg, overrides, keep4k = self._cfg()
        files = []
        for p in sorted(title_dir.rglob("*")):
            if p.suffix in (".mkv", ".mp4"):
                info = dashboard.ffprobe(str(p))
                files.append({"name": p.name, "path": str(p), "height": info["height"], "cls": info["cls"]})
        ts = dashboard.title_settings(cfg, overrides, str(self.base / "Movies"), str(title_dir), keep4k)
        plan = dashboard.plan_files(files, ts["height"], ts["keep_4k"], ts["skip"], ts["profile"])
        return {f["path"]: a for f, (a, _) in zip(files, plan)}

    def _check(self, title, expected):
        d = self.base / "Movies" / title
        want = {str(d / n): a for n, a in expected.items()}
        self.assertEqual(self._bash_plan(d), want)
        self.assertEqual(self._python_plan(d), want)

    def test_solo_4k_plans_makecopy(self):
        self._check("CopySolo (2024)", {"CopySolo (2024) 2160p.mkv": "makecopy"})

    def test_pair_with_smaller_copy_keeps_4k(self):
        self._check("CopyPair (2023)", {
            "CopyPair (2023) 2160p.mkv": "keep4k",
            "CopyPair (2023) 1080p.mkv": "encode"})

    def test_end_to_end_copy_keeps_original(self):
        """Full run: the 4K original survives, a 720p copy lands next to it."""
        d = self.base / "Movies" / "CopyE2E (2025)"
        src = d / "CopyE2E (2025) 2160p.mkv"
        env = dict(os.environ, REENCODE_CONFIG=str(self.conf))
        subprocess.run(["bash", str(ROOT / "reencode.sh"), "--dir", str(d)],
                       env=env, capture_output=True, check=True)
        out = d / "CopyE2E (2025) 720p.mkv"
        self.assertTrue(src.exists(), "original 4K must be kept")
        self.assertTrue(out.exists(), "720p copy must be created")
        info = dashboard.ffprobe(str(out))
        self.assertEqual((info["width"], info["height"], info["codec"]), (1280, 720, "hevc"))
        self.assertEqual((d / "CopyE2E (2025) 720p.srt").read_text(),
                         (d / "CopyE2E (2025) 2160p.srt").read_text())


class NoSavingsTest(unittest.TestCase):
    def test_keyed_by_settings_and_old_plain_entries_ignored(self):
        with tempfile.TemporaryDirectory() as d:
            script = f'''
                source "{ROOT / "common.sh"}"
                LOG_DIR="{d}/logs"; ENCODER="vaapi"; QUALITY=32; TARGET_HEIGHT=720
                record_no_savings "/media/Movies/Film (2020) 1080p.mkv"
                if is_no_savings "/media/Movies/Film (2020) 1080p.mkv"; then echo SAME; else echo NOT_SAME; fi
                QUALITY=28
                if is_no_savings "/media/Movies/Film (2020) 1080p.mkv"; then echo SAME_AFTER_CHANGE; else echo RETRY_AFTER_CHANGE; fi
                echo "/media/Movies/Film (2020) 1080p.mkv" >> "$(NO_SAVINGS_LIST)"
                if is_no_savings "/media/Movies/Film (2020) 1080p.mkv"; then echo PLAIN_BLOCKS; else echo PLAIN_IGNORED; fi
                clear_no_savings
                test -f "$(NO_SAVINGS_LIST)" && echo STILL_THERE || echo CLEARED
            '''
            out = subprocess.run(["bash", "-c", script], capture_output=True, text=True, check=True).stdout
            self.assertIn("SAME\n", out)
            self.assertIn("RETRY_AFTER_CHANGE\n", out)
            self.assertIn("PLAIN_IGNORED\n", out)
            self.assertIn("CLEARED\n", out)


class VaapiFiltersTest(unittest.TestCase):
    def test_10bit_and_8bit_both_use_nv12_and_main_profile(self):
        """Regression: 10-bit sources must NOT select p010/Main10.

        p010 made hevc_vaapi request VAProfileHEVCMain10, which GPUs like
        Polaris/RX 580 can decode but not encode (no EncSlice), so every
        10-bit file failed at encoder open. Output is always 8-bit Main.
        """
        with tempfile.TemporaryDirectory() as d:
            base = Path(d)
            run_ffmpeg("-f", "lavfi", "-i", "color=size=1280x720:duration=0.2",
                       "-c:v", "libx265", "-pix_fmt", "yuv420p10le", "-preset", "ultrafast",
                       str(base / "10bit.mkv"))
            run_ffmpeg("-f", "lavfi", "-i", "color=size=1280x720:duration=0.2",
                       "-c:v", "libx264", "-preset", "ultrafast", str(base / "8bit.mkv"))
            script = f'''
                source "{ROOT / "common.sh"}"
                ENCODER="vaapi"
                set_filters 1280 720 "{base / "10bit.mkv"}" 0
                echo "10bit: $ENC_VF $GPU_VF"
                set_filters 1280 720 "{base / "8bit.mkv"}" 0
                echo "8bit: $ENC_VF $GPU_VF"
            '''
            out = subprocess.run(["bash", "-c", script], capture_output=True, text=True, check=True).stdout
            self.assertIn("10bit: scale=1280:720,format=nv12,hwupload scale_vaapi=w=1280:h=720:format=nv12", out)
            self.assertIn("8bit: scale=1280:720,format=nv12,hwupload scale_vaapi=w=1280:h=720:format=nv12", out)
            self.assertNotIn("p010", out)

    def test_encoder_failure_is_not_a_decode_failure(self):
        """An encoder-open failure must not be retried as a decode failure."""
        with tempfile.TemporaryDirectory() as d:
            log = Path(d) / "fail.log"
            log.write_text("[hevc_vaapi] No usable encoding entrypoint found for profile "
                           "VAProfileHEVCMain10 (18).\n"
                           "[vost#0:0/hevc_vaapi] Error while opening encoder\n")
            ok = Path(d) / "ok.log"
            ok.write_text("Invalid data found when processing input\n")
            script = f'''
                source "{ROOT / "common.sh"}"
                # reencode.sh only runs main when executed directly, so
                # sourcing it here just loads its functions.
                source "{ROOT / "reencode.sh"}"
                is_encoder_failure "{log}" && echo ENCODER_FAIL || echo NOT_ENCODER
                is_encoder_failure "{ok}" && echo ENCODER_FAIL || echo DECODE_FAIL
                echo "sig: $(failure_signature "{log}")"
            '''
            out = subprocess.run(["bash", "-c", script], capture_output=True, text=True, check=True).stdout
            self.assertIn("ENCODER_FAIL\n", out)
            self.assertIn("DECODE_FAIL\n", out)
            self.assertIn("sig: vaapi-no-encoding-entrypoint", out)


class VerifyOnlyTest(unittest.TestCase):
    def test_widescreen_720p_class_passes(self):
        with tempfile.TemporaryDirectory() as d:
            base = Path(d)
            (base / "tmp").mkdir()
            make_video(base / "tmp" / "Wide 720p.mkv", "1280x534", duration=0.5)
            conf = base / "reencode.conf"
            conf.write_text(f"""LIBRARIES=("{base}/Movies")
LIBRARY_PROFILES=("movies")
LOG_DIR="{base}/logs"
TEMP_DIR="{base}/tmp"
TV_HEIGHT=720
TV_QUALITY=30
TV_KEEP_4K="no"
MOVIES_HEIGHT=1080
MOVIES_QUALITY=28
MOVIES_KEEP_4K="if-other-version"
ENCODER="software"
""")
            env = dict(os.environ, REENCODE_CONFIG=str(conf))
            out = subprocess.run(["bash", str(ROOT / "reencode.sh"), "--verify-only"],
                                 capture_output=True, text=True, env=env).stdout
            self.assertIn("Verified: 1/1", out)


class DashboardJobsTest(unittest.TestCase):
    def test_job_log_name_is_unique_and_allow_failures_is_public(self):
        a = dashboard.Job({"path": "/media/Movies/Film", "name": "Film", "library": "/media/Movies"},
                          False, True)
        b = dashboard.Job({"path": "/media/TV/Film", "name": "Film", "library": "/media/TV"}, False)
        self.assertNotEqual(a.log_name(), b.log_name())
        self.assertTrue(a.log_name().startswith(a.id + "_"))
        self.assertTrue(a.public()["allow_failures"])
        self.assertFalse(b.public()["allow_failures"])

    def test_queue_save_load_round_trip(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "queue.json"
            a = dashboard.Job({"path": "/media/Movies/Film", "name": "Film", "library": "/media/Movies"},
                              False, True)
            dashboard.save_queue([a], path)
            loaded = dashboard.load_queue(path)
            self.assertEqual(len(loaded), 1)
            self.assertEqual(loaded[0]["path"], "/media/Movies/Film")
            self.assertTrue(loaded[0]["allow_failures"])
            self.assertEqual(loaded[0]["created"], a.created)
            self.assertEqual(dashboard.load_queue(Path(d) / "missing.json"), [])
            self.assertEqual(dashboard.load_queue(path.with_suffix(".bad")), [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
