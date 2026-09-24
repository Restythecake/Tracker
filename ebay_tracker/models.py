from __future__ import annotations

from dataclasses import dataclass, field


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

    @property
    def price_text(self) -> str:
        if self.price is None:
            return "Preis unbekannt"
        text = f"{self.price:,.2f} {self.currency}".replace(",", "X").replace(".", ",").replace("X", ".")
        if self.shipping:
            text += f" (+{self.shipping:.2f} Versand)".replace(".", ",")
        return text

    @property
    def is_auction(self) -> bool:
        return "AUCTION" in self.buying_options
