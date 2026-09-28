# Tracker (eBay + Kleinanzeigen)

Überwacht eBay und Kleinanzeigen dauerhaft nach den Configs, die du suchst, und schickt dir eine
Benachrichtigung, sobald ein neues passendes Angebot online kommt.

Läuft auf zwei Arten:

- **als Nest-Plugin** (empfohlen, siehe unten): Suchen im Nest-Einstellungsformular, Push über Nests
  Notification-API, Widgets und eine Bilder-Galerie auf dem Nest-Desktop
- **als eigenständiges CLI** mit `config.yaml` und ntfy/Telegram/Discord/E-Mail (Rest dieser Datei)

- Beliebig viele Suchen, jeweils mit Preisspanne, Pflichtbegriffen, „einer von“-Begriffen und Ausschlusswörtern
- Schreibweisen-tolerant (`rtx 4070` = `RTX-4070` = `rtx4070`), Regex möglich
- Jedes Angebot wird nur **einmal** gemeldet (SQLite-Datenbank in `data/`)
- Benachrichtigung per **ntfy** (Push aufs Handy), **Telegram**, **Discord**, **E-Mail** und Konsole
- Datenquelle: offizielle **eBay Browse API** (empfohlen) oder ohne Keys direkt die eBay-Suchseite

## Als Nest-Plugin

```bash
deploy/install.sh
```

Baut das Image, startet `nest-tracker` (Label `nest.plugin=1`, Netz `nest_default`, Volume
`tracker-data`), installiert und gibt das Plugin in Nest frei und lässt Nest den Token selbst in den
Container schreiben (`apply-token`). Danach:

- **Suchen** bearbeiten: Nest → Einstellungen → Plugins → Tracker → Einstellungen. Eine Zeile pro Suche:

  ```text
  thinkpad t14 32gb max:600 +32gb -defekt -bastler
  name:"Gaming-PC" gaming pc rtx 4070 500-1100 +"rtx 4070" ~ryzen ~i7 -defekt @kleinanzeigen plz:10115 umkreis:50
  ```

  | Token | Bedeutung |
  | --- | --- |
  | Wörter | Suchbegriff beim Marktplatz |
  | `+wort`, `+"a b"` | muss im Titel stehen |
  | `~wort` | mindestens eines davon |
  | `-wort` | darf nicht vorkommen |
  | `max:600`, `min:150`, `150-600`, `<600` | Preis |
  | `@ebay`, `@kleinanzeigen` / `@ka` | nur diese Quelle (sonst beide) |
  | `plz:10115 umkreis:30` | Kleinanzeigen-Umkreis für diese Suche |
  | `zustand:neu\|gebraucht`, `kauf:sofort\|auktion` | eBay-Filter |
  | `name:"…"` | Anzeigename |
  | `a b \| c d` | mehrere Suchbegriffe, Treffer zusammengeführt |
  | `kat:pc` | nur Kategorie PCs (eBay 179, Kleinanzeigen c228) |
  | `bewertung:pc` | GPU/CPU/RAM/SSD/Netzteil lesen, Preis gegen Teilewert bewerten, nur Deals melden |
  | `gpu:4070`, `ram:32` | Mindestausstattung für `bewertung:pc` (Default: RX 6800/RTX 3080-Klasse, 16 GB) |
  | `//` am Anfang | Suche pausieren |

  **PC-Bewertung** (`ebay_tracker/hardware.py`): Aus Titel, Kurztext und – für neue Kandidaten – der
  Detailseite werden die Teile gelesen. Fairer Preis = grobe Gebrauchtpreise der Teile (Tabellen oben
  in der Datei, Stand Herbst 2026). Urteil: *Top-Deal* (≤ 85 %), *Guter Preis* (≤ 100 %), *Fair*,
  *Zu teuer*, *Zu gut um wahr zu sein?* (< 62 % → Betrugsverdacht). Gemeldet werden Top/Gut/Verdacht;
  aussortiert werden zu schwache GPUs/CPUs, zu wenig RAM, Laptops, Gesuche, Tausch, defekt, ohne GPU und
  „Zustand Neu" (Händler-PCs). Zusätzlich Vergleich mit den selbst beobachteten Preisen je GPU.

- **Widgets** auf dem Desktop: *Neue Treffer* (stat), *Neueste Treffer* (list), *Suchen* (status),
  *Angebote* (table), *Preisverlauf* (chart) und *Galerie* (iframe mit Bildern; „Alles ansehen ↗“
  öffnet die Vollbild-Seite).
- **Aktionen** auf der Plugin-Seite: *Jetzt prüfen*, *Testbenachrichtigung*, *Verlauf zurücksetzen*.
- **Push**: jeder neue Treffer einzeln (Tippen öffnet das Angebot), ab `max_notifications` pro Lauf
  eine Sammelmeldung. Neue oder geänderte Suchen lesen erst still ein – pro Quelle, damit ein
  gesperrtes eBay beim ersten Lauf später keine Flut auslöst.

**Zelda-Restock-Tracker** (`ebay_tracker/restock.py`, Widget *Zelda Restock*, Version 0.3.0): prüft
bei jedem Lauf Otto (Suchseite), MediaMarkt/Saturn (Produktseite), Amazon (Buybox) und Müller
(ld+json) auf die Switch 2 „The Legend of Zelda“-Edition. Push kommt **nur beim Wechsel** auf
„verfügbar“ (UVP 519,99 €; Müller listet +2 %). Der Nintendo-Store ist eine Warteschlange
(queue-it) und wird über „Warteschlange aktiv“ erkannt — der ist im Widget-Text, nicht als
eigener Check. Eigene Shop-URLs: Einstellung „Restock: Shop-URLs“ (eine URL oder JSON je Zeile).

Was beim Bauen über Nests Plugin-API aufgefallen ist: [docs/nest-plugin-bericht.md](docs/nest-plugin-bericht.md).

## Abruf: HTTP zuerst, Browser bei Sperre (Scrapling, ab 0.7)

eBay sitzt hinter Akamai und hat den reinen HTTP-Client (curl_cffi, Chrome-Fingerprint) am
27./28.09.2026 **39× in 24 h** gesperrt – bei 30 min Pause je Sperre war eBay fast den ganzen Tag
aus. Seit 0.7 eskaliert der Abruf (`ebay_tracker/sources/fetch.py`):

1. **Stufe 1 – HTTP** (curl_cffi): schnell, wenig RAM, reicht meistens.
2. **Stufe 2 – Browser** ([Scrapling](https://github.com/D4Vinci/Scrapling) `StealthySession`,
   getarntes Chromium): nur wenn Stufe 1 gesperrt wird (403/429/503, Captcha, Cloudflare). Eine
   Session für den ganzen Prozess, ein Tab, ohne Bilder/Fonts/CSS; wärmt je Host einmal die
   Startseite an (Akamai setzt dabei seine Cookies), schließt sich nach 5 min Leerlauf.
3. Erst wenn **beide** scheitern, gilt die Quelle als gesperrt und pausiert 30 min.

**Weniger Anfragen statt mehr Tarnung**: Die eigentliche Ursache der Sperren war die Menge –
jede Alternative (`a | b | c`) war eine eigene eBay-Anfrage, ~20 alle 10 Minuten. Der Scraper
fasst sie jetzt zu eBays ODER-Syntax zusammen, ohne die Bedeutung zu ändern
(`combine_queries` in `sources/scraper.py`): `gaming pc | pc rtx` → `pc (gaming,rtx)`,
`ddr4 96gb | … | ddr5 128gb` → `(128gb,96gb) (ddr4,ddr5)`, GPU-Modelle → `("rtx 3090","rx 7900 xtx")`.
Was sich nicht sauber zerlegen lässt, bleibt einzeln. Mit den aktuellen Suchen: 6 statt 20
eBay-Anfragen je Lauf.

Einstellung **Abruf** (`fetcher`): `auto` (Standard, wie oben), `http` (nur Stufe 1, altes
Verhalten), `stealth` (immer Browser). Je Quelle überschreibbar (`fetcher_overrides`, z. B.
`ebay=stealth`). Das Status-Widget zeigt je Quelle, wie oft Stufe 1 reichte, wie oft der Browser
gerettet hat und wie oft beides gesperrt war.

**Parser**: Kleinanzeigen und eBay lesen über `scrapling.parser.Selector` mit adaptivem Tracking
(`/data/scrapling.db`): liefern die Selektoren nach einem Seitenumbau nichts mehr, sucht Scrapling
die zuletzt gemerkten Elemente per Ähnlichkeit wieder. Fehlt Scrapling, läuft der alte BS4-Pfad.

**Image**: Mit Browser ~1,8 GB statt 266 MB (`docker build --build-arg WITH_BROWSER=0` baut das
schlanke Image ohne Stufe 2). RAM im Betrieb gemessen ~680 MB während eines Laufs mit Browser;
ohne Arbeit wird der Browser nach 5 min geschlossen.

**mydealz** (`@mydealz`/`@md`): liest die serverseitig gerenderte Suche (Deal-JSON im
`data-vue3`-Attribut, kein Browser nötig). Nur aktive Deals, Suchwörter müssen im Titel stehen.
Achtung: `hide_expired`/`sortBy=new` liefern bei mydealz eine fast leere Seite, deshalb ohne.

**Quellen ändern löscht keine Historie mehr**: Die Quellen gehören seit 0.7 nicht mehr zur
Such-Signatur. Eine neue Quelle liest still ein (keine Meldungsflut), eine entfernte Quelle nimmt
nur ihre eigenen Treffer mit. Bestehende Suchen werden beim Update übernommen, nicht zurückgesetzt.

## Schnellstart (CLI)

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
