#!/bin/bash

set -e

echo "======================================"
echo " Mini Runtime - Environment Setup"
echo "======================================"

# ------------------------------------------------------------
# Runtime directory
# ------------------------------------------------------------
REAL_USER="${SUDO_USER:-${USER:-$(id -un)}}"

REAL_HOME=$(getent passwd "$REAL_USER" | cut -d: -f6)

if [ -z "$REAL_HOME" ]; then
    echo "ERROR: Could not determine home directory."
    exit 1
fi

RUNTIME_DIR="$REAL_HOME/.mini-runtime"
BASE="$RUNTIME_DIR/images/base"

echo "User:          $REAL_USER"
echo "Home:          $REAL_HOME"
echo "Runtime:       $RUNTIME_DIR"
echo "Base image:    $BASE"

# ------------------------------------------------------------
# Check required commands
# ------------------------------------------------------------
for command in g++ mount ldd; do
    if ! command -v "$command" >/dev/null 2>&1; then
        echo "ERROR: Required command '$command' was not found."
        echo "Install the required packages and try again."
        exit 1
    fi
done

# ------------------------------------------------------------
# Create runtime directory structure
# ------------------------------------------------------------
echo
echo "[1/5] Creating runtime directories..."

mkdir -p "$BASE"/bin
mkdir -p "$BASE"/lib
mkdir -p "$BASE"/lib64
mkdir -p "$BASE"/proc
mkdir -p "$BASE"/tmp
mkdir -p "$BASE"/etc
mkdir -p "$BASE"/usr
mkdir -p "$BASE"/dev
mkdir -p "$BASE"/sys
mkdir -p "$BASE"/home
mkdir -p "$BASE"/root

mkdir -p "$RUNTIME_DIR/containers"

# ------------------------------------------------------------
# Copy shell and basic utilities with their shared libraries
# ------------------------------------------------------------
echo
echo "[2/5] Building minimal base filesystem..."

# ps and ls make the PID and filesystem isolation demos readable.
TOOLS=""
for name in sh cat hostname ls ps mkdir rm sleep touch id uname; do
    path=$(command -v "$name" || true)
    if [ -n "$path" ] && [ -x "$path" ]; then
        TOOLS="$TOOLS $path"
    else
        echo "Warning: '$name' not found on the host, skipping it."
    fi
done

for cmd in $TOOLS; do
    sudo cp -L --parents "$cmd" "$BASE"
done

echo "Copying required shared libraries..."

for cmd in $TOOLS; do
    ldd "$cmd" |
    awk '/=> \// {print $3} $1 ~ /^\// {print $1}' |
    sort -u |
    while read -r lib; do
        if [ -f "$lib" ]; then
            sudo cp -L --parents "$lib" "$BASE"
        fi
    done
done

# /bin/sh must exist because it is the default container command.
if [ ! -x "$BASE/bin/sh" ]; then
    sudo mkdir -p "$BASE/bin"
    sudo cp -L /bin/sh "$BASE/bin/sh"
fi

# ------------------------------------------------------------
# Create demonstration file
# ------------------------------------------------------------
echo
echo "[3/5] Creating OverlayFS demonstration file..."

echo "BASE FILE" > "$BASE/message.txt"

# ------------------------------------------------------------
# Build memory hog test
# ------------------------------------------------------------
echo
echo "[4/5] Building memory limit test..."

g++ -std=c++17 -static \
    tests/memory_hog.cpp \
    -o /tmp/memory_hog

sudo cp /tmp/memory_hog "$BASE/bin/memory_hog"

rm -f /tmp/memory_hog

# ------------------------------------------------------------
# Fix ownership
# ------------------------------------------------------------
echo
echo "[5/5] Setting ownership..."

sudo chown -R "$REAL_USER:$REAL_USER" "$RUNTIME_DIR"

# ------------------------------------------------------------
# Finish
# ------------------------------------------------------------
echo
echo "======================================"
echo " Setup completed successfully!"
echo "======================================"
echo
echo "Runtime directory:"
echo "  $RUNTIME_DIR"
echo
echo "Base filesystem:"
echo "  $BASE"
echo
for command in ip nsenter; do
    if ! command -v "$command" >/dev/null 2>&1; then
        echo "Note: '$command' is missing; 'run --net' will not work."
    fi
done

echo
echo "Next:"
echo "  make"
echo "  sudo ./mini-runtime run demo"
echo "  sudo make test"
echo
