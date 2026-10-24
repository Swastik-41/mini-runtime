# Lightweight Container Runtime with Copy-on-Write Storage

A minimal Linux container runtime implemented from scratch in C++17.

This project demonstrates the core operating-system mechanisms behind lightweight containers without using Docker, containerd, or Kubernetes.

## Requirements

- Linux with the unified **cgroup v2** hierarchy (default on Ubuntu 22.04+, Debian 11+, Fedora 31+)
- OverlayFS support in the kernel
- `g++` (C++17), `ldd`, `mount`
- Optional, for `--net`: `ip` (iproute2) and `nsenter` (util-linux)

## Quick start

```bash
make setup            # build the base image in ~/.mini-runtime/images/base
make                  # build ./mini-runtime
sudo ./mini-runtime run demo
sudo make test        # automated checks for every feature below
sudo make benchmark   # CoW vs full-copy startup time and disk usage
```

## Web dashboard

```bash
sudo make dashboard          # or: sudo python3 dashboard/server.py [--port 8080]
```

Open http://localhost:8080 (on WSL2, open it in your Windows browser). It needs only Python 3, with no packages to install. The dashboard runs the same `./mini-runtime` binary and shows:

- a form to run containers with memory, CPU, PID, image, network and command settings, plus one-click demos (memory OOM, CPU cap, fork limit, copy-on-write)
- a container table with search, filters and sorting; each container's status (Running, Stopped or Unknown, read from the runtime's lock file), where its terminal is, and its limits when they are known
- **Start** to run a stopped container again with the settings it last ran with here, and **Edit and run** to change them first
- live graphs of `memory.current`, CPU (as % of one CPU core) and `pids.current` against their cgroup limits, with OOM kill and throttle counts. Missing counters show as "Unavailable", never as 0, and a sample older than 5 seconds is marked STALE. The graphs hold live samples only (the last 60 seconds) and are not saved.
- the container's shell in the browser (xterm.js, bundled under `dashboard/static/vendor`), with clear, copy and fullscreen
- the copy-on-write changes (added, modified, deleted) and the private layer's size (apparent and on disk) against the shared image
- an event log of starts, stops, OOM kills and exit codes

Things to know:

- **Images:** the dashboard only accepts image names from `~/.mini-runtime/images/`. Paths, `..` and symlinks that point outside that folder are refused. The command line still accepts `--image /any/path`.
- **Terminals:** a container started from a terminal window shows "Terminal elsewhere". Its usage and Stop work, but the runtime can't re-attach its shell to the browser. The same applies to containers that were running when the dashboard restarted.
- **Stopping the dashboard** (Ctrl-C) stops the containers it started. Containers started from a terminal keep running.
- **Security:** it listens on 127.0.0.1 only, answers only `localhost`, `127.0.0.1` and `[::1]` Host headers, and every action needs the random token in the page it served. It runs as root and is a local development tool, not hardened for remote or multi-user access.

Dashboard tests (no root needed; they use a fake runtime and fake cgroup files):

```bash
python3 -m unittest discover -s dashboard/tests -v               # server and API
NODE_PATH=$(npm root -g) node dashboard/tests/ui_test.js         # browser, needs Playwright
```

## Usage

```text
sudo ./mini-runtime create <name> [--image <name-or-dir>]
sudo ./mini-runtime run <name> [--image <name-or-dir>] [--memory 100M]
                               [--cpu 50] [--pids 64] [--net] [-- command args...]
./mini-runtime list
./mini-runtime diff <name>
sudo ./mini-runtime delete <name>
```

- `run` creates the container if it does not exist yet, then starts `/bin/sh`, or the command given after `--`.
- `--image` takes an image name under `~/.mini-runtime/images/` or a path to any root filesystem directory. A container keeps the image it was created with.
- `--memory` needs a K, M or G suffix (4M to 1024G). Swap is always disabled.
- `--net` connects the container to the host with a veth pair (`10.200.N.2` inside, `10.200.N.1` on the host).
- `diff` shows what the container added, modified or deleted compared with its image, read straight from the OverlayFS upper layer.
- The runtime exits with the container's exit code (128 + signal if it was killed, for example 137 after an OOM kill).

## Features

1. **Linux namespaces**
   - PID, mount, UTS, IPC and network namespaces via `clone()`
   - cgroup namespace via `unshare()` once the process is in its cgroup
   - Mount propagation set to private, so nothing leaks to the host
   - Hostname set to the container name
   - Loopback brought up; optional veth networking with `--net`

2. **cgroups v2**
   - Memory limit (default 100 MB), swap disabled
   - CPU limit (default 50%)
   - PID limit (default 64, fork bomb protection)
   - Kernel-enforced OOM termination, reported after exit with CPU time, throttling and peak memory

3. **OverlayFS Copy-on-Write**
   - Shared read-only base image, multiple images supported
   - Private writable upper layer and work directory per container
   - Merged view used as the container root via `pivot_root`

4. **Hardening**
   - `pivot_root` with the old root detached (no chroot escape)
   - Fresh `/proc`, read-only `/sys`, and a minimal `/dev` (null, zero, full, random, urandom, tty)
   - Capability bounding set reduced to Docker's default set without `CAP_MKNOD`; `no_new_privs` set

---

## Architecture

```text
 run <name>
     |
     v
 Prepare container dirs (upper, work, merged) for the chosen image
     |
     v
 clone(): new PID, mount, UTS, IPC and network namespaces
     |          (child waits on a pipe)
     v
 Parent: create cgroup, write memory/cpu/pids limits, add child   [--net: veth]
     |
     v
 Child: unshare cgroup ns, make mounts private, mount OverlayFS
     |
     v
 pivot_root into merged, mount /proc /sys /dev, set hostname, drop capabilities
     |
     v
 exec the command; parent waits, reports usage, removes cgroup


                 Container root
                      |
                    merged
                      |
          +-----------+-----------+
          |                       |
       upper                    lower
     writable                 read-only
          |                       |
    Container changes          Base image
```
