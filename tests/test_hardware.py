"""Hardware-Erkennung und Preisbewertung, an echten Anzeigentiteln (Kleinanzeigen/eBay, 24.09.2026)."""
import pytest

from ebay_tracker.hardware import assess, extract_specs


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
