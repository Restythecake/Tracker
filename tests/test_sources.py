from ebay_tracker.config import Search, Settings
from ebay_tracker.sources.ebay_api import EbayApiSource
from ebay_tracker.sources.scraper import EbayScraperSource, parse_price, parse_results

OLD_LAYOUT = """
<ul class="srp-results">
  <li class="s-item"><a class="s-item__link" href="https://www.ebay.com/itm/123456"><div class="s-item__title">Shop on eBay</div></a></li>
  <li class="s-item">
    <a class="s-item__link" href="https://www.ebay.de/itm/Gaming-PC/315123456789?hash=abc">
      <div class="s-item__title"><span class="LIGHT_HIGHLIGHT">Neues Angebot</span>Gaming PC RTX 4070 Ryzen 7</div>
    </a>
    <img src="https://i.ebayimg.com/1.jpg">
    <span class="s-item__price">EUR 899,00</span>
    <span class="s-item__shipping">+EUR 19,99 Versand</span>
  </li>
  <li class="s-item">
    <a class="s-item__link" href="https://www.ebay.de/itm/315999999999"><div class="s-item__title">RTX 4070 PC Auktion</div></a>
    <span class="s-item__price">EUR 1.234,50</span><span>3 Gebote</span>
    <span class="s-item__shipping">Kostenloser Versand</span>
  </li>
</ul>
"""

NEW_LAYOUT = """
<ul class="srp-results">
  <li class="s-card" data-listingid="226111111111">
    <a class="su-link" href="https://www.ebay.de/itm/226111111111?_skw=x"><img src="https://i.ebayimg.com/2.jpg"></a>
    <a class="su-link" href="https://www.ebay.de/itm/226111111111?_skw=x">
      <div class="s-card__title"><span>ThinkPad T14 Gen 2 32GB</span><span>Wird in neuem Fenster oder Tab geöffnet</span></div>
    </a>
    <span class="s-card__price">549,00 EUR</span>
  </li>
</ul>
"""


def test_parse_price():
    assert parse_price("EUR 1.234,56") == 1234.56
    assert parse_price("$1,234.56") == 1234.56
    assert parse_price("EUR 10,00 bis EUR 20,00") == 10.0
    assert parse_price("1234.56") == 1234.56  # ohne Tausenderpunkt (ld+json) las er früher 123
    assert parse_price("1234") == 1234.0
    assert parse_price("EUR 899") == 899.0
    assert parse_price("") is None


def test_parse_old_layout():
    items = parse_results(OLD_LAYOUT, "EUR")
    assert [i.item_id for i in items] == ["315123456789", "315999999999"]
    first, second = items
    assert first.title == "Gaming PC RTX 4070 Ryzen 7"
    assert first.price == 899.0 and first.shipping == 19.99
    assert first.url == "https://www.ebay.de/itm/315123456789"
    assert first.image_url == "https://i.ebayimg.com/1.jpg"
    assert not first.is_auction
    assert second.price == 1234.5 and second.shipping == 0.0 and second.is_auction


def test_parse_new_layout():
    (item,) = parse_results(NEW_LAYOUT, "EUR")
    assert item.item_id == "226111111111"
    assert item.title == "ThinkPad T14 Gen 2 32GB"
    assert item.price == 549.0


def test_scraper_params():
    s = Search(name="x", query="rtx 4070", min_price=500, max_price=1000, condition="used", buying="fixed")
    params = EbayScraperSource(Settings()).build_params(s)
    assert params["_sop"] == "10" and params["_udlo"] == "500" and params["_udhi"] == "1000"
    assert params["LH_ItemCondition"] == "3000" and params["LH_BIN"] == "1"


def test_api_params_and_mapping():
    src = EbayApiSource(Settings(ebay_client_id="a", ebay_client_secret="b"))
    s = Search(name="x", query="rtx 4070", max_price=1000, condition="new", buying="auction")
    params = src.build_params(s)
    assert params["sort"] == "newlyListed"
    assert params["filter"] == "price:[..1000],priceCurrency:EUR,conditions:{NEW},buyingOptions:{AUCTION}"

    listing = src._to_listing({
        "itemId": "v1|315123456789|0", "legacyItemId": "315123456789", "title": "PC",
        "price": {"value": "899.00", "currency": "EUR"}, "itemWebUrl": "https://www.ebay.de/itm/315123456789",
        "buyingOptions": ["FIXED_PRICE"], "shippingOptions": [{"shippingCost": {"value": "4.99"}}],
        "itemLocation": {"postalCode": "10*", "country": "DE"},
    })
    assert listing.item_id == "315123456789" and listing.price == 899.0 and listing.shipping == 4.99
    assert listing.location == "10*, DE"


def _api_source_with_token(monkeypatch):
    """EbayApiSource mit gemocktem Token (kein echter OAuth-Call)."""
    src = EbayApiSource(Settings(ebay_client_id="a", ebay_client_secret="b"))
    src._token = "t"
    src._token_expires = float("inf")
    return src


class _FakeResp:
    def __init__(self, payload, status=200):
        self._payload = payload
        self.status_code = status
        self.text = ""

    def json(self):
        return self._payload


def test_api_details_mapping(monkeypatch):
    src = _api_source_with_token(monkeypatch)
    item = {
        "condition": "Neu",
        "localizedAspects": [
            {"name": "Marke", "value": ["Lenovo"]},
            {"name": "Artikelzustand", "value": ["Neu"]},
            {"name": "Prozessortyp", "value": ["Intel Core i7-1265U"]},
            {"name": "Riesig", "value": ["x" * 300]},
        ],
        "shortDescription": "Kurze Beschreibung",
        "seller": {"username": "shopx", "feedbackPercentage": "99,4", "feedbackScore": 1234},
    }
    monkeypatch.setattr(src.session, "get", lambda *a, **kw: _FakeResp(item))
    from ebay_tracker.models import Listing
    l = Listing(item_id="315123456789", title="t", price=1, currency="EUR", url="u", source="ebay")
    src.load_details(l)
    assert l.condition == "Neu" and l.is_new and l.details_loaded
    assert "Marke: Lenovo" in l.description
    assert "Prozessortyp: Intel Core i7-1265U" in l.description and "Riesig" not in l.description
    assert "shopx (99,4 % positiv" in l.description


def test_api_details_no_seller_without_feedback(monkeypatch):
    src = _api_source_with_token(monkeypatch)
    monkeypatch.setattr(src.session, "get", lambda *a, **kw: _FakeResp({"condition": "Gebraucht"}))
    from ebay_tracker.models import Listing
    l = Listing(item_id="1", title="t", price=1, currency="EUR", url="u", source="ebay")
    src.load_details(l)
    assert l.condition == "Gebraucht" and not l.is_new and l.description is None


def test_api_details_rate_limit_blocked(monkeypatch):
    src = _api_source_with_token(monkeypatch)
    monkeypatch.setattr(src.session, "get", lambda *a, **kw: _FakeResp({}, status=429))
    from ebay_tracker.models import Listing
    from ebay_tracker.sources.base import SourceError
    l = Listing(item_id="1", title="t", price=1, currency="EUR", url="u", source="ebay")
    try:
        src.load_details(l)
        assert False, "sollte SourceError werfen"
    except SourceError as exc:
        assert getattr(exc, "blocked", False)
