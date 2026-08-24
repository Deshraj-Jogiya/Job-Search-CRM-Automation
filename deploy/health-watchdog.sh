#!/bin/bash
# Restarts career-pilot.service if it stops responding to its own health
# check. systemd's Restart=on-failure (see career-pilot.service) only
# catches a process that actually exits/crashes -- it does nothing for a
# process that's still alive but hung (deadlocked thread, exhausted DB
# connection pool, etc.), which is exactly the failure mode this covers.
# Run periodically via career-pilot-watchdog.timer, not directly.
set -euo pipefail

if ! curl -fsS --max-time 10 http://127.0.0.1:8000/api/health > /dev/null; then
    logger -t career-pilot-watchdog "Health check failed -- restarting career-pilot.service"
    systemctl restart career-pilot.service
fi
