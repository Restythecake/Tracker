"""Benachrichtigungskanäle: Konsole, ntfy (Push aufs Handy), Telegram, Discord, E-Mail."""
from __future__ import annotations

import html
import logging
import smtplib
from email.message import EmailMessage
from typing import Any, Protocol

import requests

from .models import Listing

log = logging.getLogger(__name__)


class Notifier(Protocol):
    name: str

    def send(self, search_name: str, listing: Listing) -> None: ...


def _headline(search_name: str, listing: Listing) -> str:
    kind = "Auktion" if listing.is_auction else "Sofortkauf"
    return f"[{search_name}] {listing.price_text} · {kind}"


class ConsoleNotifier:
    name = "console"

    def send(self, search_name: str, listing: Listing) -> None:
        print(f"\n🔔 {_headline(search_name, listing)}\n   {listing.title}\n   {listing.url}", flush=True)


class NtfyNotifier:
    """https://ntfy.sh – kostenlose App für iOS/Android, kein Account nötig."""

    name = "ntfy"
    PRIORITIES = {"min": 1, "low": 2, "default": 3, "high": 4, "max": 5, "urgent": 5}

    def __init__(self, topic: str, server: str = "https://ntfy.sh", token: str | None = None, priority: str = "high"):
        self.server = server.rstrip("/")
        self.topic = topic
        self.token = token
        self.priority = self.PRIORITIES.get(str(priority).lower(), 4)

    def send(self, search_name: str, listing: Listing) -> None:
        # JSON-Variante, damit Umlaute/Emojis im Titel sauber ankommen
        payload: dict[str, Any] = {
            "topic": self.topic,
            "title": _headline(search_name, listing),
            "message": listing.title,
            "click": listing.url,
            "tags": ["shopping_cart"],
            "priority": self.priority,
        }
        if listing.image_url:
            payload["attach"] = listing.image_url
        headers = {"Authorization": f"Bearer {self.token}"} if self.token else {}
        resp = requests.post(self.server, json=payload, headers=headers, timeout=15)
        resp.raise_for_status()


class TelegramNotifier:
    name = "telegram"

    def __init__(self, bot_token: str, chat_id: str):
        self.url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
        self.chat_id = chat_id

    def send(self, search_name: str, listing: Listing) -> None:
        text = (
            f"🔔 <b>{html.escape(_headline(search_name, listing))}</b>\n"
            f"{html.escape(listing.title)}\n"
            f'<a href="{html.escape(listing.url)}">Zum Angebot</a>'
        )
        resp = requests.post(
            self.url,
            json={"chat_id": self.chat_id, "text": text, "parse_mode": "HTML"},
            timeout=15,
        )
        resp.raise_for_status()


class DiscordNotifier:
    name = "discord"

    def __init__(self, webhook_url: str):
        self.webhook_url = webhook_url

    def send(self, search_name: str, listing: Listing) -> None:
        embed: dict[str, Any] = {
            "title": listing.title[:256],
            "url": listing.url,
            "description": _headline(search_name, listing),
            "color": 0x0064D2,
        }
        if listing.image_url:
            embed["thumbnail"] = {"url": listing.image_url}
        resp = requests.post(self.webhook_url, json={"embeds": [embed]}, timeout=15)
        resp.raise_for_status()


class EmailNotifier:
    name = "email"

    def __init__(self, smtp_host: str, username: str, password: str, to: str, smtp_port: int = 587, sender: str | None = None):
        self.smtp_host = smtp_host
        self.smtp_port = int(smtp_port)
        self.username = username
        self.password = password
        self.to = to
        self.sender = sender or username

    def send(self, search_name: str, listing: Listing) -> None:
        msg = EmailMessage()
        msg["Subject"] = f"eBay: {_headline(search_name, listing)}"
        msg["From"] = self.sender
        msg["To"] = self.to
        msg.set_content(f"{listing.title}\n{listing.price_text}\n\n{listing.url}\n")
        with smtplib.SMTP(self.smtp_host, self.smtp_port, timeout=30) as smtp:
            smtp.starttls()
            smtp.login(self.username, self.password)
            smtp.send_message(msg)


def _complete(cfg: dict[str, Any], *keys: str) -> bool:
    return all(cfg.get(k) for k in keys)


def build_notifiers(cfg: dict[str, Any]) -> list[Notifier]:
    """Erstellt alle konfigurierten Kanäle. Kanäle mit fehlenden Werten werden übersprungen."""
    notifiers: list[Notifier] = []
    if cfg.get("console", True):
        notifiers.append(ConsoleNotifier())

    ntfy = cfg.get("ntfy") or {}
    if _complete(ntfy, "topic"):
        notifiers.append(
            NtfyNotifier(ntfy["topic"], ntfy.get("server") or "https://ntfy.sh", ntfy.get("token"), ntfy.get("priority") or "high")
        )
    telegram = cfg.get("telegram") or {}
    if _complete(telegram, "bot_token", "chat_id"):
        notifiers.append(TelegramNotifier(telegram["bot_token"], str(telegram["chat_id"])))
    discord = cfg.get("discord") or {}
    if _complete(discord, "webhook_url"):
        notifiers.append(DiscordNotifier(discord["webhook_url"]))
    email = cfg.get("email") or {}
    if _complete(email, "smtp_host", "username", "password", "to"):
        notifiers.append(
            EmailNotifier(
                email["smtp_host"], email["username"], email["password"], email["to"],
                email.get("smtp_port") or 587, email.get("sender"),
            )
        )
    return notifiers


def notify_all(notifiers: list[Notifier], search_name: str, listing: Listing) -> bool:
    """Sendet an alle Kanäle. True, wenn mindestens ein Kanal erfolgreich war."""
    ok = False
    for notifier in notifiers:
        try:
            notifier.send(search_name, listing)
            ok = True
        except Exception as exc:  # ein kaputter Kanal soll die anderen nicht blockieren
            log.error("Benachrichtigung über %s fehlgeschlagen: %s", notifier.name, exc)
    return ok
