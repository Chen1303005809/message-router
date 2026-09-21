#!/usr/bin/env bash
set -euo pipefail

APP_DIR=/opt/kefu/repo
CURRENT_LINK=/opt/kefu/current
VENV=/opt/kefu/venv
BRANCH=main

if [[ "$(id -u)" -ne 0 ]]; then
  echo "Run this script as root on the remote server (ssh bubble)." >&2
  exit 1
fi

if [[ ! -d "$APP_DIR/.git" ]]; then
  echo "Git checkout not found at $APP_DIR; follow the one-time setup in REMOTE_DEPLOYMENT.md." >&2
  exit 1
fi

if [[ "$(readlink -f "$CURRENT_LINK")" != "$APP_DIR" ]]; then
  echo "$CURRENT_LINK must point to $APP_DIR before running updates." >&2
  exit 1
fi

if [[ -n "$(git -C "$APP_DIR" status --porcelain)" ]]; then
  echo "Refusing to update a Git checkout with local changes: $APP_DIR" >&2
  exit 1
fi

systemctl stop kefu-web.service kefu-worker.service

if ! git -C "$APP_DIR" pull --ff-only origin "$BRANCH"; then
  systemctl start kefu-web.service kefu-worker.service || true
  exit 1
fi
"$VENV/bin/python" -m pip install "$APP_DIR"

install -o root -g root -m 0644 "$APP_DIR"/deploy/systemd/*.service /etc/systemd/system/
systemctl daemon-reload

if ! systemctl start kefu-migrate.service; then
  systemctl show kefu-migrate.service -p Result -p ExecMainStatus >&2
  exit 1
fi

migration_result="$(systemctl show kefu-migrate.service -p Result --value)"
migration_status="$(systemctl show kefu-migrate.service -p ExecMainStatus --value)"
if [[ "$migration_result" != "success" || "$migration_status" != "0" ]]; then
  printf 'Migration failed: Result=%s ExecMainStatus=%s\n' "$migration_result" "$migration_status" >&2
  exit 1
fi

systemctl start kefu-web.service kefu-worker.service

for _ in {1..20}; do
  if systemctl is-active --quiet kefu-web.service kefu-worker.service \
    && curl -fsS -o /dev/null http://127.0.0.1:18000/healthz; then
    printf 'Updated to %s; web and worker are active.\n' "$(git -C "$APP_DIR" rev-parse --short HEAD)"
    exit 0
  fi
  sleep 1
done

systemctl is-active kefu-web.service kefu-worker.service || true
echo "Services did not become healthy; inspect journalctl before retrying." >&2
exit 1
