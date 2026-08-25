#!/usr/bin/env bash
# Change-alert delivery (#23): runs on the HOST after the sweep. Pulls the
# alert lines out of the engine container and, if there are any, generates a
# Nextcloud notification for the user via occ — the engine itself holds no
# Nextcloud admin credential. Exit 0 always: a failed notification must not
# fail the sweep unit.
#
# Deliver, THEN advance the cursor (issue #47). `alerts --defer` parks the
# new cursor value; only a successful occ run acks it. Before that the cursor
# moved at collection time, so a failed occ — which this script swallows on
# purpose — dropped the day's alerts with no trace. A repeated notification
# is recoverable; a missing one is not.
#
# TRUNCATE, and treat a truncated delivery as a delivery (issue #61). occ
# refuses a long-message over 4000 BYTES and exits 1, so the deliver-then-ack
# rule above turns one oversized night into a permanently wedged channel: the
# un-acked backlog comes back larger the next night and fails again. The
# notification is a nudge — the Journal and the weekly digest are the record —
# so a clipped message that arrives beats a whole one that never does.
set -u
NC_USER="${PC_ALERT_NC_USER:-admin}"
# occ's own limit is 4000; the slack absorbs the tail line and leaves room to
# spare. Byte budget, not characters — alert lines carry umlauts and em dashes.
BUDGET=3800
TAIL_RESERVE=64

lines="$(docker exec china-calendar-mcp pcal alerts --defer 2>/dev/null)" || exit 0
if [ -z "$lines" ]; then
    docker exec china-calendar-mcp pcal alerts-ack >/dev/null 2>&1 || true
    exit 0
fi

count="$(printf '%s\n' "$lines" | wc -l)"
short="china-calendar: ${count} change(s) — see long message"
# Whole lines only, and stop at the first that does not fit rather than
# skipping it — half an alert, or alerts out of order, is worse than a count.
# LC_ALL=C makes awk's length() count bytes, which is what occ measures.
body="$(printf '%s\n' "$lines" | LC_ALL=C awk -v budget="$((BUDGET - TAIL_RESERVE))" '
    !stop && used + length($0) + 1 <= budget { used += length($0) + 1; print; next }
    { stop = 1; dropped++ }
    END { if (dropped) printf "… +%d more — see the Journal\n", dropped }
')"
if out="$(docker exec -u www-data nextcloud-app-1 php occ notification:generate \
        "$NC_USER" "$short" --long-message "$body" 2>&1)"; then
    docker exec china-calendar-mcp pcal alerts-ack >/dev/null 2>&1 || true
else
    # Never fail the unit, but never fail silently either: an undelivered
    # notification looked exactly like a quiet day until #61 was found.
    echo "pcal-alerts: occ notification:generate failed (${out}); cursor left in place, ${count} alert(s) repeat next sweep" >&2
fi
exit 0
