#!/usr/bin/env bash
# Run a Next Move admin command as the service's own data identity, so SQLite
# files and resume directories keep their owner and 0700/0600 modes. Install as
# /usr/local/sbin/next-move-admin. Examples:
#   next-move-admin invite create --count 1 --expires-days 7
#   next-move-admin invite list
#   next-move-admin user list
#   next-move-admin user reset-password you@example.com
set -euo pipefail
exec systemd-run --quiet --wait --pipe --collect \
  -p User=next-move -p DynamicUser=yes \
  -p StateDirectory=next-move -p StateDirectoryMode=0700 -p UMask=0077 \
  -p EnvironmentFile=/etc/next-move/production.env \
  -p WorkingDirectory=/opt/next-move/current \
  /opt/next-move/current/.venv/bin/flask --app jobtracker "$@"
