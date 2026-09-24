# eBay-Tracker

Überwacht eBay dauerhaft nach den Configs, die du suchst, und schickt dir eine Benachrichtigung,
sobald ein neues passendes Angebot online kommt.

- Beliebig viele Suchen, jeweils mit Preisspanne, Pflichtbegriffen, „einer von“-Begriffen und Ausschlusswörtern
- Schreibweisen-tolerant (`rtx 4070` = `RTX-4070` = `rtx4070`), Regex möglich
- Jedes Angebot wird nur **einmal** gemeldet (SQLite-Datenbank in `data/`)
- Benachrichtigung per **ntfy** (Push aufs Handy), **Telegram**, **Discord**, **E-Mail** und Konsole
- Datenquelle: offizielle **eBay Browse API** (empfohlen) oder ohne Keys direkt die eBay-Suchseite

## Schnellstart

```bash
python -m venv .venv && source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -r requirements.txt

# 1. config.yaml anpassen (Suchen eintragen)
# 2. Benachrichtigungskanal einrichten, z. B. ntfy:
export NTFY_TOPIC=ebay-mein-geheimes-topic-123

python -m ebay_tracker test-notify   # kommt die Testnachricht an?
python -m ebay_tracker check         # Probelauf: was würde gerade gefunden?
python -m ebay_tracker run           # dauerhaft überwachen
```

Beim **ersten Lauf** werden alle aktuell vorhandenen Treffer nur gespeichert (sonst gäbe es eine Flut
an Nachrichten). Ab dann wird nur gemeldet, was neu dazukommt. Wer auch die bestehenden Treffer
gemeldet haben will: `notify_on_first_run: true`.

## Befehle

| Befehl | Zweck |
| --- | --- |
| `python -m ebay_tracker run` | Endlosschleife, prüft alle `interval_minutes` |
| `python -m ebay_tracker run --once` | ein Durchlauf (für cron / GitHub Actions) |
| `python -m ebay_tracker check` | zeigt aktuelle Treffer ✅ und warum andere verworfen wurden |
| `python -m ebay_tracker test-notify` | Testnachricht an alle Kanäle |
| `-v` | ausführliches Log (vor dem Befehl: `python -m ebay_tracker -v run`) |
| `-c pfad.yaml` | andere Konfigurationsdatei |

## Suchen konfigurieren

Siehe Kommentare in [`config.yaml`](config.yaml). Beispiel:

```yaml
searches:
  - name: Gaming-PC RTX 4070
    query: gaming pc rtx 4070          # das, was du bei eBay eintippen würdest
    min_price: 500
    max_price: 1100
    include_all: ["rtx 4070"]          # muss alles im Titel stehen
    include_any: ["ryzen 7", "i7"]     # mindestens eins davon
    exclude: [defekt, bastler, suche]  # nichts davon
    condition: any                     # any | new | used
    buying: all                        # all | fixed | auction
```

## Benachrichtigungen

Geheimnisse gehören nicht in die `config.yaml`, sondern in Umgebungsvariablen
(in der Config per `${NAME}` referenziert). Kanäle ohne Werte werden automatisch übersprungen.

- **ntfy** (am einfachsten): App [ntfy](https://ntfy.sh) installieren, ein langes zufälliges Topic abonnieren,
  `NTFY_TOPIC` setzen. Kein Account nötig. Tippen auf die Nachricht öffnet das Angebot.
- **Telegram**: Bot über [@BotFather](https://t.me/BotFather) erstellen → `TELEGRAM_BOT_TOKEN`;
  dem Bot schreiben und die Chat-ID über `https://api.telegram.org/bot<TOKEN>/getUpdates` auslesen → `TELEGRAM_CHAT_ID`.
- **Discord**: Kanal → Integrationen → Webhook → `DISCORD_WEBHOOK_URL`.
- **E-Mail (Gmail)**: [App-Passwort](https://myaccount.google.com/apppasswords) erzeugen →
  `EMAIL_USER`, `EMAIL_APP_PASSWORD`, `EMAIL_TO`.

## eBay-API-Keys (empfohlen)

Ohne Keys liest der Tracker die normale eBay-Suchseite. Das funktioniert, kann aber bei häufigen
Abfragen ein Captcha auslösen oder nach Layout-Änderungen bei eBay kaputtgehen.
Stabiler ist die offizielle API (kostenlos, 5.000 Abfragen/Tag):

1. Account auf <https://developer.ebay.com> anlegen
2. Unter *Application Keys* ein **Production**-Keyset erzeugen
3. `EBAY_CLIENT_ID` (App ID) und `EBAY_CLIENT_SECRET` (Cert ID) setzen

Mit `source: auto` wird die API automatisch genutzt, sobald die Keys gesetzt sind.

## Dauerhaft laufen lassen

**Linux / Raspberry Pi (systemd):** `/etc/systemd/system/ebay-tracker.service`

```ini
[Unit]
Description=eBay Tracker
After=network-online.target

[Service]
WorkingDirectory=/home/pi/Tracker
EnvironmentFile=/home/pi/Tracker/.env
ExecStart=/home/pi/Tracker/.venv/bin/python -m ebay_tracker run
Restart=always
RestartSec=60

[Install]
WantedBy=multi-user.target
```

`.env` enthält Zeilen wie `NTFY_TOPIC=...`. Dann `sudo systemctl enable --now ebay-tracker`.

**GitHub Actions:** [`.github/workflows/tracker.yml`](.github/workflows/tracker.yml) führt den Tracker
in der Cloud aus. Dort den `schedule`-Block einkommentieren und die Secrets im Repo anlegen.
Nachteile: GitHub startet geplante Läufe oft mit mehreren Minuten Verzögerung, und bei eBay-Webseitensuche
von GitHub-Servern aus sind Blockaden wahrscheinlich. Dort also API-Keys verwenden.

## Tests

```bash
pip install pytest && python -m pytest
```
