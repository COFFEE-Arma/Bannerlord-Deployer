#!/bin/bash
set -e

SERVER_DIR=/data/server
EXE_NAME=BannerlordCoopServer.exe
DATA_ROOT=/data/server-data

cd "$SERVER_DIR" 2>/dev/null || {
    echo "[entrypoint] $SERVER_DIR does not exist yet."
    echo "[entrypoint] Copy the Console Server files into ./data/server on the host, then restart."
    sleep 60
    exit 1
}

if [ ! -f "$EXE_NAME" ]; then
    echo "[entrypoint] $EXE_NAME not found in $SERVER_DIR."
    echo "[entrypoint] Copy the Console Server files into ./data/server on the host, then restart."
    sleep 60
    exit 1
fi

mkdir -p "$WINEPREFIX" "$DATA_ROOT"

# Silence wine's "XDG_RUNTIME_DIR is invalid or not set" warnings
export XDG_RUNTIME_DIR=/tmp/xdg-runtime
mkdir -p "$XDG_RUNTIME_DIR"
chmod 700 "$XDG_RUNTIME_DIR"

# The dedicated server writes its persistent data (campaign saves under
# CoopData/DedicatedServer/"Game Saves", config backups, ...) to
# Documents\Mount and Blade II Bannerlord inside the wine prefix. Symlink that
# folder to /data/server-data so the data lives in one visible place on the
# volume and survives a wineprefix rebuild, regardless of which path contract
# the current server build uses.
DOCS_DIR="$WINEPREFIX/drive_c/users/root/Documents"
BLD_DIR="$DOCS_DIR/Mount and Blade II Bannerlord"
mkdir -p "$DOCS_DIR"
if [ -e "$BLD_DIR" ] && [ ! -L "$BLD_DIR" ]; then
    echo "[entrypoint] Migrating existing data from wineprefix Documents to $DATA_ROOT (no overwrite)"
    cp -an "$BLD_DIR/." "$DATA_ROOT/"
    rm -rf "$BLD_DIR"
fi
if [ ! -e "$BLD_DIR" ]; then
    ln -s "$DATA_ROOT" "$BLD_DIR"
fi

echo "[entrypoint] Starting $EXE_NAME under wine (USE_XVFB=${USE_XVFB:-0})"
if [ "${USE_XVFB:-0}" = "1" ]; then
    exec xvfb-run -a wine "$EXE_NAME"
fi
exec wine "$EXE_NAME"
