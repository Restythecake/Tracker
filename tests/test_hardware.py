"""Hardware-Erkennung und Preisbewertung, an echten Anzeigentiteln (Kleinanzeigen/eBay, 24.09.2026)."""
import pytest

from ebay_tracker.hardware import assess, assess_ram, extract_specs


@pytest.mark.parametrize("title, gpu, cpu, ram", [
    ("Gaming PC Ryzen 7 5700X, RTX 4070, 32GB RAM", "RTX 4070", "Ryzen 7 5700X", 32),
    ("GAMING PC | Intel Core i7-13700F | GeForce RTX 4070 12GB | 32GB DDR5 | 1TB NVMe SSD", "RTX 4070", "i7-13700F", 32),
    ("Gaming PC – Ryzen 5 5600 + RTX 3080 10GB | 16GB RAM | 512GB SSD", "RTX 3080", "Ryzen 5 5600", 16),
    ("High-End Gaming Pc | TUF RTX 3080 10gb | 5 5600X | TOP Zustand!", "RTX 3080", "Ryzen 5 5600X", None),
    ("Gaming PC | RTX 4070 Super | Ryzen 5 7500F | 1TB | 32GB DDR5", "RTX 4070 Super", "Ryzen 5 7500F", 32),
    ("High End Gaming PC Ryzen7 5700X RX 6800 16 GB VRAM 32 GB 1 TB m2", "RX 6800", "Ryzen 7 5700X", 32),
    ("Gaming High End PC RX7800XT 16gb / Ryzen 7 5700X3D", "RX 7800 XT", "Ryzen 7 5700X3D", None),
    ("High end Gaming pc rtx 4070 i7 13th", "RTX 4070", "i7 (13. Gen)", None),
    ("Gaming PC – RTX 4060 Ti 16GB | i5-13400F | 32GB DDR5", "RTX 4060 Ti", "i5-13400F", 32),
    ("Gaming PC i7 4770 GTX 1070 16GB", "GTX 1070", "i7-4770", 16),
    ("i9 14900K /32GB DDR5 6000MHz/2TB /AIO /RGB /High Gaming PC 4K", None, "i9-14900K", 32),
])
def test_extract(title, gpu, cpu, ram):
    s = extract_specs(title)
    assert (s.gpu, s.cpu, s.ram_gb) == (gpu, cpu, ram)


def test_good_deal_and_reasons():
    a = assess("Gaming PC RTX 4070 12GB i7-12700F 32GB DDR4 1TB NVMe be quiet 650W", 880)
    assert a.ok and a.verdict == "gut" and a.notify_worthy
    assert any("Marken-Netzteil" in p for p in a.pros)


@pytest.mark.parametrize("title, price, reason", [
    ("Gaming PC RTX 3060 i5-12400F 16GB", 600, "zu schwach"),
    ("Gaming PC GTX 1650 i5 16GB", 500, "zu schwach"),
    ("Gaming PC RTX 4070 i7-4770 16GB", 700, "bremst"),
    ("Gaming PC RTX 4070 Ryzen 5 5600 8GB RAM", 700, "RAM"),
    ("Suche Gaming PC RTX 4070", 900, "Gesuch"),
    ("Gaming Laptop RTX 4070 i7-13700H 32GB", 900, "Laptop"),
    ("Gaming PC ohne Grafikkarte i7-12700 32GB", 400, "ohne Grafikkarte"),
])
def test_rejections(title, price, reason):
    a = assess(title, price)
    assert not a.ok and reason in a.reason


def test_new_dealer_pc_is_rejected():
    a = assess("Gaming PC RTX 4070 Ryzen 7 5700X 32GB", 999, new=True, dealer=True)
    assert not a.ok and "neu" in a.reason


def test_overpriced_and_suspicious():
    assert assess("Gaming PC RTX 4070 Ryzen 5 5600 16GB", 1250).verdict == "teuer"
    sus = assess("Gaming PC RTX 5080 Ryzen 7 7800X3D 32GB", 700)
    assert sus.verdict == "verdaechtig" and sus.notify_worthy and "Vorkasse" in sus.cons[0]


def test_description_fills_gaps():
    a = assess("Gaming PC RTX 4070", 850, description="Prozessor: Intel Core i7-12700F\nArbeitsspeicher: 32 GB DDR4")
    assert a.specs.cpu == "i7-12700F" and a.specs.ram_gb == 32


def test_min_gpu_requirement():
    from ebay_tracker.hardware import requirements_from
    req = requirements_from("4070", None)
    assert not assess("Gaming PC RTX 3080 i7-12700 32GB", 700, req=req).ok
    assert assess("Gaming PC RTX 4070 Super i7-12700 32GB", 900, req=req).ok


# ------------------------------------------------------------------ RAM-Modus
def test_assess_ram_cheap_ddr4_is_a_deal():
    a = assess_ram("64 GB Crucial DDR4 3200 MHz RAM Kit 2x32GB", 90)
    assert a.ok and a.verdict == "top" and a.specs.ram_gb == 64 and a.fair_price == 140


def test_assess_ram_ddr5_speed_bonus():
    a = assess_ram("G.Skill Trident Z5 Neo 64GB DDR5-6000 CL30", 120)
    assert a.ok and a.specs.ram_gb == 64
    assert any("6000" in p for p in a.pros) and a.fair_price == 200


def test_assess_ram_rejects_pcs_and_small_kits():
    assert not assess_ram("Gaming PC RTX 4070 32GB RAM", 850).ok  # PC, kein RAM
    assert assess_ram("16 GB DDR4 Riegel", 25).ok  # 16 GB zählt
    a = assess_ram("8 GB DDR4 Stick", 10)
    assert not a.ok and "unter 16 GB" in a.reason


def test_assess_ram_server_ram_is_cheap_and_flagged():
    a = assess_ram("256 GB DDR4-3200 RDIMM ECC Server-RAM", 300)
    assert a.ok and a.specs.ram_gb == 256
    assert any("ECC" in c for c in a.cons)


def test_assess_ram_suspicious():
    a = assess_ram("64 GB DDR4 Kit", 45)
    assert a.verdict == "verdaechtig" and a.notify_worthy


# deal:-Schwelle in der Engine
def test_deal_threshold_keeps_normal_prices_silent():
    from nest_plugin.engine import Engine
    from nest_plugin.store import Store
    from ebay_tracker.sources import MultiSource
    from tests.test_plugin import FakeClient, FakeSource, item
    ebay = FakeSource()
    client = FakeClient()
    engine = Engine(Store(":memory:"), client, source_factory=lambda st: MultiSource({"ebay": ebay}),
                    pause=lambda s: None)
    engine.update_settings({"searches": ["thinkpad t14 -defekt"]}, trigger=False)
    engine.run_once()
    ebay.items = [item("ebay", 1, title="RTX 4070 PC i7-12700F 32GB DDR4 1TB", price=880),  # ueber Schwelle
                  item("ebay", 2, title="RTX 4070 PC i7-12700F 32GB DDR4 1TB be quiet 650W", price=700)]
    engine.update_settings({"searches": ['rtx 4070 bewertung:pc deal:0.8 -defekt']}, trigger=False)
    engine.run_once()  # neuer Suchname -> stiller erster Lauf
    engine.run_once()  # jetzt gilt deal:0.8
    shown = {m.listing.item_id: m.silent for m in engine.store.recent_matches(include_silent=True)}
    # 880 € = ratio 0.89 > 0.8 -> still in der Galerie; 700 € = 0.71 -> gemeldet
    assert shown == {"1": 1, "2": 0}, shown


@pytest.mark.parametrize("title", [
    "Acer TravelMate P259-G2-M 15,6\" FHD i5-7200U 4GB DDR4 128GB 500GB",
    "Mini PC intel nuc Intel I 3 7100u 8gb RAM ddr4 128gb M2 USB 3.0",
    "HP-250 G7 / 15.6“ FHD / 8GB DDR4 / 128GB SSD / Windows 11",
    "Tausche 128 GB Kingston DDR5-5600 4×32 GB gegen 2x64gb DDR5-6000",
])
def test_ram_mode_rejects_computers_and_swaps(title):
    """Die sieben Fehlalarme vom 25.09. 02:18."""
    assert not assess_ram(title, 60).ok


def test_ssd_after_ddr_is_not_ram():
    assert extract_specs("Laptop 8GB DDR4 128GB SSD").ram_gb == 8


def test_live_false_deals_from_2026_09_28_are_rejected():
    from ebay_tracker.hardware import assess_gpu, assess_ram
    for title in ("Asus Vivobook E410KA 128GB eMMC Intel N4500 4GB RAM DDR4 Pink Grade C",
                  "X99 Gaming Motherboard Für LGA 2011 3 Computer Motherboard 4 Kanal DDR4 128GB M",
                  'Dell Precision 7560 64GB DDR4 Nvidia RTXA4000 15,6" TOP ZUSTAND'):
        assert not assess_ram(title, 100, min_gb=64).ok, title
    for title in ("Ankauf RTX 3090‼️", "NVIDIA NVLink Bridge 3-Slot GPU Interconnect for RTX 3090 A5000",
                  "MSI GeForce RTX 3090 SUPRIM X 24G - meist funktionsfähig, frisch neu verklebt, kleiner VRAM"):
        assert not assess_gpu(title, 400).ok, title
    assert assess_ram("Corsair Vengeance RGB PRO DDR4 64GB (4x16GB) 3200MHz", 90, min_gb=64).verdict == "top"
    assert assess_gpu("Rx 7900 XTX Spectral White Hellhound 24GB", 600).ok
