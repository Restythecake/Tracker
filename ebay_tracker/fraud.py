"""Betrugs- und Risikosignale einer Anzeige.

Kleinanzeigen-Betrug folgt fast immer demselben Muster: neuer oder
übernommener Account, Preis weit unter Wert, und der Verkäufer drängt aus der
Plattform heraus (WhatsApp, Mail, "sicher bezahlen"-Link, PayPal Freunde,
nur Versand, "bin im Ausland"). Jedes Signal allein ist harmlos; zusammen mit
einem Preis unter ~70 % des Teilewerts ist es ein Grund, NICHT zu überweisen.

Ergebnis ist eine Liste lesbarer Gründe plus ein Score 0..100. Der Tracker
meldet solche Angebote trotzdem (ein echter Glücksfall sieht anfangs genauso
aus), aber mit dem Score im Text.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date

PATTERNS: list[tuple[str, str, int]] = [
    (r"whats\s?app|telegram|signal\b", "will auf WhatsApp/Telegram ausweichen", 25),
    (r"\b[\w.+-]+@[\w-]+\.[a-z]{2,}\b|e-?mail\s*(?:an|:)", "E-Mail-Adresse im Text", 20),
    (r"paypal\s*(?:f(?:ü|ue)r\s*)?freunde|friends\s*(?:&|and)\s*family", "PayPal Freunde (kein Käuferschutz)", 30),
    (r"sicher\s*bezahlen\s*link|zahlungslink|treuhand", "Zahlungslink/Treuhand außerhalb der Plattform", 30),
    (r"nur\s*(?:versand|vorkasse)|keine\s*abholung|abholung\s*nicht\s*m(?:ö|oe)glich", "nur Versand/Vorkasse", 20),
    (r"im\s*ausland|bin\s*(?:gerade\s*)?beruflich|montage|umzug\s*ins\s*ausland", "Verkäufer angeblich im Ausland/unterwegs", 20),
    (r"\bwestern\s*union|moneygram|gutschein|paysafe", "Zahlung per Gutschein/Western Union", 40),
    (r"schnell\s*weg|muss\s*heute\s*weg|nur\s*heute|dringend", "Zeitdruck im Text", 10),
]


@dataclass
class Risk:
    score: int = 0
    reasons: list[str] = field(default_factory=list)

    @property
    def level(self) -> str:
        return "hoch" if self.score >= 50 else "mittel" if self.score >= 25 else "niedrig"


def account_age_days(since: str | None, today: date | None = None) -> int | None:
    if not since:
        return None
    try:
        y, m, d = (int(x) for x in since.split("-"))
        return ((today or date.today()) - date(y, m, d)).days
    except ValueError:
        return None


def assess_risk(title: str, description: str | None, *, price_ratio: float | None,
                seller_since: str | None, today: date | None = None) -> Risk:
    risk = Risk()
    text = f"{title}\n{description or ''}".lower()
    for pattern, reason, weight in PATTERNS:
        if re.search(pattern, text):
            risk.score += weight
            risk.reasons.append(reason)
    age = account_age_days(seller_since, today)
    if age is not None:
        if age < 30:
            risk.score += 35
            risk.reasons.append(f"Account erst {age} Tage alt")
        elif age < 120:
            risk.score += 15
            risk.reasons.append(f"Account erst {age // 30} Monate alt")
    if price_ratio is not None:
        if price_ratio < 0.55:
            risk.score += 30
            risk.reasons.append(f"Preis nur {int(price_ratio * 100)} % des Teilewerts")
        elif price_ratio < 0.7:
            risk.score += 15
            risk.reasons.append(f"Preis deutlich unter Wert ({int(price_ratio * 100)} %)")
    # Ein neuer Account allein ist normal; erst mit Tiefpreis wird es verdächtig
    if age is not None and age < 60 and price_ratio is not None and price_ratio < 0.7:
        risk.score += 20
    risk.score = min(100, risk.score)
    return risk
