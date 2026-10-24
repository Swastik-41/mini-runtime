#!/usr/bin/env python3
"""
Stand-in for ./mini-runtime used by test_server.py, so the dashboard can be
tested without root, namespaces or cgroup v2.

It keeps the same on-disk contract as the real runtime: a folder per
container, an "image" file, a flock'ed lock file while running, and a cgroup
folder (here a plain folder under FAKE_CG_ROOT) holding the container's pid.

FAKE_RT_DIR and FAKE_CG_ROOT come from the environment.
"""

import fcntl
import os
import shutil
import signal
import subprocess
import sys

RT = os.environ["FAKE_RT_DIR"]
CG = os.environ["FAKE_CG_ROOT"]

SHELL = [sys.executable, "-c",
         "import sys\n"
         "print('fake-shell ready', flush=True)\n"
         "for line in sys.stdin:\n"
         "    if line.strip() == 'exit': break\n"
         "    print('got:' + line.strip(), flush=True)\n"]


def write(path, text):
    with open(path, "w") as f:
        f.write(text)


def run(args):
    name, args = args[0], args[1:]
    opts = {"memory": "100M", "cpu": "50", "pids": "64", "image": os.path.join(RT, "images", "base")}
    command = None
    while args:
        a = args.pop(0)
        if a == "--":
            command = args
            break
        if a == "--net":
            continue
        opts[a[2:]] = args.pop(0)

    cdir = os.path.join(RT, "containers", name)
    os.makedirs(os.path.join(cdir, "upper"), exist_ok=True)
    if not os.path.exists(os.path.join(cdir, "image")):
        write(os.path.join(cdir, "image"), opts["image"])

    os.makedirs(os.path.join(RT, "locks"), exist_ok=True)
    lock = os.open(os.path.join(RT, "locks", name + ".lock"), os.O_RDWR | os.O_CREAT)
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        print(f"Error: container '{name}' is already running")
        return 1

    cg = os.path.join(CG, "mini-runtime-" + name)
    os.makedirs(cg, exist_ok=True)
    mem = int(opts["memory"][:-1]) * {"K": 1 << 10, "M": 1 << 20, "G": 1 << 30}[opts["memory"][-1].upper()]
    write(os.path.join(cg, "memory.max"), str(mem))
    write(os.path.join(cg, "memory.current"), "1048576")
    write(os.path.join(cg, "memory.events"), "oom 0\noom_kill 0\n")
    write(os.path.join(cg, "cpu.max"), f"{int(opts['cpu']) * 1000} 100000")
    write(os.path.join(cg, "cpu.stat"), "usage_usec 1000\nnr_throttled 0\n")
    write(os.path.join(cg, "pids.max"), opts["pids"])
    write(os.path.join(cg, "pids.current"), "1")

    child = subprocess.Popen(command or SHELL)
    write(os.path.join(cg, "cgroup.procs"), str(child.pid))
    code = child.wait()
    shutil.rmtree(cg, ignore_errors=True)
    print(f"[fake-runtime] exited {code}", flush=True)
    return 128 - code if code < 0 else code


def diff(args):
    name = args[0]
    if not os.path.isdir(os.path.join(RT, "containers", name)):
        print("Error: no such container")
        return 1
    if name.startswith("broken"):
        print("internal detail: /secret/path exploded")
        return 3
    print("[ADDED]    /hello.txt")
    print("[MODIFIED] /etc/hostname")
    print("[DELETED]  /<b>bold</b>.txt")
    return 0


def delete(args):
    name = args[0]
    if name.startswith("undeletable"):
        print("internal detail: device busy")
        return 1
    shutil.rmtree(os.path.join(RT, "containers", name), ignore_errors=True)
    return 0


def main():
    signal.signal(signal.SIGHUP, signal.SIG_DFL)
    cmd, args = sys.argv[1], sys.argv[2:]
    return {"run": run, "diff": diff, "delete": delete}[cmd](args)


if __name__ == "__main__":
    sys.exit(main())
