#!/bin/sh
set -eu

# Named Docker volumes are initially root-owned. Hand off only the writable
# mount points; media/static files are then created by the unprivileged app user.
for writable_dir in /app/data/media /app/staticfiles; do
    mkdir -p "$writable_dir"
    chown app:app "$writable_dir"
done

exec gosu app "$@"
