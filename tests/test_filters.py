from ebay_tracker.config import Search
from ebay_tracker.filters import matches, rejection_reason, term_matches
from ebay_tracker.models import Listing


def listing(title, price=800.0, options=("FIXED_PRICE",)):
    return Listing(item_id="1", title=title, price=price, currency="EUR", url="u", buying_options=list(options))


def test_term_ignores_spacing_and_case():
    assert term_matches("rtx 4070", "Gaming PC RTX-4070 Ryzen")
    assert term_matches("RTX 4070", "pc rtx4070 super")
    assert not term_matches("rtx 4070", "pc rtx 4060")


def test_regex_term():
    assert term_matches(r"re:\b4070\b(?!\s*ti)", "RTX 4070 Super")
    assert not term_matches(r"re:\b4070\b(?!\s*ti)", "RTX 4070 Ti")


def test_include_exclude_and_price():
    s = Search(name="x", query="q", min_price=500, max_price=1000,
               include_all=["rtx 4070"], include_any=["ryzen 7", "i7"], exclude=["defekt"])
    assert matches(listing("PC RTX 4070 Ryzen 7 7800X3D"), s)
    assert "fehlt" in rejection_reason(listing("PC RTX 4060 Ryzen 7"), s)
    assert "keiner" in rejection_reason(listing("PC RTX 4070 i5"), s)
    assert "ausgeschlossen" in rejection_reason(listing("PC RTX 4070 i7 defekt"), s)
    assert ">" in rejection_reason(listing("PC RTX 4070 i7", price=1500), s)
    assert "<" in rejection_reason(listing("PC RTX 4070 i7", price=100), s)


def test_buying_type():
    auction = Search(name="a", query="q", buying="auction")
    fixed = Search(name="f", query="q", buying="fixed")
    assert not matches(listing("x", options=["FIXED_PRICE"]), auction)
    assert matches(listing("x", options=["AUCTION"]), auction)
    assert not matches(listing("x", options=["AUCTION"]), fixed)
    assert matches(listing("x", options=["AUCTION", "FIXED_PRICE"]), fixed)
