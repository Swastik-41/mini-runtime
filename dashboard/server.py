#!/usr/bin/env python3
"""
Web dashboard for mini-runtime.

Runs the same ./mini-runtime binary the terminal uses, and reads the live
cgroup v2 counters of each running container. Only the Python standard
library is used, so nothing needs to be installed.

This is a privileged, local-only tool: it listens on 127.0.0.1, accepts only
local Host headers, and every state-changing request must carry the random
token embedded in the page it served. It is not hardened for remote access.

Usage (from the project folder, after "make setup" and "make"):
    sudo python3 dashboard/server.py            # http://localhost:8080
    sudo python3 dashboard/server.py --port 9000
"""

import argparse
import base64
import errno
import fcntl
import hmac
import json
import logging
import os
import pty
import pwd
import re
import secrets
import shlex
import signal
import struct
import subprocess
import termios
import threading
import time
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

log = logging.getLogger("dashboard")

HERE = os.path.dirname(os.path.abspath(__file__))
STATIC = os.path.join(HERE, "static")

NAME_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
IMAGE_NAME_RE = re.compile(r"^[A-Za-z0-9_.-]{1,64}$")
MEMORY_RE = re.compile(r"^[0-9]{1,10}[KMGkmg]$")

EVENT_LIMIT = 500                   # events kept in memory
BUFFER_LIMIT = 1024 * 1024          # terminal history kept per container
MAX_BODY = 64 * 1024                # largest request body accepted
FINISHED_SESSION_TTL = 30 * 60      # finished terminals are kept this long
MAX_FINISHED_SESSIONS = 20
MAX_QUERY_INT = 2 ** 53


def default_runtime_dir():
    """Same rule as getRuntimeDir() in src/main.cpp."""
    sudo_user = os.environ.get("SUDO_USER")
    if sudo_user and os.geteuid() == 0:
        try:
            return os.path.join(pwd.getpwnam(sudo_user).pw_dir, ".mini-runtime")
        except KeyError:
            pass
    return os.path.join(os.environ.get("HOME", "/root"), ".mini-runtime")


class Config:
    runtime = os.path.abspath(os.path.join(HERE, "..", "mini-runtime"))
    runtime_dir = default_runtime_dir()
    cgroup_root = "/sys/fs/cgroup"
    port = 8080


CONFIG = Config()
SERVER_ID = secrets.token_hex(8)    # changes on every dashboard start
CSRF_TOKEN = secrets.token_urlsafe(32)


# --------------------------------------------------------------------
# Errors returned to the browser
# --------------------------------------------------------------------

class ApiError(Exception):
    status = HTTPStatus.BAD_REQUEST

    def __init__(self, message, status=None):
        super().__init__(message)
        if status is not None:
            self.status = status


class NotFound(ApiError):
    status = HTTPStatus.NOT_FOUND


class Conflict(ApiError):
    status = HTTPStatus.CONFLICT


class Forbidden(ApiError):
    status = HTTPStatus.FORBIDDEN


# --------------------------------------------------------------------
# Events
# --------------------------------------------------------------------

events = []
events_lock = threading.Lock()
_last_event_id = 0


def log_event(name, kind, text):
    global _last_event_id
    with events_lock:
        _last_event_id += 1
        events.append({
            "id": _last_event_id,
            "time": time.time(),
            "container": name,
            "kind": kind,              # start | exit | oom | stop | delete | error
            "text": text,
        })
        del events[:-EVENT_LIMIT]


def events_since(since):
    """Events newer than the cursor. A cursor from an earlier server run (ahead
    of this one) gets everything kept, flagged so the page can reset."""
    with events_lock:
        last = _last_event_id
        reset = since > last
        start = 0 if reset else since
        return [dict(e) for e in events if e["id"] > start], last, reset


# --------------------------------------------------------------------
# Containers started from the dashboard (each one runs behind a pty)
# --------------------------------------------------------------------

class Session:
    def __init__(self, name, options):
        self.name = name
        self.options = options
        self.buffer = bytearray()
        self.base = 0                  # stream offset of buffer[0]
        self.cond = threading.Condition()
        self.running = True
        self.exit_code = None
        self.started = time.time()
        self.ended = None
        self.pid = None
        self.fd = None
        self.thread = None

    def start(self, argv):
        pid, fd = pty.fork()
        if pid == 0:
            os.environ["TERM"] = "xterm-256color"
            try:
                os.execv(argv[0], argv)
            finally:
                os._exit(127)

        self.pid, self.fd = pid, fd
        self.resize(32, 100)
        self.thread = threading.Thread(target=self._pump, daemon=True)
        self.thread.start()

    def _pump(self):
        while True:
            try:
                data = os.read(self.fd, 65536)
            except OSError:
                break
            if not data:
                break
            with self.cond:
                self.buffer += data
                if len(self.buffer) > BUFFER_LIMIT:
                    cut = len(self.buffer) - BUFFER_LIMIT
                    del self.buffer[:cut]
                    self.base += cut
                self.cond.notify_all()

        try:
            _, status = os.waitpid(self.pid, 0)
            code = os.waitstatus_to_exitcode(status)
        except ChildProcessError:
            code = None

        with self.cond:
            try:
                os.close(self.fd)
            except OSError:
                pass
            self.fd = None
            self.running = False
            self.exit_code = code
            self.ended = time.time()
            tail = bytes(self.buffer[-4000:]).decode("utf-8", "replace")
            self.cond.notify_all()

        if "OOM killer" in tail:
            log_event(self.name, "oom", "Memory limit reached: the kernel OOM killer stopped it")
        log_event(self.name, "exit", f"Exited with code {code}")

    def write(self, data):
        with self.cond:
            if not self.running:
                raise Conflict("This terminal session has ended.")
            try:
                os.write(self.fd, data)
            except OSError:
                raise Conflict("This terminal session has ended.")

    def resize(self, rows, cols):
        with self.cond:
            if self.running and self.fd is not None:
                try:
                    fcntl.ioctl(self.fd, termios.TIOCSWINSZ,
                                struct.pack("HHHH", rows, cols, 0, 0))
                except OSError:
                    pass

    def read_from(self, offset, timeout):
        """Returns (next_offset, data, running), waiting up to timeout."""
        with self.cond:
            end = self.base + len(self.buffer)
            if offset >= end and self.running:
                self.cond.wait(timeout)
                end = self.base + len(self.buffer)
            start = max(offset, self.base)
            data = bytes(self.buffer[start - self.base:]) if start < end else b""
            return end, data, self.running


sessions = {}
sessions_lock = threading.Lock()


def cleanup_sessions(now=None):
    """Drop finished terminals after FINISHED_SESSION_TTL, and keep at most
    MAX_FINISHED_SESSIONS of them, so old output does not pile up."""
    now = now or time.time()
    with sessions_lock:
        finished = sorted((s for s in sessions.values() if not s.running),
                          key=lambda s: s.ended or 0)
        for s in finished:
            if now - (s.ended or now) > FINISHED_SESSION_TTL:
                sessions.pop(s.name, None)
        finished = [s for s in finished if s.name in sessions]
        for s in finished[:max(0, len(finished) - MAX_FINISHED_SESSIONS)]:
            sessions.pop(s.name, None)


def shutdown_sessions(timeout=5):
    """Stops only the containers this dashboard started; containers started
    from a terminal are left alone."""
    with sessions_lock:
        running = [s for s in sessions.values() if s.running]
    for s in running:
        print(f"Stopping {s.name} (started from the dashboard)")
        kill_cgroup(s.name)
    deadline = time.time() + timeout
    for s in running:
        s.thread.join(max(0, deadline - time.time()))
        if s.running:                  # the runtime itself did not exit
            try:
                os.kill(s.pid, signal.SIGKILL)
            except OSError:
                pass
            s.thread.join(1)


# --------------------------------------------------------------------
# Reading container state from disk and from cgroups
# --------------------------------------------------------------------

def read_text(path):
    """File contents, or None when missing or unreadable."""
    try:
        with open(path) as f:
            return f.read().strip()
    except (OSError, UnicodeDecodeError):
        return None


def read_counter(path):
    """An integer counter: int, "max" for unlimited, or None when missing
    or malformed (never a fake 0)."""
    value = read_text(path)
    if value is None:
        return None
    if value == "max":
        return "max"
    try:
        number = int(value)
    except ValueError:
        return None
    return number if number >= 0 else None


def read_keyed(path):
    text = read_text(path)
    if text is None:
        return None
    result = {}
    for line in text.splitlines():
        parts = line.split()
        if len(parts) == 2 and parts[1].isdigit():
            result[parts[0]] = int(parts[1])
    return result


def container_dir(name):
    return os.path.join(CONFIG.runtime_dir, "containers", name)


def require_container(name):
    if not NAME_RE.match(name) or not os.path.isdir(container_dir(name)):
        raise NotFound(f"No container named '{name}'.")


def lifecycle(name):
    """RUNNING / STOPPED / UNKNOWN from the runtime's own lock file, the same
    check as containerState() in src/main.cpp."""
    path = os.path.join(CONFIG.runtime_dir, "locks", name + ".lock")
    try:
        fd = os.open(path, os.O_RDONLY)
    except FileNotFoundError:
        return "STOPPED"
    except OSError:
        return "UNKNOWN"
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        fcntl.flock(fd, fcntl.LOCK_UN)
        return "STOPPED"
    except OSError as e:
        return "RUNNING" if e.errno in (errno.EWOULDBLOCK, errno.EAGAIN) else "UNKNOWN"
    finally:
        os.close(fd)


def cgroup_path(name):
    return os.path.join(CONFIG.cgroup_root, "mini-runtime-" + name)


def cpu_limit(cg):
    text = read_text(os.path.join(cg, "cpu.max"))
    if text is None:
        return None
    parts = text.split()
    try:
        if parts[0] == "max":
            return "max"
        return round(int(parts[0]) * 100 / int(parts[1]))
    except (IndexError, ValueError, ZeroDivisionError):
        return None


def stats(name):
    require_container(name)
    cg = cgroup_path(name)
    if not os.path.isdir(cg):
        return {"metrics": "UNAVAILABLE", "time": time.time()}

    cpu_stat = read_keyed(os.path.join(cg, "cpu.stat"))
    mem_events = read_keyed(os.path.join(cg, "memory.events"))
    result = {
        "metrics": "AVAILABLE",
        "time": time.time(),
        "memory_current": read_counter(os.path.join(cg, "memory.current")),
        "memory_max": read_counter(os.path.join(cg, "memory.max")),
        "memory_peak": read_counter(os.path.join(cg, "memory.peak")),
        "cpu_usage_usec": cpu_stat.get("usage_usec") if cpu_stat else None,
        "cpu_throttled": cpu_stat.get("nr_throttled") if cpu_stat else None,
        "cpu_limit_percent": cpu_limit(cg),
        "pids_current": read_counter(os.path.join(cg, "pids.current")),
        "pids_max": read_counter(os.path.join(cg, "pids.max")),
        "oom_kills": mem_events.get("oom_kill") if mem_events else None,
    }
    # The container may have exited (and its cgroup gone) while we read.
    if not os.path.isdir(cg):
        return {"metrics": "UNAVAILABLE", "time": time.time()}
    return result


def measure(path):
    """Apparent size and allocated disk usage of a directory tree.
    Returns {"apparent": int|None, "disk": int|None, "complete": bool,
    "error": str|None}; unavailable sizes are None, never 0."""
    if not os.path.isdir(path):
        return {"apparent": None, "disk": None, "complete": False,
                "error": "not found"}
    if not os.access(path, os.R_OK | os.X_OK):
        return {"apparent": None, "disk": None, "complete": False,
                "error": "not readable"}

    apparent = disk = 0
    problems = []

    def onerror(err):
        problems.append(err)

    for root, dirs, files in os.walk(path, onerror=onerror):
        for entry in dirs + files:
            try:
                info = os.lstat(os.path.join(root, entry))
            except OSError as err:     # deleted or unreadable during the scan
                problems.append(err)
                continue
            apparent += info.st_size
            disk += info.st_blocks * 512

    return {"apparent": apparent, "disk": disk, "complete": not problems,
            "error": None if not problems else f"{len(problems)} entries could not be read"}


def known_limits(name, session, state):
    """Limits only when actually known: from the dashboard's own start
    options, or from the live cgroup. Otherwise None."""
    if state == "RUNNING":
        cg = cgroup_path(name)
        if os.path.isdir(cg):
            return {"source": "cgroup",
                    "memory": read_counter(os.path.join(cg, "memory.max")),
                    "cpu": cpu_limit(cg),
                    "pids": read_counter(os.path.join(cg, "pids.max"))}
    if session:
        o = session.options
        return {"source": "dashboard", "memory": o.get("memory"),
                "cpu": o.get("cpu"), "pids": o.get("pids")}
    return None


def list_containers():
    base = os.path.join(CONFIG.runtime_dir, "containers")
    try:
        names = set(os.listdir(base))
    except OSError:
        names = set()
    with sessions_lock:
        # A container started here a moment ago may not have its folder yet.
        launched = {n for n, s in sessions.items() if s.running}

    result = []
    for name in sorted(names | launched):
        if name not in launched and (not NAME_RE.match(name)
                                     or not os.path.isdir(os.path.join(base, name))):
            continue

        with sessions_lock:
            session = sessions.get(name)
        state = lifecycle(name)
        if state == "STOPPED" and session and session.running:
            state = "STARTING"         # launched here; the runtime has not locked yet
        image = read_text(os.path.join(base, name, "image"))

        if session and session.running:
            terminal = "ATTACHED"
        elif session:
            terminal = "ENDED"         # finished here; output is kept
        elif state == "RUNNING":
            terminal = "ELSEWHERE"     # started from a terminal, or before a dashboard restart
        else:
            terminal = "NONE"

        result.append({
            "name": name,
            "image": os.path.basename(image) if image else None,
            "state": state,
            "terminal": terminal,
            "exit_code": session.exit_code if session and not session.running else None,
            "options": session.options if session else None,
            "limits": known_limits(name, session, state),
        })
    return result


def images_root():
    return os.path.join(CONFIG.runtime_dir, "images")


def list_images():
    root = images_root()
    try:
        entries = sorted(os.listdir(root))
    except OSError:
        return []
    result = []
    for entry in entries:
        try:
            result.append(resolve_image(entry) and entry)
        except ApiError:
            pass
    return result


def resolve_image(name):
    """An image name under ~/.mini-runtime/images, resolved canonically and
    required to stay inside that folder (no paths, '..' or symlink escapes)."""
    if not IMAGE_NAME_RE.match(name) or name in (".", ".."):
        raise ApiError("Image: pick one of the images in the list.")
    root = os.path.realpath(images_root())
    path = os.path.realpath(os.path.join(root, name))
    if os.path.commonpath([root, path]) != root or path == root:
        raise ApiError("Image: that image points outside the images folder.")
    if not os.path.isdir(path):
        raise ApiError(f"Image '{name}' does not exist.")
    return path


def run_runtime(args):
    proc = subprocess.run([CONFIG.runtime] + args, capture_output=True,
                          text=True, timeout=60)
    return proc.returncode, (proc.stdout + proc.stderr).strip()


def container_diff(name):
    require_container(name)
    code, out = run_runtime(["diff", name])
    if code != 0:
        log.warning("diff %s failed (%s): %s", name, code, out)
        raise ApiError("Could not read the container's changes. "
                       "Details are in the dashboard console.",
                       HTTPStatus.INTERNAL_SERVER_ERROR)

    changes = []
    for line in out.splitlines():
        m = re.match(r"^\[(ADDED|MODIFIED|DELETED)\]\s+(.*)$", line)
        if m:
            changes.append({"kind": m.group(1).lower(), "path": m.group(2)})

    image = read_text(os.path.join(container_dir(name), "image"))
    return {
        "changes": changes,
        "upper": measure(os.path.join(container_dir(name), "upper")),
        "image": measure(image) if image else
                 {"apparent": None, "disk": None, "complete": False, "error": "unknown image"},
    }


# --------------------------------------------------------------------
# Actions
# --------------------------------------------------------------------

def start_container(body):
    if not isinstance(body, dict):
        raise ApiError("Expected a JSON object.")

    name = str(body.get("name", ""))
    if not NAME_RE.match(name):
        raise ApiError("Name: use letters, numbers, _ or - (up to 64).")

    argv = [CONFIG.runtime, "run", name]
    options = {}

    memory = str(body.get("memory") or "100M")
    if not MEMORY_RE.match(memory):
        raise ApiError("Memory: a number with K, M or G, such as 100M.")
    argv += ["--memory", memory]
    options["memory"] = memory

    for key, low, high, default in (("cpu", 1, 100, 50), ("pids", 1, 32768, 64)):
        raw = body.get(key)
        try:
            value = default if raw in (None, "") else int(raw)
        except (TypeError, ValueError):
            raise ApiError(f"{key.upper()}: a whole number.")
        if not low <= value <= high:
            raise ApiError(f"{key.upper()}: from {low} to {high}.")
        argv += ["--" + key, str(value)]
        options[key] = value

    image = str(body.get("image") or "").strip()
    if image:
        argv += ["--image", resolve_image(image)]
        options["image"] = image

    if body.get("net"):
        argv.append("--net")
        options["net"] = True

    command = str(body.get("command") or "").strip()
    if len(command) > 4096:
        raise ApiError("Command: too long.")
    if command:
        try:
            parts = shlex.split(command)
        except ValueError as e:
            raise ApiError(f"Command: {e}.")
        argv += ["--"] + parts
        options["command"] = command

    with sessions_lock:
        old = sessions.get(name)
        if (old and old.running) or lifecycle(name) == "RUNNING":
            raise Conflict(f"'{name}' is already running.")

        session = Session(name, options)
        session.start(argv)
        sessions[name] = session

    log_event(name, "start", "Started: " + (command or "/bin/sh") +
              f" (memory {options['memory']}, CPU {options['cpu']}%, PIDs {options['pids']}"
              + (", network" if options.get("net") else "") + ")")
    return {"ok": True}


def kill_cgroup(name):
    """Kills every process in the container's cgroup. Returns False if the
    container has no cgroup (it is not running)."""
    cg = cgroup_path(name)
    if not os.path.isdir(cg):
        return False

    kill_file = os.path.join(cg, "cgroup.kill")
    try:
        if os.path.exists(kill_file):
            with open(kill_file, "w") as f:
                f.write("1")
            return True
    except OSError:
        pass

    for pid in (read_text(os.path.join(cg, "cgroup.procs")) or "").split():
        try:
            os.kill(int(pid), signal.SIGKILL)
        except (OSError, ValueError):
            pass
    return True


def stop_container(name):
    require_container(name)
    if lifecycle(name) != "RUNNING":
        raise Conflict(f"'{name}' is not running.")
    if not kill_cgroup(name):
        raise Conflict(f"'{name}' has no live cgroup to stop.")
    log_event(name, "stop", "Stopped from the dashboard")
    return {"ok": True}


def delete_container(name):
    require_container(name)
    if lifecycle(name) == "RUNNING":
        raise Conflict(f"'{name}' is running. Stop it first.")
    code, out = run_runtime(["delete", name])
    if code != 0:
        log.warning("delete %s failed (%s): %s", name, code, out)
        raise ApiError("Delete failed. Details are in the dashboard console.",
                       HTTPStatus.INTERNAL_SERVER_ERROR)
    with sessions_lock:
        sessions.pop(name, None)
    log_event(name, "delete", "Deleted")
    return {"ok": True}


def session_for(name):
    with sessions_lock:
        session = sessions.get(name) if NAME_RE.match(name) else None
    if not session:
        require_container(name)
        raise Conflict(f"'{name}' has no terminal session in this dashboard.")
    return session


# --------------------------------------------------------------------
# HTTP
# --------------------------------------------------------------------

STATIC_FILES = {
    "/": ("index.html", "text/html; charset=utf-8"),
    "/app.js": ("app.js", "text/javascript; charset=utf-8"),
    "/style.css": ("style.css", "text/css; charset=utf-8"),
    "/vendor/xterm.js": ("vendor/xterm.js", "text/javascript; charset=utf-8"),
    "/vendor/xterm.css": ("vendor/xterm.css", "text/css; charset=utf-8"),
    "/vendor/addon-fit.js": ("vendor/addon-fit.js", "text/javascript; charset=utf-8"),
}

ROUTE = re.compile(r"^/api/containers/([^/]{1,200})/(\w+)$")
LOCAL_HOSTS = ("localhost", "127.0.0.1", "[::1]")

# xterm.js adds <style> elements for its theme, so styles need 'unsafe-inline';
# scripts are only the files served above.
CSP = ("default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; "
       "img-src 'self' data:; connect-src 'self'; object-src 'none'; "
       "base-uri 'none'; form-action 'self'; frame-ancestors 'none'")


def parse_int(query, key, default, high=MAX_QUERY_INT):
    values = query.get(key)
    if values is None:
        return default
    value = values[0]
    if not value.isdigit() or len(value) > 16:
        raise ApiError(f"'{key}' must be a whole number from 0 to {high}.")
    number = int(value)
    if number > high:
        raise ApiError(f"'{key}' must be a whole number from 0 to {high}.")
    return number


class Handler(BaseHTTPRequestHandler):
    server_version = "mini-runtime-dashboard"
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        log.debug("%s %s", self.address_string(), fmt % args)

    # ---- responses -------------------------------------------------

    def common_headers(self):
        self.send_header("Content-Security-Policy", CSP)
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Cache-Control", "no-store")

    def send_json(self, data, status=HTTPStatus.OK):
        body = json.dumps(data).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.common_headers()
        self.end_headers()
        self.wfile.write(body)

    def send_error_json(self, status, message):
        self.send_json({"ok": False, "error": message}, status)

    def guard(self, action):
        try:
            self.send_json(action())
        except ApiError as e:
            # An unread request body would be parsed as the next request.
            self.close_connection = True
            self.send_error_json(e.status, str(e))
        except Exception:
            self.close_connection = True
            log.exception("Unexpected error on %s %s", self.command, self.path)
            self.send_error_json(HTTPStatus.INTERNAL_SERVER_ERROR,
                                 "Internal error. Details are in the dashboard console.")

    # ---- request checks --------------------------------------------

    def host_ok(self):
        host = self.headers.get("Host", "")
        return host in LOCAL_HOSTS or host in (f"{h}:{CONFIG.port}" for h in LOCAL_HOSTS)

    def check_mutation(self, query=None):
        """Every state-changing request (and the terminal stream) must come
        from the dashboard's own page: same Origin and the page's token."""
        if self.command == "POST":
            origin = self.headers.get("Origin")
            if not origin or origin != f"http://{self.headers.get('Host', '')}":
                raise Forbidden("Request refused: it did not come from this dashboard.")
        token = self.headers.get("X-Dashboard-Token")
        if token is None and query is not None:
            token = (query.get("token") or [""])[0]
        if not token or not hmac.compare_digest(token, CSRF_TOKEN):
            raise Forbidden("Request refused: missing or wrong dashboard token. Reload the page.")

    def read_body(self):
        raw = self.headers.get("Content-Length")
        if raw is None:
            return b""
        if not raw.isdigit():
            raise ApiError("Invalid Content-Length.")
        length = int(raw)
        if length > MAX_BODY:
            raise ApiError("Request too large.", HTTPStatus.REQUEST_ENTITY_TOO_LARGE)
        return self.rfile.read(length)

    def read_json(self):
        body = self.read_body()
        try:
            data = json.loads(body or b"{}")
        except (ValueError, UnicodeDecodeError):
            raise ApiError("Request body is not valid JSON.")
        if not isinstance(data, dict):
            raise ApiError("Expected a JSON object.")
        return data

    # ---- methods ---------------------------------------------------

    def do_GET(self):
        if not self.host_ok():
            self.close_connection = True
            self.send_error_json(HTTPStatus.FORBIDDEN, "This dashboard only answers on localhost.")
            return

        url = urlparse(self.path)
        query = parse_qs(url.query, keep_blank_values=True)

        if url.path in STATIC_FILES:
            self.send_static(*STATIC_FILES[url.path])
            return

        if url.path == "/api/overview":
            def overview():
                since = parse_int(query, "since", 0)
                cleanup_sessions()
                new_events, last, reset = events_since(since)
                return {
                    "server_id": SERVER_ID,
                    "last_event_id": last,
                    "reset": reset,
                    "events": new_events,
                    "containers": list_containers(),
                    "images": list_images(),
                    "runtime_dir": CONFIG.runtime_dir,
                    "time": time.time(),
                }
            self.guard(overview)
            return

        m = ROUTE.match(url.path)
        if m and m.group(2) == "stats":
            self.guard(lambda: stats(m.group(1)))
        elif m and m.group(2) == "diff":
            self.guard(lambda: container_diff(m.group(1)))
        elif m and m.group(2) == "output":
            try:
                self.check_mutation(query)
                offset = parse_int(query, "from", 0)
                session = session_for(m.group(1))
            except ApiError as e:
                self.close_connection = True
                self.send_error_json(e.status, str(e))
                return
            self.stream_output(session, offset)
        else:
            self.send_error_json(HTTPStatus.NOT_FOUND, "Not found.")

    def do_POST(self):
        if not self.host_ok():
            self.close_connection = True
            self.send_error_json(HTTPStatus.FORBIDDEN, "This dashboard only answers on localhost.")
            return

        url = urlparse(self.path)
        m = ROUTE.match(url.path)

        def handle():
            self.check_mutation()
            if url.path == "/api/containers":
                return start_container(self.read_json())
            if not m:
                raise NotFound("Not found.")

            name, action = m.group(1), m.group(2)
            if action == "stop":
                self.read_body()
                return stop_container(name)
            if action == "delete":
                self.read_body()
                return delete_container(name)
            if action == "input":
                data = self.read_body()
                session_for(name).write(data)
                return {"ok": True}
            if action == "resize":
                size = self.read_json()
                try:
                    rows = max(5, min(200, int(size.get("rows", 24))))
                    cols = max(20, min(400, int(size.get("cols", 80))))
                except (TypeError, ValueError):
                    raise ApiError("rows and cols must be numbers.")
                session_for(name).resize(rows, cols)
                return {"ok": True}
            raise NotFound("Not found.")

        self.guard(handle)

    def method_not_allowed(self):
        self.close_connection = True
        self.send_response(HTTPStatus.METHOD_NOT_ALLOWED)
        body = json.dumps({"ok": False, "error": "Method not allowed."}).encode()
        self.send_header("Allow", "GET, POST")
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.common_headers()
        self.end_headers()
        self.wfile.write(body)

    do_PUT = do_DELETE = do_PATCH = do_OPTIONS = do_HEAD = method_not_allowed

    # ---- helpers ---------------------------------------------------

    def send_static(self, file, mime):
        with open(os.path.join(STATIC, file), "rb") as f:
            body = f.read()
        if file == "index.html":
            body = body.replace(b"{{CSRF_TOKEN}}", CSRF_TOKEN.encode())
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", mime)
        self.send_header("Content-Length", str(len(body)))
        self.common_headers()
        self.end_headers()
        self.wfile.write(body)

    def stream_output(self, session, offset):
        """Server-sent events with the container's terminal output."""
        self.close_connection = True
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/event-stream")
        self.common_headers()
        self.end_headers()
        try:
            while True:
                offset, data, running = session.read_from(offset, 15)
                if data:
                    chunk = base64.b64encode(data).decode()
                    self.wfile.write(f"id: {offset}\ndata: {chunk}\n\n".encode())
                else:
                    self.wfile.write(b": keep-alive\n\n")
                self.wfile.flush()
                if not running and not data:
                    self.wfile.write(f"event: end\ndata: {session.exit_code}\n\n".encode())
                    self.wfile.flush()
                    return
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass                       # the browser closed the terminal


def make_server(host="127.0.0.1", port=8080):
    CONFIG.port = port
    server = ThreadingHTTPServer((host, port), Handler)
    server.daemon_threads = True
    if port == 0:
        CONFIG.port = server.server_address[1]
    return server


def main():
    parser = argparse.ArgumentParser(description="mini-runtime web dashboard (local only)")
    parser.add_argument("--port", type=int, default=8080)
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")

    if os.geteuid() != 0:
        raise SystemExit("The dashboard starts containers, so it needs root:\n"
                         "  sudo python3 dashboard/server.py")
    if not os.access(CONFIG.runtime, os.X_OK):
        raise SystemExit(f"{CONFIG.runtime} not found. Run 'make' first.")

    server = make_server("127.0.0.1", args.port)

    def stop(signum, frame):
        threading.Thread(target=server.shutdown, daemon=True).start()
    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)

    def janitor():
        while True:
            time.sleep(60)
            cleanup_sessions()
    threading.Thread(target=janitor, daemon=True).start()

    print(f"mini-runtime dashboard: http://localhost:{args.port}  (local only)")
    print(f"Containers and images: {CONFIG.runtime_dir}")
    print("Press Ctrl-C to stop. Containers started from the dashboard are stopped too;")
    print("containers started from a terminal keep running.")
    server.serve_forever()
    server.server_close()
    shutdown_sessions()
    print("Dashboard stopped.")


if __name__ == "__main__":
    main()
