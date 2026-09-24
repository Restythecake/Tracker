from __future__ import annotations

from dataclasses import dataclass, field

# Anzeigenamen der Quellen. Der Schlüssel landet in der Datenbank und in
# Listing.key, darf sich also nicht mehr ändern.
SOURCE_LABELS = {
    "ebay": "eBay",
    "kleinanzeigen": "Kleinanzeigen",
}


@dataclass
class Listing:
    item_id: str
    title: str
    price: float | None
    currency: str
    url: str
    image_url: str | None = None
    condition: str | None = None
    buying_options: list[str] = field(default_factory=list)
    shipping: float | None = None
    location: str | None = None
    source: str = "ebay"
    # Freitext neben dem Preis, z. B. "VB" oder "Zu verschenken"
    price_note: str | None = None

    @property
    def key(self) -> str:
        """Quellenübergreifend eindeutig: IDs von eBay und Kleinanzeigen können kollidieren."""
        return f"{self.source}:{self.item_id}"

    @property
    def source_label(self) -> str:
        return SOURCE_LABELS.get(self.source, self.source)

    @property
    def price_text(self) -> str:
        if self.price is None:
            return self.price_note or "Preis unbekannt"
        text = format_money(self.price, self.currency)
        if self.price_note:
            text += f" {self.price_note}"
        if self.shipping:
            text += f" (+{self.shipping:.2f} Versand)".replace(".", ",")
        return text

    @property
    def is_auction(self) -> bool:
        return "AUCTION" in self.buying_options


def format_money(value: float, currency: str = "EUR") -> str:
    """1234.5 -> '1.234,50 €' (ganze Beträge ohne Nachkommastellen: '1.234 €')."""
    symbol = {"EUR": "€", "USD": "$", "GBP": "£", "CHF": "CHF"}.get(currency, currency)
    if float(value).is_integer():
        number = f"{value:,.0f}"
    else:
        number = f"{value:,.2f}"
    number = number.replace(",", "X").replace(".", ",").replace("X", ".")
    return f"{number} {symbol}"
