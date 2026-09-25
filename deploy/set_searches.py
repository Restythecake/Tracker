#!/usr/bin/env python3
"""Setzt die aktuellen Suchen des Tracker-Plugins per Nest-API."""
import json
import subprocess

SEARCHES = [
    'name:"Gaming-PC bis 1000 €" gaming pc | pc rtx 400-1000 bewertung:pc deal:0.9 '
    'zustand:gebraucht kauf:sofort -defekt -bastler',
    'name:"GPU-Schnäppchen im Paket" pc rtx | pc rx | gaming pc bewertung:pc deal:0.78 '
    'zustand:gebraucht kauf:sofort -defekt -bastler',
    'name:"Grafikkarten einzeln" rtx 3090 | rtx 4080 | rtx 4090 | rtx 5080 | rx 7900 xtx | rtx 4070 ti super '
    'bewertung:gpu gpu:3080 deal:0.8 zustand:gebraucht kauf:sofort -defekt -bastler @ebay @ka @vinted',
    'name:"RAM 64GB Deals" ddr4 64gb | ddr5 64gb bewertung:ram ram:64 -defekt -ecc -server @ebay @ka @vinted',
    'name:"RAM 96/128GB Deals" ddr4 96gb | ddr4 128gb | ddr5 96gb | ddr5 128gb '
    'bewertung:ram ram:96 -defekt -ecc -server',
    'name:"Zelda-Switch bis UVP" switch 2 zelda edition | switch 2 zelda konsole | switch 2 zelda jubiläum '
    '350-540 +zelda +"switch 2" ~edition ~jubiläum ~anniversary ~sondermodell ~40 '
    '-spiel -softwarekarte -tears -kingdom -breath -botw -totk -ocarina -lot -bundle -hülle -tasche '
    '-controller -amiibo -dock -skin -folie -suche -tausch @ka @vinted @ebay',
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