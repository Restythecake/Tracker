#!/usr/bin/env bash
# Baut und startet den Tracker.
#
#   deploy/install.sh
#
# Mehr braucht es nicht: Nest erkennt den Container an seinen Labels, und den
# Token holt sich der Tracker selbst (POST /claim). Neue Rechte akzeptiert man
# einmal in Nest (Rechtsklick auf die Kachel → Plugin-Einstellungen); bis dahin
# läuft der Tracker ohne Benachrichtigungen und fragt jede Minute wieder.
set -euo pipefail
cd "$(dirname "$0")/.."

CONTAINER=nest-tracker

echo "==> Container bauen und starten"
docker compose up -d --build

echo "==> Warten auf Health"
for _ in $(seq 1 30); do
  [ "$(docker inspect -f '{{.State.Health.Status}}' "$CONTAINER" 2>/dev/null)" = healthy ] && break
  sleep 1
done

echo "==> Token-Status laut Log"
docker logs "$CONTAINER" 2>&1 | grep -E "Token von Nest|Nest gibt keinen Token" | tail -n 1 \
  || echo "    noch kein Claim im Log – docker logs $CONTAINER"
