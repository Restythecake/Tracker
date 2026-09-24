"""Offizielle eBay Browse API (https://developer.ebay.com/api-docs/buy/browse/overview.html)."""
from __future__ import annotations

import base64
import logging
import time

import requests

from ..config import Search, Settings
from ..models import Listing
from .base import SourceError

log = logging.getLogger(__name__)

TOKEN_URL = "https://api.ebay.com/identity/v1/oauth2/token"
SEARCH_URL = "https://api.ebay.com/buy/browse/v1/item_summary/search"
SCOPE = "https://api.ebay.com/oauth/api_scope"


class EbayApiSource:
    def __init__(self, settings: Settings, session: requests.Session | None = None):
        self.settings = settings
        self.session = session or requests.Session()
        self._token: str | None = None
        self._token_expires = 0.0

    def _get_token(self) -> str:
        if self._token and time.time() < self._token_expires - 60:
            return self._token
        creds = f"{self.settings.ebay_client_id}:{self.settings.ebay_client_secret}"
        resp = self.session.post(
            TOKEN_URL,
            headers={
                "Authorization": "Basic " + base64.b64encode(creds.encode()).decode(),
                "Content-Type": "application/x-www-form-urlencoded",
            },
            data={"grant_type": "client_credentials", "scope": SCOPE},
            timeout=20,
        )
        if resp.status_code != 200:
            raise SourceError(f"eBay-Token fehlgeschlagen ({resp.status_code}): {resp.text[:200]}")
        payload = resp.json()
        self._token = payload["access_token"]
        self._token_expires = time.time() + int(payload.get("expires_in", 7200))
        return self._token

    def build_params(self, search: Search) -> dict[str, str]:
        filters = []
        if search.min_price is not None or search.max_price is not None:
            low = f"{search.min_price:g}" if search.min_price is not None else ""
            high = f"{search.max_price:g}" if search.max_price is not None else ""
            filters.append(f"price:[{low}..{high}]")
            filters.append(f"priceCurrency:{self.settings.currency}")
        if search.condition == "new":
            filters.append("conditions:{NEW}")
        elif search.condition == "used":
            filters.append("conditions:{USED}")
        if search.buying == "fixed":
            filters.append("buyingOptions:{FIXED_PRICE|BEST_OFFER}")
        elif search.buying == "auction":
            filters.append("buyingOptions:{AUCTION}")

        params = {
            "q": search.query,
            "sort": "newlyListed",
            "limit": str(min(self.settings.max_results, 200)),
        }
        if filters:
            params["filter"] = ",".join(filters)
        if search.category_id:
            params["category_ids"] = search.category_id
        return params

    def search(self, search: Search) -> list[Listing]:
        resp = self.session.get(
            SEARCH_URL,
            params=self.build_params(search),
            headers={
                "Authorization": f"Bearer {self._get_token()}",
                "X-EBAY-C-MARKETPLACE-ID": self.settings.marketplace,
                "Accept-Language": "de-DE",
            },
            timeout=30,
        )
        if resp.status_code == 401:
            self._token = None  # beim nächsten Durchlauf neu holen
        if resp.status_code != 200:
            raise SourceError(f"eBay-API-Fehler ({resp.status_code}): {resp.text[:200]}")
        return [self._to_listing(item) for item in resp.json().get("itemSummaries", [])]

    def _to_listing(self, item: dict) -> Listing:
        price = item.get("price") or item.get("currentBidPrice") or {}
        shipping = None
        for opt in item.get("shippingOptions") or []:
            cost = (opt.get("shippingCost") or {}).get("value")
            if cost is not None:
                shipping = float(cost)
                break
        location = item.get("itemLocation") or {}
        return Listing(
            item_id=str(item.get("legacyItemId") or item["itemId"]),
            title=item.get("title", ""),
            price=float(price["value"]) if price.get("value") is not None else None,
            currency=price.get("currency", self.settings.currency),
            url=item.get("itemWebUrl", ""),
            image_url=(item.get("image") or {}).get("imageUrl"),
            condition=item.get("condition"),
            buying_options=list(item.get("buyingOptions") or []),
            shipping=shipping,
            location=", ".join(x for x in (location.get("postalCode"), location.get("country")) if x) or None,
        )
