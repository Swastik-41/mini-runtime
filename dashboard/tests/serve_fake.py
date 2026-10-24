#!/usr/bin/env python3
"""
Runs the dashboard against fake_runtime.py and a fake cgroup tree, without
root, for the browser test (ui_test.js). Counters in the fake cgroups are
bumped every 0.5 s so the charts have something to draw.

    python3 dashboard/tests/serve_fake.py <port> <scratch-dir>
"""

import os
import signal
import sys
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

import server  # noqa: E402


def feeder(cg_root):
    step = 0
    while True:
        time.sleep(0.5)
        step += 1
        for entry in os.listdir(cg_root):
            cg = os.path.join(cg_root, entry)
            try:
                with open(os.path.join(cg, "cpu.stat")) as f:
                    usage = int(f.readline().split()[1])
                with open(os.path.join(cg, "cpu.stat"), "w") as f:
                    f.write(f"usage_usec {usage + 150000}\nnr_throttled {step}\n")
                with open(os.path.join(cg, "memory.current"), "w") as f:
                    f.write(str((8 + step % 20) * 1024 * 1024))
            except (OSError, ValueError, IndexError):
                pass


def main():
    port, scratch = int(sys.argv[1]), sys.argv[2]
    rt, cg = os.path.join(scratch, "rt"), os.path.join(scratch, "cgroup")
    for d in (os.path.join(rt, "images", "base", "bin"), os.path.join(rt, "images", "second"),
              os.path.join(rt, "containers"), cg):
        os.makedirs(d, exist_ok=True)
    with open(os.path.join(rt, "images", "base", "bin", "sh"), "w") as f:
        f.write("x" * 4096)

    os.environ["FAKE_RT_DIR"], os.environ["FAKE_CG_ROOT"] = rt, cg
    server.CONFIG.runtime = os.path.join(HERE, "fake_runtime.py")
    server.CONFIG.runtime_dir = rt
    server.CONFIG.cgroup_root = cg

    httpd = server.make_server("127.0.0.1", port)
    signal.signal(signal.SIGTERM, lambda *_: threading.Thread(target=httpd.shutdown).start())
    threading.Thread(target=feeder, args=(cg,), daemon=True).start()
    print(f"ready on {port}", flush=True)
    httpd.serve_forever()
    httpd.server_close()
    server.shutdown_sessions()


if __name__ == "__main__":
    main()
