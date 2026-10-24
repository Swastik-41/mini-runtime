#!/usr/bin/env python3
"""
Tests for dashboard/server.py. They need no root: the server runs on a random
port against a temporary runtime folder, a fake cgroup tree and
fake_runtime.py instead of ./mini-runtime.

    python3 -m unittest discover -s dashboard/tests -v
"""

import http.client
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

import server  # noqa: E402

FAKE = os.path.join(HERE, "fake_runtime.py")


def wait_for(check, timeout=10, step=0.05):
    end = time.time() + timeout
    while time.time() < end:
        result = check()
        if result:
            return result
        time.sleep(step)
    raise AssertionError("condition not reached in time")


class DashboardTest(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="dash-test-")
        cls.rt = os.path.join(cls.tmp, "rt")
        cls.cg = os.path.join(cls.tmp, "cgroup")
        os.makedirs(os.path.join(cls.rt, "images", "base", "bin"))
        os.makedirs(os.path.join(cls.rt, "images", "second"))
        os.makedirs(os.path.join(cls.rt, "containers"))
        os.makedirs(cls.cg)
        with open(os.path.join(cls.rt, "images", "base", "bin", "sh"), "w") as f:
            f.write("x" * 1000)

        os.environ["FAKE_RT_DIR"] = cls.rt
        os.environ["FAKE_CG_ROOT"] = cls.cg
        server.CONFIG.runtime = FAKE
        server.CONFIG.runtime_dir = cls.rt
        server.CONFIG.cgroup_root = cls.cg

        cls.httpd = server.make_server("127.0.0.1", 0)
        cls.port = server.CONFIG.port
        cls.thread = threading.Thread(target=cls.httpd.serve_forever, daemon=True)
        cls.thread.start()
        cls.external = []

    @classmethod
    def tearDownClass(cls):
        for p in cls.external:
            if p.poll() is None:
                p.kill()
                p.wait()
        server.shutdown_sessions()
        cls.httpd.shutdown()
        cls.httpd.server_close()
        shutil.rmtree(cls.tmp, ignore_errors=True)

    # ---- helpers ---------------------------------------------------

    def request(self, method, path, body=None, headers=None, host=None,
                origin="same", token=True):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        hdrs = {"Host": host or f"127.0.0.1:{self.port}"}
        if origin == "same":
            hdrs["Origin"] = f"http://{hdrs['Host']}"
        elif origin:
            hdrs["Origin"] = origin
        if token is True:
            hdrs["X-Dashboard-Token"] = server.CSRF_TOKEN
        elif token:
            hdrs["X-Dashboard-Token"] = token
        if isinstance(body, (dict, list)):
            body = json.dumps(body)
            hdrs["Content-Type"] = "application/json"
        hdrs.update(headers or {})
        conn.request(method, path, body=body, headers=hdrs)
        resp = conn.getresponse()
        raw = resp.read()
        conn.close()
        try:
            data = json.loads(raw)
        except ValueError:
            data = raw
        return resp.status, data, resp

    def get(self, path, **kw):
        return self.request("GET", path, origin=None, token=None, **kw)

    def post(self, path, body=None, **kw):
        return self.request("POST", path, body=body if body is not None else {}, **kw)

    def container(self, name):
        _, data, _ = self.get("/api/overview")
        return next((c for c in data["containers"] if c["name"] == name), None)

    def start(self, name, **opts):
        status, data, _ = self.post("/api/containers", {"name": name, **opts})
        self.assertEqual(status, 200, data)
        wait_for(lambda: (self.container(name) or {}).get("state") == "RUNNING")

    def wait_ended(self, name):
        wait_for(lambda: (self.container(name) or {}).get("terminal") == "ENDED")

    def make_container(self, name, image=None):
        d = os.path.join(self.rt, "containers", name)
        os.makedirs(os.path.join(d, "upper"), exist_ok=True)
        with open(os.path.join(d, "image"), "w") as f:
            f.write(image or os.path.join(self.rt, "images", "base"))
        return d

    def start_external(self, name):
        p = subprocess.Popen([FAKE, "run", name, "--", "sleep", "30"],
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.external.append(p)
        wait_for(lambda: server.lifecycle(name) == "RUNNING"
                 and os.path.exists(os.path.join(self.cg, "mini-runtime-" + name, "cgroup.procs")))
        return p

    # ---- 1. events -------------------------------------------------

    def test_event_ids_stay_unique_after_trimming(self):
        _, before, _ = self.get("/api/overview")
        first = before["last_event_id"]
        for i in range(server.EVENT_LIMIT + 100):
            server.log_event("x", "start", f"event {i}")
        _, data, _ = self.get(f"/api/overview?since={first}")
        ids = [e["id"] for e in data["events"]]
        self.assertEqual(len(ids), server.EVENT_LIMIT)
        self.assertEqual(len(ids), len(set(ids)))
        self.assertEqual(ids, sorted(ids))
        self.assertEqual(data["last_event_id"], first + server.EVENT_LIMIT + 100)
        _, recent, _ = self.get(f"/api/overview?since={data['last_event_id'] - 3}")
        self.assertEqual([e["id"] for e in recent["events"]], ids[-3:])
        self.assertFalse(recent["reset"])

    def test_cursor_ahead_of_server_resets(self):
        server.log_event("x", "start", "one")
        _, data, _ = self.get("/api/overview?since=999999999")
        self.assertTrue(data["reset"])
        self.assertTrue(data["events"])
        self.assertEqual(data["server_id"], server.SERVER_ID)

    # ---- 2. images -------------------------------------------------

    def test_image_names_are_resolved_inside_the_images_folder(self):
        base = os.path.join(self.rt, "images", "base")
        self.assertEqual(server.resolve_image("base"), os.path.realpath(base))
        self.assertTrue(server.resolve_image("second"))
        os.symlink("/etc", os.path.join(self.rt, "images", "escape"))
        os.symlink(base, os.path.join(self.rt, "images", "alias"))
        with open(os.path.join(self.rt, "images", "afile"), "w") as f:
            f.write("not a directory")
        try:
            self.assertEqual(server.resolve_image("alias"), os.path.realpath(base))
            for bad in ("../../etc", "/etc", "/", "..", ".", "", "missing",
                        "escape", "afile", "base/../..", "a" * 65, "base\x00"):
                with self.subTest(image=bad):
                    with self.assertRaises(server.ApiError):
                        server.resolve_image(bad)
            _, data, _ = self.get("/api/overview")
            self.assertEqual(sorted(data["images"]), ["alias", "base", "second"])
        finally:
            for n in ("escape", "alias", "afile"):
                os.remove(os.path.join(self.rt, "images", n))

    def test_start_rejects_image_paths(self):
        for bad in ("/", "/etc", "../../etc", "missing"):
            with self.subTest(image=bad):
                status, data, _ = self.post("/api/containers", {"name": "imgtest", "image": bad})
                self.assertEqual(status, 400)
                self.assertFalse(data["ok"])
        self.assertIsNone(self.container("imgtest"))

    def test_start_passes_the_resolved_image_path(self):
        self.start("imgok", image="second", command="sleep 30")
        with open(os.path.join(self.rt, "containers", "imgok", "image")) as f:
            self.assertEqual(f.read(), os.path.realpath(os.path.join(self.rt, "images", "second")))
        self.assertEqual(self.container("imgok")["image"], "second")
        self.post("/api/containers/imgok/stop")
        self.wait_ended("imgok")

    # ---- 3. host, origin, token, limits, methods -------------------

    def test_bad_host_is_refused(self):
        for host in ("evil.example", f"evil.example:{self.port}", f"127.0.0.1.evil.example:{self.port}",
                     "localhost:1", "0.0.0.0"):
            with self.subTest(host=host):
                status, _, _ = self.get("/api/overview", host=host)
                self.assertEqual(status, 403)
                status, _, _ = self.post("/api/containers", {"name": "h"}, host=host)
                self.assertEqual(status, 403)
        for host in (f"localhost:{self.port}", f"[::1]:{self.port}", "localhost"):
            with self.subTest(host=host):
                status, _, _ = self.get("/api/overview", host=host)
                self.assertEqual(status, 200)

    def test_post_needs_same_origin(self):
        for origin in (None, "http://evil.example", "null", f"http://localhost:{self.port}"):
            with self.subTest(origin=origin):
                status, data, _ = self.post("/api/containers", {"name": "o1"}, origin=origin)
                self.assertEqual(status, 403, data)
        self.assertIsNone(self.container("o1"))

    def test_post_and_stream_need_the_token(self):
        for token in (None, "wrong", server.CSRF_TOKEN[:-1]):
            with self.subTest(token=token):
                status, _, _ = self.post("/api/containers", {"name": "t1"}, token=token)
                self.assertEqual(status, 403)
        self.make_container("t2")
        status, _, _ = self.get("/api/containers/t2/output")
        self.assertEqual(status, 403)
        status, _, _ = self.get("/api/containers/t2/output?token=nope")
        self.assertEqual(status, 403)

    def test_token_is_injected_into_the_page(self):
        status, body, resp = self.get("/")
        self.assertEqual(status, 200)
        self.assertIn(server.CSRF_TOKEN.encode(), body)
        self.assertNotIn(b"{{CSRF_TOKEN}}", body)

    def test_security_headers(self):
        for path in ("/", "/app.js", "/api/overview", "/api/containers/nope/stats"):
            with self.subTest(path=path):
                _, _, resp = self.get(path)
                self.assertIn("script-src 'self'", resp.getheader("Content-Security-Policy"))
                self.assertIn("frame-ancestors 'none'", resp.getheader("Content-Security-Policy"))
                self.assertEqual(resp.getheader("X-Content-Type-Options"), "nosniff")
                self.assertEqual(resp.getheader("X-Frame-Options"), "DENY")
                self.assertEqual(resp.getheader("Referrer-Policy"), "no-referrer")

    def test_body_size_limit(self):
        big = json.dumps({"name": "big", "command": "x" * (server.MAX_BODY + 10)})
        status, data, _ = self.post("/api/containers", big)
        self.assertEqual(status, 413)
        self.assertFalse(data["ok"])

    def test_invalid_json(self):
        for body in ("{not json", "[1,2]", "\"text\"", "\xff"):
            with self.subTest(body=body):
                status, data, _ = self.post("/api/containers", body.encode("latin-1"))
                self.assertEqual(status, 400)
                self.assertFalse(data["ok"])

    def test_unsupported_methods(self):
        for method in ("PUT", "DELETE", "PATCH", "OPTIONS"):
            with self.subTest(method=method):
                status, data, resp = self.request(method, "/api/containers")
                self.assertEqual(status, 405)
                self.assertEqual(resp.getheader("Allow"), "GET, POST")

    # ---- 4. query parsing ------------------------------------------

    def test_bad_query_values_get_400(self):
        for value in ("abc", "-1", "1.5", "", "9" * 30, "1e3", "%20"):
            with self.subTest(since=value):
                status, data, _ = self.get(f"/api/overview?since={value}")
                self.assertEqual(status, 400)
                self.assertFalse(data["ok"])
        status, _, _ = self.get("/api/overview?since=0")
        self.assertEqual(status, 200)

    def test_bad_output_offset_gets_400(self):
        self.make_container("q1")
        status, _, _ = self.get(f"/api/containers/q1/output?token={server.CSRF_TOKEN}&from=abc")
        self.assertEqual(status, 400)

    def test_input_validation(self):
        cases = [
            {"name": ""}, {"name": "a b"}, {"name": "../x"}, {"name": "x" * 65},
            {"name": "v1", "memory": "100"}, {"name": "v1", "memory": "1T"},
            {"name": "v1", "cpu": 0}, {"name": "v1", "cpu": 101}, {"name": "v1", "cpu": "lots"},
            {"name": "v1", "pids": 0}, {"name": "v1", "pids": 40000},
            {"name": "v1", "command": "echo 'unclosed"},
            {"name": "v1", "command": "x" * 4097},
        ]
        for body in cases:
            with self.subTest(body=str(body)[:60]):
                status, data, _ = self.post("/api/containers", body)
                self.assertEqual(status, 400, data)
        self.assertIsNone(self.container("v1"))

    # ---- 5. lifecycle and terminal state ---------------------------

    def test_external_container_is_running_elsewhere(self):
        p = self.start_external("ext1")
        c = self.container("ext1")
        self.assertEqual(c["state"], "RUNNING")
        self.assertEqual(c["terminal"], "ELSEWHERE")
        self.assertEqual(c["limits"]["source"], "cgroup")
        self.assertEqual(c["limits"]["cpu"], 50)
        status, _, _ = self.get(f"/api/containers/ext1/output?token={server.CSRF_TOKEN}")
        self.assertEqual(status, 409)
        status, _, _ = self.post("/api/containers", {"name": "ext1"})
        self.assertEqual(status, 409)
        p.kill()
        p.wait()
        wait_for(lambda: self.container("ext1")["state"] == "STOPPED")
        self.assertEqual(self.container("ext1")["terminal"], "NONE")
        self.assertIsNone(self.container("ext1")["limits"])

    def test_running_state_survives_a_dashboard_restart(self):
        self.start("restart1", command="sleep 30")
        with server.sessions_lock:
            saved = dict(server.sessions)
            server.sessions.clear()    # what a restarted dashboard knows
        try:
            c = self.container("restart1")
            self.assertEqual(c["state"], "RUNNING")
            self.assertEqual(c["terminal"], "ELSEWHERE")
        finally:
            with server.sessions_lock:
                server.sessions.update(saved)
        self.post("/api/containers/restart1/stop")
        self.wait_ended("restart1")

    def test_shutdown_stops_only_dashboard_containers(self):
        p = self.start_external("ext2")
        self.start("own2", command="sleep 30")
        server.shutdown_sessions(timeout=5)
        self.assertEqual(server.lifecycle("own2"), "STOPPED")
        self.assertFalse(server.sessions["own2"].running)
        self.assertIsNone(p.poll(), "the external container must keep running")
        self.assertEqual(server.lifecycle("ext2"), "RUNNING")
        p.kill()
        p.wait()

    # ---- 6. metrics ------------------------------------------------

    def write_cgroup(self, name, files):
        cg = os.path.join(self.cg, "mini-runtime-" + name)
        os.makedirs(cg, exist_ok=True)
        for k, v in files.items():
            with open(os.path.join(cg, k), "w") as f:
                f.write(v)
        return cg

    def test_stats_without_cgroup_are_unavailable(self):
        self.make_container("s0")
        status, data, _ = self.get("/api/containers/s0/stats")
        self.assertEqual(status, 200)
        self.assertEqual(data["metrics"], "UNAVAILABLE")
        self.assertNotIn("memory_current", data)

    def test_missing_and_malformed_counters_are_null(self):
        self.make_container("s1")
        cg = self.write_cgroup("s1", {
            "memory.current": "garbage", "memory.max": "max",
            "cpu.stat": "usage_usec nope\n", "pids.current": "-4", "cpu.max": "max 100000",
        })
        try:
            _, data, _ = self.get("/api/containers/s1/stats")
            self.assertEqual(data["metrics"], "AVAILABLE")
            self.assertIsNone(data["memory_current"])
            self.assertEqual(data["memory_max"], "max")
            self.assertIsNone(data["memory_peak"])
            self.assertIsNone(data["cpu_usage_usec"])
            self.assertIsNone(data["cpu_throttled"])
            self.assertEqual(data["cpu_limit_percent"], "max")
            self.assertIsNone(data["pids_current"])
            self.assertIsNone(data["pids_max"])
            self.assertIsNone(data["oom_kills"])
        finally:
            shutil.rmtree(cg)

    def test_good_counters(self):
        self.make_container("s2")
        cg = self.write_cgroup("s2", {
            "memory.current": "2048", "memory.max": "104857600", "memory.peak": "4096",
            "cpu.stat": "usage_usec 12345\nnr_throttled 7\n", "cpu.max": "25000 100000",
            "pids.current": "3", "pids.max": "64", "memory.events": "oom 1\noom_kill 2\n",
        })
        try:
            _, d, _ = self.get("/api/containers/s2/stats")
            self.assertEqual((d["memory_current"], d["memory_max"], d["memory_peak"]), (2048, 104857600, 4096))
            self.assertEqual((d["cpu_usage_usec"], d["cpu_throttled"], d["cpu_limit_percent"]), (12345, 7, 25))
            self.assertEqual((d["pids_current"], d["pids_max"], d["oom_kills"]), (3, 64, 2))
        finally:
            shutil.rmtree(cg)

    def test_cgroup_removed_mid_poll(self):
        self.make_container("s3")
        cg = self.write_cgroup("s3", {"memory.current": "10"})
        real = server.read_counter

        def vanish(path):
            shutil.rmtree(cg, ignore_errors=True)
            return real(path)

        with mock.patch.object(server, "read_counter", side_effect=vanish):
            status, data, _ = self.get("/api/containers/s3/stats")
        self.assertEqual(status, 200)
        self.assertEqual(data["metrics"], "UNAVAILABLE")

    # ---- 7. terminal sessions --------------------------------------

    def read_stream(self, name, until, timeout=10):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=timeout)
        conn.request("GET", f"/api/containers/{name}/output?from=0&token={server.CSRF_TOKEN}",
                     headers={"Host": f"127.0.0.1:{self.port}"})
        resp = conn.getresponse()
        self.assertEqual(resp.status, 200)
        import base64
        text = ""
        end = time.time() + timeout
        while time.time() < end:
            line = resp.fp.readline().decode()
            if line.startswith("data: ") and not text.endswith("\x00"):
                try:
                    text += base64.b64decode(line[6:].strip()).decode(errors="replace")
                except ValueError:
                    pass
            if line.startswith("event: end"):
                text += "\x00END"
            if until in text:
                break
        conn.close()
        return text

    def test_terminal_input_output_and_reaping(self):
        self.start("term1")
        self.assertEqual(self.container("term1")["terminal"], "ATTACHED")
        self.assertIn("fake-shell ready", self.read_stream("term1", "fake-shell ready"))
        status, _, _ = self.post("/api/containers/term1/input", b"hello\n")
        self.assertEqual(status, 200)
        self.assertIn("got:hello", self.read_stream("term1", "got:hello"))
        status, _, _ = self.post("/api/containers/term1/resize", {"rows": 40, "cols": 120})
        self.assertEqual(status, 200)
        status, _, _ = self.post("/api/containers/term1/resize", {"rows": "x"})
        self.assertEqual(status, 400)

        self.post("/api/containers/term1/input", b"exit\n")
        self.wait_ended("term1")
        c = self.container("term1")
        self.assertEqual(c["state"], "STOPPED")
        self.assertEqual(c["exit_code"], 0)
        self.assertEqual(c["options"]["memory"], "100M")
        session = server.sessions["term1"]
        self.assertFalse(session.thread.is_alive())
        self.assertIsNone(session.fd)  # pty descriptor closed
        self.assertIn("END", self.read_stream("term1", "END"))

        status, data, _ = self.post("/api/containers/term1/input", b"again\n")
        self.assertEqual(status, 409)
        self.assertFalse(data["ok"])

    def test_input_is_size_limited(self):
        self.start("term2", command="sleep 30")
        status, _, _ = self.post("/api/containers/term2/input", b"x" * (server.MAX_BODY + 1))
        self.assertEqual(status, 413)
        self.post("/api/containers/term2/stop")
        self.wait_ended("term2")

    def test_stop_records_exit_and_restart_works(self):
        self.start("rerun", memory="64M", cpu=30, pids=20, command="sleep 30")
        status, _, _ = self.post("/api/containers/rerun/stop")
        self.assertEqual(status, 200)
        self.wait_ended("rerun")
        self.assertEqual(self.container("rerun")["exit_code"], 137)
        status, _, _ = self.post("/api/containers/rerun/stop")
        self.assertEqual(status, 409)

        opts = self.container("rerun")["options"]
        self.assertEqual(opts, {"memory": "64M", "cpu": 30, "pids": 20, "command": "sleep 30"})
        self.start("rerun", **opts)    # the Start button sends the saved options back
        self.assertEqual(self.container("rerun")["limits"]["cpu"], 30)
        self.post("/api/containers/rerun/stop")
        self.wait_ended("rerun")

    def test_finished_sessions_are_cleaned_up(self):
        status, _, _ = self.post("/api/containers", {"name": "old1", "command": "true"})
        self.assertEqual(status, 200)    # exits at once, so it may never be seen RUNNING
        self.wait_ended("old1")
        server.cleanup_sessions(time.time() + server.FINISHED_SESSION_TTL - 60)
        self.assertIn("old1", server.sessions)
        server.cleanup_sessions(time.time() + server.FINISHED_SESSION_TTL + 60)
        self.assertNotIn("old1", server.sessions)

    def test_finished_sessions_are_capped(self):
        fakes = []
        with server.sessions_lock:
            for i in range(server.MAX_FINISHED_SESSIONS + 5):
                s = server.Session(f"cap{i}", {})
                s.running, s.ended = False, time.time() - 100 + i
                server.sessions[s.name] = s
                fakes.append(s.name)
        server.cleanup_sessions()
        kept = [n for n in fakes if n in server.sessions]
        self.assertLessEqual(len([s for s in server.sessions.values() if not s.running]),
                             server.MAX_FINISHED_SESSIONS)
        self.assertNotIn("cap0", kept)
        self.assertIn(fakes[-1], kept)
        with server.sessions_lock:
            for n in fakes:
                server.sessions.pop(n, None)

    # ---- 10. status codes ------------------------------------------

    def test_unknown_container_is_404(self):
        for path in ("/api/containers/nope/stats", "/api/containers/nope/diff",
                     "/api/containers/..%2f..%2fetc/stats"):
            with self.subTest(path=path):
                status, data, _ = self.get(path)
                self.assertEqual(status, 404)
                self.assertFalse(data["ok"])
        for action in ("stop", "delete", "input", "resize"):
            with self.subTest(action=action):
                status, _, _ = self.post(f"/api/containers/nope/{action}")
                self.assertEqual(status, 404)
        status, _, _ = self.get("/no/such/page")
        self.assertEqual(status, 404)

    def test_delete_rules(self):
        self.start("del1", command="sleep 30")
        status, _, _ = self.post("/api/containers/del1/delete")
        self.assertEqual(status, 409)
        self.post("/api/containers/del1/stop")
        self.wait_ended("del1")
        status, _, _ = self.post("/api/containers/del1/delete")
        self.assertEqual(status, 200)
        self.assertIsNone(self.container("del1"))
        status, _, _ = self.post("/api/containers/del1/delete")
        self.assertEqual(status, 404)

    def test_runtime_failures_give_a_generic_500(self):
        self.make_container("broken1")
        self.make_container("undeletable1")
        status, data, _ = self.get("/api/containers/broken1/diff")
        self.assertEqual(status, 500)
        self.assertNotIn("secret", data["error"])
        status, data, _ = self.post("/api/containers/undeletable1/delete")
        self.assertEqual(status, 500)
        self.assertNotIn("busy", data["error"])

    def test_unexpected_errors_give_a_generic_500(self):
        with mock.patch.object(server, "list_containers", side_effect=RuntimeError("boom /x/y")):
            with self.assertLogs("dashboard", "ERROR"):
                status, data, _ = self.get("/api/overview")
        self.assertEqual(status, 500)
        self.assertNotIn("boom", data["error"])

    # ---- 8. sizes --------------------------------------------------

    def test_diff_and_sizes(self):
        d = self.make_container("diff1")
        with open(os.path.join(d, "upper", "hello.txt"), "w") as f:
            f.write("hi" * 50)
        status, data, _ = self.get("/api/containers/diff1/diff")
        self.assertEqual(status, 200)
        self.assertEqual([c["kind"] for c in data["changes"]], ["added", "modified", "deleted"])
        self.assertEqual(data["changes"][2]["path"], "/<b>bold</b>.txt")
        self.assertEqual(data["upper"]["apparent"], 100)
        self.assertIsNotNone(data["upper"]["disk"])
        self.assertTrue(data["upper"]["complete"])
        self.assertGreaterEqual(data["image"]["apparent"], 1000)

    def test_missing_image_size_is_null(self):
        self.make_container("diff2", image=os.path.join(self.tmp, "gone"))
        _, data, _ = self.get("/api/containers/diff2/diff")
        self.assertIsNone(data["image"]["apparent"])
        self.assertIsNone(data["image"]["disk"])
        self.assertEqual(data["image"]["error"], "not found")

    def test_unreadable_entries_are_reported(self):
        d = os.path.join(self.tmp, "measure")
        os.makedirs(os.path.join(d, "sub"))
        with open(os.path.join(d, "a"), "w") as f:
            f.write("12345")
        with mock.patch.object(server.os, "lstat", side_effect=FileNotFoundError("gone")):
            result = server.measure(d)
        self.assertFalse(result["complete"])
        self.assertIn("could not be read", result["error"])
        self.assertEqual(server.measure(d)["apparent"], 5 + os.lstat(os.path.join(d, "sub")).st_size)


if __name__ == "__main__":
    unittest.main()
