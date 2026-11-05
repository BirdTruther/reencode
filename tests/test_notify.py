#!/usr/bin/env python3
"""REENCODE_NOTIFY_URL: what reencode.sh posts, when, and that a failed post
never affects encoding. Encoding is stubbed, so this doesn't need ffmpeg.

    python3 tests/test_notify.py
"""

import json
import os
import socket
import subprocess
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


class Hub:
    """A local stand-in for the notification hub that records what it gets."""

    def __init__(self, status=200):
        self.posts = []
        hub = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                body = self.rfile.read(int(self.headers["Content-Length"]))
                hub.posts.append({"path": self.path, "type": self.headers["Content-Type"],
                                  "json": json.loads(body)})
                self.send_response(status)
                self.end_headers()

            def log_message(self, *args):
                pass

        self.server = HTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_port}/notify/key?tag=media"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()


def run_bash(script, *args, url=None, path_prefix=None):
    env = {k: v for k, v in os.environ.items() if k != "REENCODE_NOTIFY_URL"}
    if url is not None:
        env["REENCODE_NOTIFY_URL"] = url
    if path_prefix:
        env["PATH"] = f"{path_prefix}:{env['PATH']}"
    prelude = f'source "{ROOT / "common.sh"}"; source "{ROOT / "reencode.sh"}"\n'
    return subprocess.run(["bash", "-c", prelude + script, "_", *args], env=env,
                          capture_output=True, text=True)


def show_script(*results, dry_run=False):
    """process_show over one file per result, with the encoder stubbed out to
    return each rc in turn. A successful encode saves 300MB."""
    files = " ".join(f'"/lib/Show/e{i}.mkv"' for i in range(len(results)))
    return f"""
        TEMP_DIR=$(mktemp -d); DRY_RUN={"true" if dry_run else "false"}; CIRCUIT_MAX=2
        PROFILE_NAME=tv; TARGET_HEIGHT=720; QUALITY=30; KEEP_4K=no
        title_files() {{ TITLE_FILES=({files}); }}
        plan_title() {{ local f; for f in "$@"; do PLAN["$f"]=encode; done; }}
        results=({" ".join(str(r) for r in results)})
        calls="$TEMP_DIR/calls"
        encode_file() {{
            local n=$(wc -l < "$calls" 2>/dev/null || echo 0)
            echo >> "$calls"
            LAST_FAIL_SIG=same-error
            : > "$TEMP_DIR/$(build_newbase "$(basename "${{1%.*}}")").mkv"
            return "${{results[$n]}}"
        }}
        get_file_size_mb() {{ [[ "$1" == "$TEMP_DIR"/* ]] && echo 200 || echo 500; }}
        process_show "/lib/Show"
        echo "rc=$? encoded_calls=$(wc -l < "$calls")"
    """


class SendNotificationTest(unittest.TestCase):
    def setUp(self):
        self.hub = Hub()
        self.addCleanup(self.hub.close)

    def test_posts_json_to_the_url(self):
        out = run_bash('send_notification "$1" "$2" info', 'T "quoted" \\ ü', "line1\nline2",
                       url=self.hub.url)
        self.assertEqual(out.returncode, 0)
        self.assertEqual(len(self.hub.posts), 1)
        post = self.hub.posts[0]
        self.assertEqual(post["path"], "/notify/key?tag=media")
        self.assertEqual(post["type"], "application/json")
        self.assertEqual(post["json"], {"title": 'T "quoted" \\ ü', "body": "line1\nline2",
                                        "type": "info"})

    def test_unset_or_empty_does_nothing_at_all(self):
        """Not even Python runs: a stand-in for it would leave a marker."""
        with tempfile.TemporaryDirectory() as d:
            shim = Path(d) / "python3"
            marker = Path(d) / "ran"
            shim.write_text(f'#!/bin/sh\n: > "{marker}"\n')
            shim.chmod(0o755)
            for url in (None, ""):
                out = run_bash('send_notification a b info; echo "rc=$?"', url=url, path_prefix=d)
                self.assertIn("rc=0", out.stdout)
                self.assertNotIn("WARN", out.stdout)
                self.assertFalse(marker.exists())

    def test_unreachable_hub_only_warns(self):
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            port = s.getsockname()[1]
        out = run_bash('send_notification a b info; echo "rc=$?"', url=f"http://127.0.0.1:{port}/x")
        self.assertIn("rc=0", out.stdout)
        self.assertIn("Notification not sent", out.stdout)

    def test_http_error_only_warns(self):
        bad = Hub(status=500)
        self.addCleanup(bad.close)
        out = run_bash('send_notification a b info; echo "rc=$?"', url=bad.url)
        self.assertIn("rc=0", out.stdout)
        self.assertIn("Notification not sent", out.stdout)

    def test_missing_python_only_warns(self):
        with tempfile.TemporaryDirectory() as d:
            out = run_bash(f'PATH="{d}"; send_notification a b info; echo "rc=$?"', url=self.hub.url)
        self.assertIn("rc=0", out.stdout)

    def test_hung_hub_times_out_after_5_seconds(self):
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            s.listen()  # accepts the connection, never answers
            start = time.monotonic()
            out = run_bash('send_notification a b info; echo "rc=$?"',
                           url=f"http://127.0.0.1:{s.getsockname()[1]}/x")
            took = time.monotonic() - start
        self.assertIn("rc=0", out.stdout)
        self.assertIn("Notification not sent", out.stdout)
        self.assertGreaterEqual(took, 4.5)
        self.assertLess(took, 8)


class ProcessShowTest(unittest.TestCase):
    def setUp(self):
        self.hub = Hub()
        self.addCleanup(self.hub.close)

    def test_clean_title_sends_one_summary_and_no_per_file_messages(self):
        out = run_bash(show_script(0, 0, 0), url=self.hub.url)
        self.assertIn("rc=0 encoded_calls=3", out.stdout)
        self.assertEqual(len(self.hub.posts), 1)
        msg = self.hub.posts[0]["json"]
        self.assertEqual(msg["type"], "success")
        self.assertEqual(msg["title"], "Reencode: Show")
        self.assertIn("Files processed: 3", msg["body"])
        self.assertIn("Space saved: 900MB", msg["body"])
        self.assertIn("Failures: 0", msg["body"])

    def test_some_failures_make_the_summary_a_warning(self):
        out = run_bash(show_script(0, 1, 0), url=self.hub.url)
        self.assertIn("rc=1 encoded_calls=3", out.stdout)
        self.assertEqual(len(self.hub.posts), 1)
        msg = self.hub.posts[0]["json"]
        self.assertEqual(msg["type"], "warning")
        self.assertIn("Files processed: 2", msg["body"])
        self.assertIn("Failures: 1", msg["body"])

    def test_circuit_breaker_sends_failure_then_summary(self):
        out = run_bash(show_script(1, 1, 0, 0), url=self.hub.url)
        self.assertIn("rc=1 encoded_calls=2", out.stdout)
        self.assertEqual([p["json"]["type"] for p in self.hub.posts], ["failure", "warning"])
        trip = self.hub.posts[0]["json"]
        self.assertEqual(trip["title"], "Reencode: circuit breaker tripped")
        self.assertIn("Show: 2 consecutive failures", trip["body"])
        self.assertIn("same-error", trip["body"])

    def test_all_skipped_sends_nothing(self):
        run_bash(show_script(2, 2), url=self.hub.url)
        self.assertEqual(self.hub.posts, [])

    def test_dry_run_sends_nothing(self):
        run_bash(show_script(0, 1, dry_run=True), url=self.hub.url)
        self.assertEqual(self.hub.posts, [])

    def test_dead_hub_does_not_change_the_outcome(self):
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            port = s.getsockname()[1]
        out = run_bash(show_script(0, 0), url=f"http://127.0.0.1:{port}/x")
        self.assertIn("rc=0 encoded_calls=2", out.stdout)


class ConfigTest(unittest.TestCase):
    """The dashboard's Settings page saves by regenerating reencode.conf."""

    def save(self, conf_text, url=None):
        with tempfile.TemporaryDirectory() as d:
            conf = Path(d) / "reencode.conf"
            conf.write_text(conf_text)
            env = {k: v for k, v in os.environ.items() if k != "REENCODE_NOTIFY_URL"}
            if url:
                env["REENCODE_NOTIFY_URL"] = url
            env["REENCODE_CONFIG"] = str(conf)
            script = f'''source "{ROOT / "common.sh"}"; load_config >/dev/null 2>&1
                         write_config; load_config >/dev/null 2>&1
                         echo "url=${{REENCODE_NOTIFY_URL:-}}"'''
            out = subprocess.run(["bash", "-c", script], env=env, capture_output=True,
                                 text=True, check=True).stdout
            return conf.read_text(), out

    BASE = 'LIBRARIES=("/lib")\nLIBRARY_PROFILES=("tv")\nLOG_DIR="/tmp/l"\nTEMP_DIR="/tmp/t"\nENCODER="software"\n'

    def test_save_keeps_the_line_from_the_file(self):
        line = 'REENCODE_NOTIFY_URL="http://hub:8000/notify/key?tag=media"\n'
        text, out = self.save(self.BASE + line)
        self.assertIn(line, text)
        self.assertIn("url=http://hub:8000/notify/key?tag=media", out)

    def test_save_does_not_write_the_environment_value_to_the_file(self):
        text, out = self.save(self.BASE, url="http://from-env/x")
        self.assertNotIn("from-env", text)
        self.assertIn("url=http://from-env/x", out)

    def test_generated_config_leaves_it_off(self):
        text, out = self.save(self.BASE)
        self.assertIn('# REENCODE_NOTIFY_URL="http://apprise.local', text)
        self.assertIn("url=\n", out)


if __name__ == "__main__":
    unittest.main(verbosity=2)
