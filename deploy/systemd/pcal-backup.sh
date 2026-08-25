#!/usr/bin/env bash
# Daily cold copy of the event store to the Nextcloud mount (issue #54).
# Runs on the HOST: the store is a host-side bind mount, and the engine
# container has no business holding a path outside it. Newest 5 kept.
#
# The store's own writers are atomic (tmp + rename per file), and this runs
# an hour before the 07:30 sweep — so the copy is both internally consistent
# and a pre-sweep snapshot of each day.
set -eu
SRC="$HOME/china-calendar/store"
DEST="/mnt/nextcloud/backups"
OUT="$DEST/pcal-store-$(date +%Y%m%d).tgz"

# tmp + mv so a half-written tarball never sits under the final name —
# these files are the restore path, a truncated one is worse than none.
tar -czf "$OUT.tmp" -C "$(dirname "$SRC")" "$(basename "$SRC")"
tar -tzf "$OUT.tmp" >/dev/null
mv "$OUT.tmp" "$OUT"

ls -1t "$DEST"/pcal-store-*.tgz | tail -n +6 | xargs -r rm --
