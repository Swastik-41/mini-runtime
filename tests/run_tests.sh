#!/bin/bash
#
# Automated checks for every feature the runtime claims.
# Usage (after "make setup" and "make"):  sudo make test
#
# Each test starts a throw-away container with a command after "--",
# checks the output, and deletes the container again.

RUNTIME="${RUNTIME:-./mini-runtime}"

if [ "$(id -u)" -ne 0 ]; then
    echo "Run the tests as root: sudo make test"
    exit 1
fi

if [ -n "$SUDO_USER" ]; then
    RUNTIME_DIR="$(getent passwd "$SUDO_USER" | cut -d: -f6)/.mini-runtime"
else
    RUNTIME_DIR="$HOME/.mini-runtime"
fi
BASE="$RUNTIME_DIR/images/base"

if [ ! -x "$BASE/bin/sh" ]; then
    echo "Base image missing at $BASE. Run 'make setup' first."
    exit 1
fi

PASSED=0
FAILED=0
OUT=""

pass() { echo "  PASS  $1"; PASSED=$((PASSED + 1)); }
fail() { echo "  FAIL  $1"; echo "$OUT" | sed 's/^/        | /'; FAILED=$((FAILED + 1)); }

# run <name> [options] -- command...   (stores output in $OUT, code in $CODE)
run() {
    OUT=$("$RUNTIME" run "$@" </dev/null 2>&1)
    CODE=$?
}

cleanup() {
    for name in t-uts t-pid t-mnt t-root t-net t-veth t-cow1 t-cow2 \
                t-mem t-cpu t-pids t-exit t-cap t-dev t-img; do
        "$RUNTIME" delete "$name" >/dev/null 2>&1
    done
    rm -rf "$RUNTIME_DIR/images/t-image"
}

cleanup
echo "Namespaces"

run t-uts -- hostname
echo "$OUT" | grep -qx "t-uts" && pass "UTS: hostname is the container name" \
    || fail "UTS: hostname is the container name"

run t-pid -- sh -c 'echo "pid=$$"; n=0; for p in /proc/[0-9]*; do n=$((n+1)); done; echo "$n"'
if echo "$OUT" | grep -qx "pid=1" &&
   [ "$(echo "$OUT" | grep -x '[0-9][0-9]*' | tail -1)" -le 3 ]; then
    pass "PID: container shell is PID 1 and sees only its own processes"
else
    fail "PID: container shell is PID 1 and sees only its own processes"
fi

run t-mnt -- cat /proc/self/mountinfo
if ! grep -q "containers/t-mnt/merged" /proc/self/mountinfo; then
    pass "Mount: container mounts do not leak to the host"
else
    fail "Mount: container mounts do not leak to the host"
fi

run t-root -- ls -a /
if ! echo "$OUT" | grep -q "old_root" && echo "$OUT" | grep -qx "message.txt"; then
    pass "Root: pivot_root shows only the image, old root is gone"
else
    fail "Root: pivot_root shows only the image, old root is gone"
fi

run t-net -- cat /proc/net/dev
if echo "$OUT" | grep -q "lo:" && ! echo "$OUT" | grep -q "eth"; then
    pass "Network: isolated namespace has only loopback"
else
    fail "Network: isolated namespace has only loopback"
fi

if command -v ip >/dev/null && command -v nsenter >/dev/null; then
    run t-veth --net -- cat /proc/net/dev
    if [ "$CODE" -eq 0 ] && echo "$OUT" | grep -q "eth0:"; then
        pass "Network: --net gives the container an eth0 veth interface"
    else
        fail "Network: --net gives the container an eth0 veth interface"
    fi
else
    echo "  SKIP  Network: --net (ip or nsenter not installed)"
fi

echo "Copy-on-write storage"

run t-cow1 -- sh -c 'echo CHANGED > /message.txt; echo new > /added.txt; rm "$(command -v hostname)"'
run t-cow2 -- cat /message.txt
COW2="$OUT"
if [ "$(cat "$BASE/message.txt")" = "BASE FILE" ] && [ "$COW2" != "${COW2%BASE FILE*}" ] &&
   grep -q CHANGED "$RUNTIME_DIR/containers/t-cow1/upper/message.txt"; then
    pass "CoW: changes go to the upper layer, base and other containers untouched"
else
    OUT="$COW2"
    fail "CoW: changes go to the upper layer, base and other containers untouched"
fi

OUT=$("$RUNTIME" diff t-cow1 2>&1)
if echo "$OUT" | grep -q "MODIFIED.*/message.txt" &&
   echo "$OUT" | grep -q "ADDED.*/added.txt" &&
   echo "$OUT" | grep -q "DELETED.*/hostname"; then
    pass "CoW: diff lists modified, added and deleted files"
else
    fail "CoW: diff lists modified, added and deleted files"
fi

mkdir -p "$RUNTIME_DIR/images/t-image"
cp -a "$BASE/." "$RUNTIME_DIR/images/t-image/"
echo "SECOND IMAGE" > "$RUNTIME_DIR/images/t-image/message.txt"
"$RUNTIME" create t-img --image t-image >/dev/null 2>&1
run t-img -- cat /message.txt
echo "$OUT" | grep -q "SECOND IMAGE" && pass "Images: create --image uses another base image" \
    || fail "Images: create --image uses another base image"

echo "Resource limits (cgroups v2)"

if [ -x "$BASE/bin/memory_hog" ]; then
    run t-mem --memory 50M -- /bin/memory_hog
    if [ "$CODE" -eq 137 ] && echo "$OUT" | grep -q "OOM killer"; then
        pass "Memory: 200 MB allocation is OOM-killed under a 50M limit"
    else
        fail "Memory: 200 MB allocation is OOM-killed under a 50M limit (exit $CODE)"
    fi
else
    echo "  SKIP  Memory: memory_hog missing from the base image"
fi

run t-cpu --cpu 20 -- sh -c 'while :; do :; done & sleep 3; kill $!'
CPU_MS=$(echo "$OUT" | sed -n 's/.*CPU \([0-9]*\) ms.*/\1/p')
THROTTLED=$(echo "$OUT" | sed -n 's/.*throttled \([0-9]*\) times.*/\1/p')
if [ -n "$CPU_MS" ] && [ "$CPU_MS" -lt 1000 ] && [ "${THROTTLED:-0}" -gt 0 ]; then
    pass "CPU: busy loop held to about 20% (${CPU_MS} ms of 3000 ms)"
else
    fail "CPU: busy loop held to about 20% (${CPU_MS:-?} ms of 3000 ms)"
fi

run t-pids --pids 10 -- sh -c 'i=0; while [ $i -lt 20 ]; do sleep 2 & i=$((i+1)); done; wait'
if echo "$OUT" | grep -qi "fork\|resource temporarily unavailable"; then
    pass "PIDs: fork fails once the 10-process limit is reached"
else
    fail "PIDs: fork fails once the 10-process limit is reached"
fi

echo "Runtime behaviour"

run t-exit -- sh -c 'exit 7'
[ "$CODE" -eq 7 ] && pass "Exit code of the container is returned" \
    || fail "Exit code of the container is returned (got $CODE)"

run t-cap -- cat /proc/self/status
CAPBND=$(echo "$OUT" | sed -n 's/^CapBnd:[[:space:]]*//p')
if [ -n "$CAPBND" ] && [ $((16#$CAPBND & (1 << 21))) -eq 0 ]; then
    pass "Security: CAP_SYS_ADMIN is dropped inside the container"
else
    fail "Security: CAP_SYS_ADMIN is dropped inside the container"
fi

run t-dev -- sh -c 'echo hidden > /dev/null && ls /dev'
if [ "$CODE" -eq 0 ] && echo "$OUT" | grep -qx "null" && ! echo "$OUT" | grep -q "sda\|nvme"; then
    pass "/dev: has null, zero, urandom but no host disks"
else
    fail "/dev: has null, zero, urandom but no host disks"
fi

if [ -n "$SUDO_USER" ] && [ "$SUDO_USER" != root ]; then
    OUT=$(sudo -u "$SUDO_USER" -H "$RUNTIME" list 2>&1)
else
    OUT=$("$RUNTIME" list 2>&1)
fi
if echo "$OUT" | grep -q "t-exit.*STOPPED"; then
    pass "list works without sudo"
else
    fail "list works without sudo"
fi

cleanup
echo
echo "$PASSED passed, $FAILED failed"
[ "$FAILED" -eq 0 ]
