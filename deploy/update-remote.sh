#!/usr/bin/env bash
set -euo pipefail

APP_DIR=/opt/kefu/repo
BRANCH=main

if [[ "$(id -u)" -ne 0 ]]; then
  echo "Run this script as root on the deployment server." >&2
  exit 1
fi

if [[ ! -d "$APP_DIR/.git" ]]; then
  echo "Git checkout not found at $APP_DIR." >&2
  exit 1
fi

if [[ -n "$(git -C "$APP_DIR" status --porcelain)" ]]; then
  echo "Refusing to update a Git checkout with local changes: $APP_DIR" >&2
  exit 1
fi

if [[ ! -f "$APP_DIR/.env" ]]; then
  echo "Compose deployment configuration is missing: $APP_DIR/.env" >&2
  exit 1
fi

if ! command -v docker >/dev/null || ! docker compose version >/dev/null 2>&1; then
  echo "Docker Compose v2 is required for deployment." >&2
  exit 1
fi

if ! git -C "$APP_DIR" pull --ff-only origin "$BRANCH"; then
  echo "Git update failed; running Compose services were not changed." >&2
  exit 1
fi

cd "$APP_DIR"
docker compose up -d --build --remove-orphans

for _ in {1..30}; do
  if curl -fsS -o /dev/null http://127.0.0.1:18084/ \
    && curl -fsS -o /dev/null http://127.0.0.1:18084/healthz; then
    printf 'Updated to %s; Compose page and proxied API health checks passed.\n' \
      "$(git -C "$APP_DIR" rev-parse --short HEAD)"
    exit 0
  fi
  sleep 2
done

docker compose ps >&2 || true
docker compose logs --tail=100 frontend web bot-worker migrate >&2 || true
echo "Compose services did not become healthy; inspect the logs before retrying." >&2
exit 1
