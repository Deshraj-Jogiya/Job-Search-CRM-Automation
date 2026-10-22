#!/bin/bash
# Restarts career-pilot.service if it stops responding to its own health
# check. systemd's Restart=on-failure (see career-pilot.service) only
# catches a process that actually exits/crashes -- it does nothing for a
# process that's still alive but hung (deadlocked thread, exhausted DB
# connection pool, etc.), which is exactly the failure mode this covers.
# Run periodically via career-pilot-watchdog.timer, not directly.
#
# Arguments (both optional): the local port to check and the unit to
# restart. Defaults are the personal instance; the public demo's timer
# passes its own (career-pilot-demo-watchdog.service).
set -euo pipefail

PORT="${1:-8000}"
UNIT="${2:-career-pilot.service}"

if ! curl -fsS --max-time 10 "http://127.0.0.1:${PORT}/api/health" > /dev/null; then
    logger -t career-pilot-watchdog "Health check failed on port ${PORT} -- restarting ${UNIT}"
    systemctl restart "${UNIT}"
fi
