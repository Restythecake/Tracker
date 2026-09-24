import textwrap

import pytest

from ebay_tracker.config import Config, ConfigError, Search, Settings, load_config
from ebay_tracker.models import Listing
from ebay_tracker.storage import Storage
from ebay_tracker.tracker import Tracker


class FakeSource:
    def __init__(self):
        self.listings = []

    def search(self, search):
        return list(self.listings)


class FakeNotifier:
    name = "fake"

    def __init__(self):
        self.sent = []

    def send(self, search_name, listing):
        self.sent.append(listing.item_id)


def make(item_id, title="PC RTX 4070", price=800.0):
    return Listing(item_id=item_id, title=title, price=price, currency="EUR", url=f"u/{item_id}")


def setup(notify_on_first_run=False):
    config = Config(
        settings=Settings(notify_on_first_run=notify_on_first_run),
        searches=[Search(name="pc", query="rtx 4070", max_price=1000, include_all=["4070"])],
        notifiers={},
    )
    source, notifier = FakeSource(), FakeNotifier()
    return Tracker(config, source, Storage(":memory:"), [notifier]), source, notifier


def test_first_run_is_silent_then_only_new_items_notify():
    tracker, source, notifier = setup()
    source.listings = [make("1"), make("2")]
    tracker.check_all()
    assert notifier.sent == []

    source.listings = [make("3"), make("4", price=5000), make("5", title="RTX 3060"), make("1"), make("2")]
    result = tracker.check_all()[0]
    assert notifier.sent == ["3"]
    assert result.new == 1

    tracker.check_all()
    assert notifier.sent == ["3"]  # kein Doppel-Alarm


def test_empty_first_run_does_not_swallow_later_items():
    tracker, source, notifier = setup()
    tracker.check_all()
    source.listings = [make("9")]
    tracker.check_all()
    assert notifier.sent == ["9"]


def test_notify_on_first_run_oldest_first():
    tracker, source, notifier = setup(notify_on_first_run=True)
    source.listings = [make("new"), make("old")]  # eBay liefert neueste zuerst
    tracker.check_all()
    assert notifier.sent == ["old", "new"]


def test_source_error_is_reported_not_raised():
    tracker, source, notifier = setup()
    source.search = lambda s: (_ for _ in ()).throw(RuntimeError("boom"))
    (result,) = tracker.check_all()
    assert result.error == "boom"


def test_load_config_with_env(tmp_path, monkeypatch):
    monkeypatch.setenv("NTFY_TOPIC", "abc")
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    path = tmp_path / "c.yaml"
    path.write_text(textwrap.dedent("""
        notifiers:
          ntfy: {topic: "${NTFY_TOPIC}"}
          telegram: {bot_token: "${TELEGRAM_BOT_TOKEN}", chat_id: 1}
        searches:
          - {name: a, query: rtx 4070, max_price: 900, exclude: defekt}
    """))
    config = load_config(path)
    assert config.notifiers["ntfy"]["topic"] == "abc"
    assert config.notifiers["telegram"]["bot_token"] is None
    assert config.searches[0].exclude == ["defekt"]


def test_config_validation(tmp_path):
    path = tmp_path / "c.yaml"
    path.write_text("searches:\n  - {name: a, query: x, min_price: 10, max_price: 5}\n")
    with pytest.raises(ConfigError):
        load_config(path)


def test_example_config_is_valid():
    config = load_config("config.yaml")
    assert config.searches
