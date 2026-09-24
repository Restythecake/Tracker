"""Das Manifest (nest.plugin/v1, PLUGIN-API §2).

Scopes sind absichtlich nur zwei: ``notify`` (der Zweck des Ganzen) und
``widgets:iframe`` (die Galerie). Kein ``http:outbound`` -- das betrifft nur
Aufrufe *durch* Nests API; ein Docker-Plugin, das selbst nach eBay geht,
braucht ihn nicht (§12.5 "Egress auf OSI-Ebene"). Kein ``storage:kv`` -- der
Zustand liegt in einer SQLite-Datei auf dem eigenen Volume.
"""
from __future__ import annotations

from . import __version__
from .engine import DEFAULT_SEARCHES

PLUGIN_ID = "arthur.tracker"

SEARCH_HELP = (
    "Eine Suche pro Eintrag. Wörter = Suchbegriff · +wort muss im Titel stehen · ~wort eines davon · "
    "-wort darf nicht vorkommen · max:600 / min:150 / 150-600 Preis · @ebay / @kleinanzeigen nur diese "
    "Quelle · plz:10115 umkreis:30 · zustand:gebraucht · kauf:sofort · name:\"Anzeigename\" · "
    "// am Anfang pausiert die Suche."
)

MANIFEST = {
    "apiVersion": "nest.plugin/v1",
    "id": PLUGIN_ID,
    "name": "Tracker",
    "version": __version__,
    "description": "Beobachtet eBay und Kleinanzeigen nach deinen Suchen und meldet neue passende Angebote sofort per Push.",
    "author": "Arthur",
    "icon": "icons/tracker.svg",
    "runtime": {"kind": "docker", "port": 8080},
    "scopes": ["notify", "widgets:iframe"],
    "widgets": [
        {
            "id": "overview", "kind": "stat", "name": "Neue Treffer", "title": "Tracker",
            "description": "Wie viele neue passende Angebote in den letzten 24 Stunden aufgetaucht sind.",
            "sizes": ["1x1", "2x1"], "defaultSize": "2x1", "refreshSeconds": 60,
        },
        {
            "id": "latest", "kind": "list", "name": "Neueste Treffer", "title": "Neueste Treffer",
            "description": "Die zuletzt gefundenen Angebote mit Preis, Quelle und Ort.",
            "sizes": ["2x2", "3x2", "4x2"], "defaultSize": "3x2", "refreshSeconds": 60,
        },
        {
            "id": "searches", "kind": "status", "name": "Suchen", "title": "Suchen",
            "description": "Zustand jeder Suche: wann geprüft, wie viele passen, ob eine Quelle hakt.",
            "sizes": ["2x2", "3x2", "4x2"], "defaultSize": "3x2", "refreshSeconds": 60,
        },
        {
            "id": "deals", "kind": "table", "name": "Angebote (Tabelle)", "title": "Passende Angebote",
            "description": "Alle aktuell passenden Angebote als Tabelle.",
            "sizes": ["4x2", "6x2"], "defaultSize": "6x2", "refreshSeconds": 120,
        },
        {
            "id": "prices", "kind": "chart", "name": "Preisverlauf", "title": "Preisverlauf",
            "description": "Median-Preis der passenden Angebote je Suche über 14 Tage.",
            "sizes": ["3x2", "4x2", "6x2"], "defaultSize": "4x2", "refreshSeconds": 600,
        },
        {
            "id": "gallery", "kind": "iframe", "name": "Galerie", "title": "Tracker",
            "description": "Die Angebote mit Bildern, nach Suche filterbar; Klick öffnet das Angebot.",
            "sizes": ["4x2", "6x2"], "defaultSize": "6x2",
        },
    ],
    "actions": [
        {"id": "check-now", "name": "Jetzt prüfen", "title": "Jetzt prüfen"},
        {"id": "test-notify", "name": "Testbenachrichtigung", "title": "Testbenachrichtigung senden"},
        {"id": "reset", "name": "Verlauf zurücksetzen", "title": "Verlauf zurücksetzen", "danger": True,
         "confirm": "Alle gemerkten Angebote vergessen? Der nächste Lauf meldet nichts, er merkt sich nur neu."},
    ],
    "settings": {
        "type": "object",
        "properties": {
            "searches": {
                "type": "array", "items": {"type": "string"}, "title": "Suchen",
                "description": SEARCH_HELP, "default": DEFAULT_SEARCHES,
                "x-nest-placeholder": "thinkpad t14 32gb max:600 +32gb -defekt",
            },
            "default_sources": {
                "type": "array", "items": {"type": "string", "enum": ["ebay", "kleinanzeigen"]},
                "title": "Quellen", "description": "Wo gesucht wird, wenn eine Suche kein @ebay/@kleinanzeigen nennt.",
                "default": ["ebay", "kleinanzeigen"],
            },
            "interval_minutes": {
                "type": "integer", "minimum": 3, "maximum": 1440, "default": 10,
                "title": "Prüfintervall (Minuten)",
                "description": "Unter 5 Minuten riskierst du Captchas und Sperren bei eBay/Kleinanzeigen.",
            },
            "notify_level": {
                "type": "string", "enum": ["min", "low", "default", "high", "urgent"], "default": "high",
                "title": "Priorität der Benachrichtigung",
            },
            "max_notifications": {
                "type": "integer", "minimum": 1, "maximum": 50, "default": 5,
                "title": "Max. Einzelmeldungen pro Lauf",
                "description": "Darüber kommt eine Sammelmeldung statt einer Flut.",
            },
            "kleinanzeigen_plz": {
                "type": "string", "title": "Kleinanzeigen: PLZ", "x-nest-placeholder": "10115",
                "description": "Leer = ganz Deutschland. Einzelne Suchen können mit plz:… abweichen.",
            },
            "kleinanzeigen_radius": {
                "type": "integer", "minimum": 0, "maximum": 500, "default": 0,
                "title": "Kleinanzeigen: Umkreis (km)", "description": "0 = ohne Umkreis.",
            },
            "notify_first_run": {
                "type": "boolean", "default": False, "title": "Beim ersten Lauf alles melden",
                "description": "Aus: eine neue Suche merkt sich erst still, was schon online ist.",
                "x-nest-advanced": True,
            },
            "ebay_marketplace": {
                "type": "string", "enum": ["EBAY_DE", "EBAY_AT", "EBAY_CH", "EBAY_US", "EBAY_GB", "EBAY_FR",
                                           "EBAY_IT", "EBAY_ES", "EBAY_NL"],
                "default": "EBAY_DE", "title": "eBay-Marktplatz", "x-nest-advanced": True,
            },
            "ebay_client_id": {
                "type": "string", "title": "eBay API: Client ID", "x-nest-advanced": True,
                "description": "Optional. Mit Keys nutzt der Tracker die offizielle Browse API statt der Webseite.",
            },
            "ebay_client_secret": {
                "type": "string", "format": "password", "title": "eBay API: Client Secret",
                "x-nest-advanced": True,
            },
        },
    },
}
