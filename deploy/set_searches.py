#!/usr/bin/env python3
"""Setzt die aktuellen Suchen des Tracker-Plugins per Nest-API."""
import json
import subprocess

SEARCHES = [
    'name:"Gaming-PC bis 1000 €" gaming pc | pc rtx 400-1000 bewertung:pc deal:0.9 '
    'zustand:gebraucht kauf:sofort -defekt -bastler',
    'name:"GPU-Schnäppchen im Paket" pc rtx | pc rx | gaming pc bewertung:pc deal:0.78 '
    'zustand:gebraucht kauf:sofort -defekt -bastler',
    'name:"RAM 64GB Deals" ddr4 64gb | ddr5 64gb bewertung:ram -defekt -ecc -server',
    'name:"RAM 96/128GB Deals" ddr4 96gb | ddr4 128gb | ddr5 96gb | ddr5 128gb '
    'bewertung:ram -defekt -ecc -server',
]
BODY = json.dumps({
    "searches": SEARCHES,
    "default_sources": ["ebay", "kleinanzeigen"],
    "interval_minutes": 10,
    "notify_level": "urgent",
    "max_notifications": 5,
    "kleinanzeigen_plz": "49356",
    "kleinanzeigen_radius": 200,
})
out = subprocess.run(
    ["docker", "run", "--rm", "--network", "proxy", "curlimages/curl", "-sS", "-m", "60",
     "-X", "PUT", "-H", "Remote-User: arthur", "-H", "Content-Type: application/json",
     "-d", BODY, "http://nest:8080/api/v1/plugins/arthur.tracker/settings"],
    capture_output=True, text=True, timeout=90)
print(out.stdout.strip() or out.stderr)