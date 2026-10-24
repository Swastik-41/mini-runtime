#!/bin/bash
#
# Measures what OverlayFS copy-on-write saves compared with giving every
# container a full copy of the base image.
# Usage (after "make setup" and "make"):  sudo make benchmark [COUNT=10]

RUNTIME="${RUNTIME:-./mini-runtime}"
COUNT="${COUNT:-10}"

if [ "$(id -u)" -ne 0 ]; then
    echo "Run the benchmark as root: sudo make benchmark"
    exit 1
fi

if [ -n "$SUDO_USER" ]; then
    RUNTIME_DIR="$(getent passwd "$SUDO_USER" | cut -d: -f6)/.mini-runtime"
else
    RUNTIME_DIR="$HOME/.mini-runtime"
fi
BASE="$RUNTIME_DIR/images/base"
COPIES="$RUNTIME_DIR/bench-copies"

# bash's own microsecond clock; "date +%s%3N" is not portable.
now_ms() { local t=${EPOCHREALTIME//[.,]/}; echo $((10#$t / 1000)); }

echo "Starting $COUNT containers that each run 'true'..."
start=$(now_ms)
for i in $(seq "$COUNT"); do
    "$RUNTIME" run "bench-$i" -- true >/dev/null 2>&1 </dev/null
done
cow_ms=$(( $(now_ms) - start ))

echo "Making $COUNT full copies of the base image..."
mkdir -p "$COPIES"
start=$(now_ms)
for i in $(seq "$COUNT"); do
    cp -a "$BASE" "$COPIES/copy-$i"
done
copy_ms=$(( $(now_ms) - start ))

base_kb=$(du -sk "$BASE" | cut -f1)
cow_kb=0
for i in $(seq "$COUNT"); do
    kb=$(du -sk "$RUNTIME_DIR/containers/bench-$i" | cut -f1)
    cow_kb=$((cow_kb + kb))
done
copy_kb=$(du -sk "$COPIES" | cut -f1)

echo
printf "%-28s %12s %12s\n" "" "CoW (overlay)" "Full copy"
printf "%-28s %10s ms %10s ms\n" "Total time for $COUNT" "$cow_ms" "$copy_ms"
printf "%-28s %10s ms %10s ms\n" "Per container" "$((cow_ms / COUNT))" "$((copy_ms / COUNT))"
printf "%-28s %10s KB %10s KB\n" "Extra disk for $COUNT" "$cow_kb" "$copy_kb"
echo
echo "Base image size: ${base_kb} KB (shared once by all CoW containers)."
echo "CoW time includes creating namespaces, cgroups and running the command."

for i in $(seq "$COUNT"); do
    "$RUNTIME" delete "bench-$i" >/dev/null 2>&1
done
rm -rf "$COPIES"
