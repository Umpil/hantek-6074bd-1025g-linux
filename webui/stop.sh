#!/bin/sh
# Stop the web UI; the USB interfaces are released when the process exits.
cd "$(dirname "$0")" || exit 1
if [ -f web.pid ] && kill "$(cat web.pid)" 2>/dev/null; then echo "stopped"; else echo "not running"; fi
rm -f web.pid
