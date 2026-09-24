#!/usr/bin/env bash
# Baut und startet das Tracker-Plugin und installiert es in Nest.
#
#   deploy/install.sh            # bauen, starten, installieren/aktualisieren
#
# Die Admin-API ruft das Skript aus einem Wegwerf-Container im Docker-Netz
# "proxy" auf, mit Remote-User -- genau so, wie Caddy+Authelia es tun
# (NEST_AUTH_MODE=proxy, NEST_AUTH_TRUSTED_PROXIES). Der Klartext-Token geht nie
# durch dieses Skript: "apply-token" lässt Nest ihn selbst in den Container
# schreiben (PLUGIN-API §5.1a).
set -euo pipefail
cd "$(dirname "$0")/.."

PLUGIN_ID=arthur.tracker
CONTAINER=nest-tracker
NEST_USER=${NEST_USER:-arthur}

api() { # api METHOD PATH [BODY]
  docker run --rm --network proxy curlimages/curl -sS -m 30 -X "$1" \
    -H "Remote-User: $NEST_USER" -H 'Content-Type: application/json' \
    ${3:+-d "$3"} "http://nest:8080/api/v1$2"
}
field() { python3 -c "import json,sys; d=json.load(sys.stdin); print(eval('d'+sys.argv[1]))" "$1" 2>/dev/null || true; }

echo "==> Container bauen und starten"
docker compose up -d --build

echo "==> Warten auf Health"
for _ in $(seq 1 30); do
  [ "$(docker inspect -f '{{.State.Health.Status}}' "$CONTAINER" 2>/dev/null)" = healthy ] && break
  sleep 1
done

state=$(api GET "/plugins/$PLUGIN_ID" | field "['state']")
if [ -z "$state" ]; then
  echo "==> Installieren (Discovery über das Label nest.plugin=1)"
  api GET /plugins/discoverable | python3 -c "
import json,sys
d=json.load(sys.stdin); d=d.get('items',d) if isinstance(d,dict) else d
for c in d:
    if c.get('container','').lstrip('/')=='$CONTAINER':
        print('   gefunden:', c.get('id'), c.get('version'), c.get('error') or 'ok')"
  api POST /plugins "{\"source\":\"container\",\"ref\":\"$CONTAINER\"}" | field "['state']"
  echo "==> Freigeben (alle angeforderten Scopes)"
  api POST "/plugins/$PLUGIN_ID/approve" '{}' | field "['plugin']['state']"
else
  echo "==> Bereits installiert ($state) – Manifest aktualisieren"
  api POST "/plugins/$PLUGIN_ID/update" "{\"source\":\"container\",\"ref\":\"$CONTAINER\"}" | field "['version']"
fi

echo "==> Token in den Container übernehmen"
api POST "/plugins/$PLUGIN_ID/apply-token" '{}' | python3 -c "
import json,sys; d=json.load(sys.stdin)
print('   ', d.get('plugin',{}).get('state') or d, *(d.get('warnings') or []))"

echo "==> Health laut Nest"
api GET "/plugins/$PLUGIN_ID/health"; echo
