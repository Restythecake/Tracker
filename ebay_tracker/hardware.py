"""Liest die verbaute Hardware aus einer PC-Anzeige und bewertet den Preis.

Ziel: "RTX 4070, i7-12700F, 32 GB für 850 €" von "GTX 1650 für 500 €" und von
"4070-PC vom Händler, neu, 0 % Finanzierung, 1.399 €" unterscheiden.

Zwei Teile:

* ``extract_specs``: Regex-Erkennung von GPU, CPU, RAM, SSD, Netzteil aus
  Titel und (falls vorhanden) Beschreibung. Der Titel hat Vorrang -- in
  Beschreibungen stehen oft Vergleiche ("schneller als eine 3060").
* ``assess``: fairer Gebrauchtpreis = Summe grober Teilepreise, dazu Leistungs-
  mindestwerte und Warnzeichen. Heraus kommt ein Urteil mit Pro/Contra.

Die Preis- und Leistungstabellen sind **Schätzungen** für den deutschen
Gebrauchtmarkt (Stand Herbst 2026, am 25.09. gegen Kleinanzeigen-Angebote
kalibriert: Karten mit viel VRAM -- 3090, 4090, 50er -- sind wegen KI-Nachfrage
weit teurer als ihre Spieleleistung vermuten lässt) und bewusst an einer Stelle gesammelt, damit
man sie nachziehen kann. Die Leistungsindizes sind grob 1440p-Raster relativ zur
RTX 4070 (=100) bzw. Gaming-CPU-Leistung relativ zum i7-12700 (=100).
Zusätzlich vergleicht der Tracker mit den Preisen, die er selbst am Markt
beobachtet (siehe ``market_note``).
"""
from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from statistics import median
from typing import Any

# --------------------------------------------------------------------- GPUs
# key: (Anzeigename, Leistungsindex, Gebrauchtpreis € für die Karte allein)
GPUS: dict[str, tuple[str, int, int]] = {
    # NVIDIA RTX 50
    "5090": ("RTX 5090", 230, 4000), "5080": ("RTX 5080", 160, 1300), "5070ti": ("RTX 5070 Ti", 135, 950),
    "5070": ("RTX 5070", 108, 480), "5060ti": ("RTX 5060 Ti", 82, 340), "5060": ("RTX 5060", 70, 260),
    # RTX 40
    "4090": ("RTX 4090", 190, 2300), "4080super": ("RTX 4080 Super", 150, 920), "4080": ("RTX 4080", 148, 800),
    "4070tisuper": ("RTX 4070 Ti Super", 125, 740), "4070ti": ("RTX 4070 Ti", 118, 550),
    "4070super": ("RTX 4070 Super", 112, 580), "4070": ("RTX 4070", 100, 450),
    "4060ti": ("RTX 4060 Ti", 78, 280), "4060": ("RTX 4060", 64, 220),
    # RTX 30
    "3090ti": ("RTX 3090 Ti", 112, 1250), "3090": ("RTX 3090", 105, 1100), "3080ti": ("RTX 3080 Ti", 102, 450),
    "3080": ("RTX 3080", 95, 360), "3070ti": ("RTX 3070 Ti", 80, 290), "3070": ("RTX 3070", 75, 250),
    "3060ti": ("RTX 3060 Ti", 68, 220), "3060": ("RTX 3060", 55, 190), "3050": ("RTX 3050", 38, 130),
    # RTX 20 / GTX
    "2080ti": ("RTX 2080 Ti", 72, 260), "2080super": ("RTX 2080 Super", 62, 210), "2080": ("RTX 2080", 60, 190),
    "2070super": ("RTX 2070 Super", 57, 180), "2070": ("RTX 2070", 52, 150),
    "2060super": ("RTX 2060 Super", 50, 140), "2060": ("RTX 2060", 42, 110),
    "1660super": ("GTX 1660 Super", 36, 100), "1660ti": ("GTX 1660 Ti", 35, 100), "1660": ("GTX 1660", 32, 85),
    "1650super": ("GTX 1650 Super", 28, 80), "1650": ("GTX 1650", 22, 70),
    "1080ti": ("GTX 1080 Ti", 55, 150), "1080": ("GTX 1080", 45, 110), "1070ti": ("GTX 1070 Ti", 40, 95),
    "1070": ("GTX 1070", 35, 80), "1060": ("GTX 1060", 25, 55), "1050ti": ("GTX 1050 Ti", 16, 45),
    # AMD
    "rx9070xt": ("RX 9070 XT", 140, 600), "rx9070": ("RX 9070", 125, 520), "rx9060xt": ("RX 9060 XT", 80, 300),
    "rx7900xtx": ("RX 7900 XTX", 160, 800), "rx7900xt": ("RX 7900 XT", 135, 600), "rx7900gre": ("RX 7900 GRE", 115, 480),
    "rx7800xt": ("RX 7800 XT", 108, 420), "rx7700xt": ("RX 7700 XT", 92, 330), "rx7600xt": ("RX 7600 XT", 64, 250),
    "rx7600": ("RX 7600", 60, 210),
    "rx6950xt": ("RX 6950 XT", 112, 450), "rx6900xt": ("RX 6900 XT", 105, 400), "rx6800xt": ("RX 6800 XT", 100, 360),
    "rx6800": ("RX 6800", 88, 300), "rx6750xt": ("RX 6750 XT", 72, 250), "rx6700xt": ("RX 6700 XT", 68, 230),
    "rx6700": ("RX 6700", 62, 200), "rx6650xt": ("RX 6650 XT", 56, 190), "rx6600xt": ("RX 6600 XT", 54, 170),
    "rx6600": ("RX 6600", 48, 150), "rx6500xt": ("RX 6500 XT", 25, 80), "rx5700xt": ("RX 5700 XT", 48, 120),
    "rx5700": ("RX 5700", 43, 100), "rx580": ("RX 580", 22, 50), "rx570": ("RX 570", 18, 40),
    # Intel
    "arcb580": ("Arc B580", 62, 220), "arca770": ("Arc A770", 58, 180), "arca750": ("Arc A750", 52, 140),
}
# Größter VRAM, den es für die Karte gibt (Default 12). Eine Zahl direkt hinter
# der GPU, die größer ist, kann kein VRAM sein: "GTX 1070 16GB" meint den RAM.
MAX_VRAM = {
    "5090": 32, "5080": 16, "5070ti": 16, "5060ti": 16, "4090": 24, "4080super": 16, "4080": 16,
    "4070tisuper": 16, "4060ti": 16, "3090ti": 24, "3090": 24, "rx9070xt": 16, "rx9070": 16, "rx9060xt": 16,
    "rx7900xtx": 24, "rx7900xt": 20, "rx7900gre": 16, "rx7800xt": 16, "rx7600xt": 16, "rx6950xt": 16,
    "rx6900xt": 16, "rx6800xt": 16, "rx6800": 16, "arca770": 16,
    "1060": 6, "1070": 8, "1070ti": 8, "1080": 8, "1080ti": 11, "1650": 4, "1660": 6, "1660super": 6,
    "1660ti": 6, "2060": 6, "2070": 8, "2070super": 8, "2080": 8, "2080super": 8, "2080ti": 11,
    "3050": 8, "3060ti": 8, "3070": 8, "3070ti": 8, "4060": 8, "rx580": 8, "rx570": 8, "rx6600": 8,
    "rx6600xt": 8, "rx6650xt": 8, "rx5700": 8, "rx5700xt": 8, "rx7600": 8,
}
_NV_NUMBERS = sorted({k[:4] for k in GPUS if k[:4].isdigit()})

# ------------------------------------------------------------------ RAM etc.
RAM_SIZES = (8, 12, 16, 24, 32, 48, 64, 96, 128, 192, 256)
# Gebrauchtpreise je GB, Stand Herbst 2026: DDR4 läuft aus und ist billig,
# DDR5 (6000er) liegt deutlich darüber. Server-RAM (ECC/RDIMM) ist fast nichts wert.
RAM_EUR_PER_GB_DDR4 = 2.2
RAM_EUR_PER_GB_DDR5 = 3.2
RAM_EUR_PER_GB_SERVER = 0.8
BASE_REST = 170  # Gehäuse, Netzteil, Kühler, Mainboard-Grundwert ohne CPU-Plattform
SSD_EUR_PER_TB = 40

GOOD_PSU = ("be quiet", "bequiet", "seasonic", "corsair rm", "corsair hx", "corsair sf", "msi mag a", "fsp hydro",
            "super flower", "enermax revolution", "thermaltake toughpower", "asus rog thor", "nzxt c")
BAD_PSU = ("xilence", "lc-power", "lc power", "inter-tech", "intertech", "aerocool", "kolink", "sharkoon shp",
           "no name", "noname", "no-name", "tronje")

REJECT_TERMS = {
    r"\bdefekt": "defekt", r"\bbastler": "Bastlerware", r"\bgesucht?\b|\bsuche\b": "Gesuch",
    r"\btausch": "Tausch", r"ohne\s+(grafikkarte|gpu|graka)": "ohne Grafikkarte",
    r"\b(laptop|notebook)\b": "Laptop", r"\bmining|\bminer\b": "Mining-Rig",
    r"\bmiete|\bmieten\b|\bvermiet": "Miete", r"nur\s+(gehäuse|gehaeuse|tower|mainboard|netzteil)": "nur Einzelteil",
    r"\bbarebone": "Barebone",
}


@dataclass
class Specs:
    gpu: str | None = None
    gpu_index: int | None = None
    gpu_price: int | None = None
    cpu: str | None = None
    cpu_score: int | None = None
    ram_gb: int | None = None
    ssd_tb: float | None = None
    psu_w: int | None = None
    psu_quality: str | None = None  # "gut" | "billig" | None
    new: bool | None = None  # laut Detailseite "Zustand Neu"
    dealer: bool = False

    def short(self) -> str:
        parts = [self.gpu, self.cpu, f"{self.ram_gb} GB" if self.ram_gb else None,
                 _tb(self.ssd_tb) if self.ssd_tb else None]
        return " · ".join(p for p in parts if p)


def _tb(tb: float) -> str:
    return f"{tb:g} TB".replace(".", ",") if tb >= 1 else f"{int(tb * 1000)} GB SSD"


@dataclass
class Assessment:
    ok: bool  # erfüllt die Mindestanforderungen der Suche
    verdict: str  # top | gut | fair | teuer | verdaechtig | ungeeignet
    reason: str | None  # warum nicht ok
    fair_price: int | None
    ratio: float | None  # Preis / fairer Preis
    specs: Specs
    pros: list[str] = field(default_factory=list)
    cons: list[str] = field(default_factory=list)
    market: str | None = None
    risk: int | None = None
    trip_km: int | None = None
    trip_cost: int | None = None
    summary: str | None = None  # sonst specs.short()

    @property
    def label(self) -> str:
        return VERDICT_LABELS.get(self.verdict, self.verdict)

    @property
    def notify_worthy(self) -> bool:
        return self.ok and self.verdict in ("top", "gut", "verdaechtig")

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["label"] = self.label
        d["summary"] = self.summary or self.specs.short()
        return d


VERDICT_LABELS = {
    "top": "Top-Deal", "gut": "Guter Preis", "fair": "Fairer Preis", "teuer": "Zu teuer",
    "verdaechtig": "Zu gut um wahr zu sein?", "ungeeignet": "Passt nicht",
}


# ---------------------------------------------------------------- extraction
def _norm(text: str) -> str:
    t = text.lower().replace("\u00a0", " ")
    t = re.sub(r"[|/,;()\[\]*+]", " ", t)
    return re.sub(r"\s+", " ", t)


def find_gpu(text: str) -> tuple[str, tuple[int, int]] | None:
    """Erste GPU im Text: (Schlüssel, Span). Span, damit RAM-Suche die VRAM-Angabe überspringt."""
    t = text
    best = None
    # AMD: nur mit "rx"/"radeon", sonst wäre "7800x3d" eine Grafikkarte
    for m in re.finditer(r"\b(?:radeon\s*)?rx\s*-?\s*(\d{3,4})\s*(xtx|xt|gre)?\b", t):
        key = f"rx{m.group(1)}{m.group(2) or ''}"
        if key in GPUS:
            best = min(best, (m.start(), key, m.span()), key=lambda x: x[0]) if best else (m.start(), key, m.span())
    for m in re.finditer(r"\barc\s*([ab]\d{3})\b", t):
        key = f"arc{m.group(1)}"
        if key in GPUS and (not best or m.start() < best[0]):
            best = (m.start(), key, m.span())
    nv = r"(?:\b(?:rtx|gtx|geforce(?:\s*rtx|\s*gtx)?)\s*-?\s*|\b)(" + "|".join(_NV_NUMBERS) + r")" \
         r"(?!\d|\s*x3d|x\b|k\b|f\b)\s*-?\s*(ti\s*super|tisuper|ti|super|s\b)?"
    for m in re.finditer(nv, t):
        prefix = t[max(0, m.start() - 12):m.start()]
        if re.search(r"\b(i[3579]|ryzen\s*\d|core)\s*-?\s*$", prefix):
            continue  # "i7 4770", "Ryzen 5 1600" usw. sind CPUs
        if re.search(r"(mhz|mt s|mts|watt|w)\s*$", t[m.end():m.end() + 5]):
            continue
        suffix = (m.group(2) or "").replace(" ", "")
        suffix = {"s": "super"}.get(suffix, suffix)
        key = m.group(1) + suffix
        if key not in GPUS:
            key = m.group(1)
        if key in GPUS and (not best or m.start() < best[0]):
            best = (m.start(), key, m.span())
    return (best[1], best[2]) if best else None


def _intel_score(tier: int, gen: int, suffix: str) -> int:
    k = "k" in suffix
    if gen >= 13:
        table = {3: 65, 5: 100 if k else 92, 7: 112, 9: 118}
    elif gen == 12:
        table = {3: 60, 5: 95 if k else 85, 7: 100, 9: 108}
    elif gen == 11:
        table = {3: 50, 5: 70, 7: 78, 9: 80}
    elif gen == 10:
        table = {3: 45, 5: 65, 7: 75, 9: 80}
    elif gen >= 8:
        table = {3: 35, 5: 55, 7: 65, 9: 70}
    elif gen >= 6:
        table = {3: 25, 5: 40, 7: 48, 9: 50}
    else:
        table = {3: 18, 5: 28, 7: 32, 9: 32}
    return table.get(tier, 50)


def _ryzen_score(tier: int, model: int, suffix: str) -> int:
    series = model // 1000
    x3d = "x3d" in suffix
    apu = suffix.startswith("g")
    if series >= 9:
        s = {5: 110, 7: 115, 9: 120}.get(tier, 110) + (25 if x3d else 0)
    elif series >= 7:
        s = {5: 100, 7: 105, 9: 108}.get(tier, 100) + (25 if x3d else 0)
    elif series == 8:  # 8000G-APUs
        s = 90
    elif series == 5:
        s = {3: 65, 5: 85, 7: 90, 9: 92}.get(tier, 85)
        if x3d:
            s = 105 if model >= 5800 else 100
        if model in (5500, 5600) and apu:
            s = 75
        elif apu:
            s = 80
    elif series == 3:
        s = {3: 50, 5: 60, 7: 65, 9: 68}.get(tier, 60)
    else:
        s = {3: 35, 5: 40, 7: 45}.get(tier, 40)
    return s


def find_cpu(text: str) -> tuple[str, int] | None:
    t = text
    m = re.search(r"\bcore\s*ultra\s*([579])\s*-?\s*(2\d\d)([a-z]{0,2})\b", t)
    if m:
        tier = int(m.group(1))
        return f"Core Ultra {tier} {m.group(2)}{m.group(3).upper()}", {5: 100, 7: 110, 9: 115}[tier]
    m = re.search(r"\bi([3579])\s*-?\s*(\d{4,5})([a-z]{0,2})\b", t)
    if m:
        tier, num, suffix = int(m.group(1)), m.group(2), m.group(3)
        gen = int(num[:2]) if len(num) == 5 else int(num[0])
        return f"i{tier}-{num}{suffix.upper()}", _intel_score(tier, gen, suffix)
    m = re.search(r"\bi([3579])\b[^0-9]{0,12}?(\d{1,2})\s*(?:th\b|te\b|\.?\s*gen(?:eration)?\b)", t)
    if m:
        tier, gen = int(m.group(1)), int(m.group(2))
        return f"i{tier} ({gen}. Gen)", _intel_score(tier, gen, "")
    m = re.search(r"\bryzen\s*([3579])?\s*-?\s*(\d{4})\s*(x3d|xt|x|g|ge|f|gt)?\b", t) or \
        re.search(r"(?<!rx )(?<!rx)\b()([1-9][05-9]\d0)(x3d|x|f)\b", t)
    if m:
        model = int(m.group(2))
        tier = int(m.group(1)) if m.group(1) else {5: 5, 6: 5, 7: 7, 8: 7, 9: 9}.get(model // 100 % 10, 5)
        suffix = (m.group(3) or "").lower()
        return f"Ryzen {tier} {model}{suffix.upper()}", _ryzen_score(tier, model, suffix)
    m = re.search(r"\bi([3579])\b", t)
    if m:
        return f"i{m.group(1)} (Gen. unklar)", None
    m = re.search(r"\bryzen\s*([3579])\b", t)
    if m:
        return f"Ryzen {m.group(1)} (Modell unklar)", None
    return None


def find_ram(text: str, gpu_span: tuple[int, int] | None, max_vram: int = 12) -> int | None:
    t = text
    if gpu_span:
        # "RTX 4060 Ti 16GB" -- der Speicher direkt hinter der GPU ist VRAM
        tail = re.match(r"\s*-?\s*(?:ti\s*super|super|ti|xtx|xt|gre)?\s*(\d{1,2})\s*gb"
                        r"(?!\s*(?:ram|ddr|arbeitsspeicher))(?:\s*(?:vram|gddr\d?x?))?", t[gpu_span[1]:])
        if tail and int(tail.group(1)) > max_vram:
            tail = None
        end = gpu_span[1] + (tail.end() if tail else 0)
        t = t[:gpu_span[0]] + " " + t[end:]
    t = re.sub(r"\d{1,2}\s*gb\s*(vram|gddr\d?x?)", " ", t)
    m = re.search(r"\b(\d)\s*x\s*(8|16|32|48)\s*gb", t)
    if m:
        return int(m.group(1)) * int(m.group(2))
    keyed = [int(x) for x in re.findall(r"\b(\d{1,3})\s*gb\s*(?:ddr[345]|ram|arbeitsspeicher|speicher\b)", t)]
    # "8GB DDR4 128GB SSD": die Zahl hinter "ddr4" gehört hier zur SSD, nicht zum RAM
    keyed += [int(x) for x in re.findall(
        r"\b(?:ddr[345]|ram|arbeitsspeicher)\s*:?\s*(\d{1,3})\s*gb(?!\s*(?:ssd|nvme|m\.?2|hdd|emmc|festplatte|speicherplatz))", t)]
    keyed += [int(x) for x in re.findall(r"\b(\d{2,3})\s*(?:gb\s*)?(?:ddr[345]\s*)?ram\b", t)]
    keyed = [x for x in keyed if x in RAM_SIZES]
    if keyed:
        return max(keyed)
    loose = [int(x) for x in re.findall(r"\b(\d{1,3})\s*gb\b(?!\s*(?:ssd|nvme|m\.?2|hdd|festplatte))", t)]
    loose = [x for x in loose if x in RAM_SIZES and x >= 16]
    return max(loose) if loose else None


def find_ssd(text: str) -> float | None:
    tb = [float(x.replace(",", ".")) for x in re.findall(r"\b(\d(?:[.,]\d)?)\s*tb\b", text)]
    gb = [int(x) / 1000 for x in re.findall(r"\b(\d{3,4})\s*gb\s*(?:m\.?2|nvme|ssd)", text)]
    vals = [v for v in tb + gb if 0.1 <= v <= 16]
    return sum(vals) if vals else None


def find_psu(text: str) -> tuple[int | None, str | None]:
    watts = [int(x) for x in re.findall(r"\b(\d{3,4})\s*(?:w|watt)\b", text) if 300 <= int(x) <= 1600]
    quality = None
    if any(b in text for b in BAD_PSU):
        quality = "billig"
    elif any(g in text for g in GOOD_PSU):
        quality = "gut"
    return (max(watts) if watts else None), quality


def extract_specs(title: str, description: str = "") -> Specs:
    tt, dd = _norm(title), _norm(description)
    specs = Specs()
    gpu = find_gpu(tt)
    gpu_text = tt
    if not gpu and dd:
        gpu = find_gpu(dd)
        gpu_text = dd
    if gpu:
        specs.gpu, specs.gpu_index, specs.gpu_price = GPUS[gpu[0]]
    cpu = find_cpu(tt) or (find_cpu(dd) if dd else None)
    if cpu:
        specs.cpu, specs.cpu_score = cpu
    vram = MAX_VRAM.get(gpu[0], 12) if gpu else 12
    specs.ram_gb = find_ram(tt, gpu[1] if gpu and gpu_text is tt else None, vram) or \
        (find_ram(dd, gpu[1] if gpu and gpu_text is dd else None, vram) if dd else None)
    specs.ssd_tb = find_ssd(tt) or (find_ssd(dd) if dd else None)
    specs.psu_w, specs.psu_quality = find_psu(tt + " " + dd)
    return specs


# ----------------------------------------------------------------- assessment
def platform_value(cpu_score: int | None) -> int:
    """CPU + Mainboard (+ Plattform-Aufpreis DDR5) als grober Gebrauchtwert."""
    if cpu_score is None:
        return 110
    if cpu_score >= 120:
        return 380
    if cpu_score >= 100:
        return 260
    if cpu_score >= 85:
        return 180
    if cpu_score >= 70:
        return 130
    return 80


def ram_kind(text: str) -> str:
    """ddr4 | ddr5 | server | ddr3."""
    t = text.lower()
    if "ecc" in t or "rdimm" in t or "server" in t:
        return "server"
    if re.search(r"\bddr5\b|\bddr5\b", t):
        return "ddr5"
    if re.search(r"\bddr4\b", t):
        return "ddr4"
    if re.search(r"\bddr3\b", t):
        return "ddr3"
    return "ddr4"  # ohne Angabe: konservativ DDR4 annehmen


def ram_price_per_gb(kind: str) -> float:
    return {"ddr5": RAM_EUR_PER_GB_DDR5, "ddr4": RAM_EUR_PER_GB_DDR4, "server": RAM_EUR_PER_GB_SERVER,
            "ddr3": 0.5}[kind]


# GPU-Name -> beobachteter Marktpreis (ebay_tracker.market). Wird vom Plugin
# vor jedem Lauf gesetzt; leer = Tabellenwerte.
GPU_MARKET: dict[str, float] = {}


def market_key(gpu_name: str) -> str:
    """Anzeigename -> Tabellenschlüssel ("RTX 3090" -> "3090")."""
    return next((k for k, v in GPUS.items() if v[0] == gpu_name), "")


def gpu_value(specs: "Specs") -> float | None:
    if specs.gpu is None:
        return None
    return GPU_MARKET.get(specs.gpu) or specs.gpu_price


def fair_price(specs: Specs, *, text: str = "") -> int | None:
    if not specs.gpu_price:
        return None
    ram = (specs.ram_gb or 16) * ram_price_per_gb(ram_kind(text))
    gpu = gpu_value(specs) or specs.gpu_price
    ssd = (specs.ssd_tb or 0.5) * SSD_EUR_PER_TB
    return int(round(gpu + platform_value(specs.cpu_score) + ram + BASE_REST + ssd, -1))


@dataclass
class Requirements:
    min_gpu_index: int = 88  # RX 6800 / RTX 3080-Klasse und besser
    min_cpu_score: int = 65  # i7 8./9. Gen, Ryzen 7 3700X -- darunter bremst es eine 4070 spürbar
    min_ram_gb: int = 16
    want_ram_gb: int = 32
    allow_new: bool = False


def requirements_from(min_gpu: str | None, min_ram: int | None) -> Requirements:
    req = Requirements()
    if min_gpu:
        key = find_gpu(_norm(min_gpu))
        if key:
            req.min_gpu_index = GPUS[key[0]][1]
    if min_ram:
        req.min_ram_gb = min_ram
    return req


def assess_ram(title: str, price: float | None, *, description: str = "",
               market: list[float] | None = None, min_gb: int = 16) -> Assessment:
    """Einzelne RAM-Kits: fairer Wert aus Groesse x Generation.

    Arthur will 64-GB-Kits deutlich unter Marktpreis. "Unter dem Markt" ist
    konservativ: unter 65 % des ueblichen Gebrauchtpreises ist es ein Deal.
    """
    if price is not None and price < 10:
        price = None  # 1 €-Platzhalter ("VB", "Tausch") sind kein Preis
    specs = extract_specs(title, description)
    text = _norm(title + " " + description)
    kind = ram_kind(text)
    t = _norm(title)
    for pattern, label in REJECT_TERMS.items():
        if re.search(pattern, t):
            return Assessment(False, "ungeeignet", label, None, None, specs)
    # Ganze Rechner mit viel RAM gehören in die PC-Suche, nicht hierher
    if specs.cpu or re.search(r"\b(?:mini\s*pc|nuc|thinkpad|latitude|elitebook|macbook|imac|mac\s*pro|"
                              r"workstation|tower|desktop\s*pc|all.in.one|windows\s*1[01]|ssd)\b", t):
        return Assessment(False, "ungeeignet", "ganzer Rechner, kein RAM-Kit", None, None, specs)
    if specs.ram_gb is None or specs.ram_gb < min_gb:
        return Assessment(False, "ungeeignet", f"RAM-Groesse nicht erkennbar oder unter {min_gb} GB",
                          None, None, specs)
    if specs.gpu is not None:
        # Ein PC, kein RAM-Einzelteil -- der gehoert in die PC-Suche
        return Assessment(False, "ungeeignet", "verbauter PC, kein RAM-Einzelteil", None, None, specs)
    per_gb = ram_price_per_gb(kind)
    fair = int(round(specs.ram_gb * per_gb, -1))
    specs.cpu = None
    summary = f"{specs.ram_gb} GB {kind.upper()}"
    pros = [f"{specs.ram_gb} GB {kind.upper()}"]
    cons: list[str] = []
    if kind == "ddr5":
        speed = re.search(r"(\d{4,5})\s*(?:mhz|mt)|ddr5-?(\d{4,5})", text)
        speed_val = next((g for g in speed.groups() if g), None) if speed else None
        if speed_val and int(speed_val) >= 6000:
            pros.append(f"{speed_val} MHz - schnell")
        if re.search(r"cl\s*30\b|cl\s*32\b", text):
            pros.append("niedrige Latenz (CL30/32)")
    if re.search(r"\b(ecc|rdimm)\b", text):
        cons.append("ECC/Server-RAM - laeuft nicht in jedem Desktop-Board")
    if "so-dimm" in text or "sodimm" in text:
        pros.append("SO-DIMM (Laptop)")
    if price is None or price <= 0:
        a = Assessment(True, "fair", None, fair, None, specs, pros, cons + ["Preis fehlt (VB?)"])
        a.market = market_note(price, market)
        return a
    ratio = price / fair
    if price < fair * 0.40:
        verdict = "verdaechtig"
        cons.insert(0, "unrealistisch billig - Betrugs- oder Defektverdacht")
    elif ratio <= 0.65:
        verdict = "top"
    elif ratio <= 0.85:
        verdict = "gut"
    elif ratio <= 1.0:
        verdict = "fair"
    else:
        verdict = "teuer"
    diff = int(round((ratio - 1) * 100))
    (pros if diff <= 0 else cons).append(f"{abs(diff)} % {'unter' if diff <= 0 else 'ueber'} Marktwert (~{fair} EUR)")
    a = Assessment(True, verdict, None, fair, round(ratio, 3), specs, pros, cons)
    a.market = market_note(price, market)
    a.summary = summary
    return a


def assess_gpu(title: str, price: float | None, *, description: str = "",
               req: "Requirements | None" = None, new: bool | None = None) -> Assessment:
    """Einzelne Grafikkarte gegen ihren Marktwert (vor allem für Vinted/Einzelanzeigen)."""
    req = req or Requirements()
    if price is not None and price < 10:
        price = None
    specs = extract_specs(title, description)
    specs.new = new
    t = _norm(title)
    for pattern, label in REJECT_TERMS.items():
        if re.search(pattern, t):
            return Assessment(False, "ungeeignet", label, None, None, specs)
    if re.search(r"\bgaming\s*pc\b|\bpc\b.*\b(ryzen|i[579])\b|k(?:ü|ue)hlk(?:ö|oe)rper|keine\s*karte|"
                 r"heatsink|backplate\b|nur\s*(?:k(?:ü|ue)hler|box|karton)", t):
        return Assessment(False, "ungeeignet", "keine einzelne Grafikkarte", None, None, specs)
    if specs.gpu is None:
        return Assessment(False, "ungeeignet", "keine Grafikkarte erkennbar", None, None, specs)
    if specs.gpu_index < req.min_gpu_index:
        return Assessment(False, "ungeeignet", f"{specs.gpu} zu schwach", None, None, specs)
    value = gpu_value(specs)
    fair = int(round(value, -1))
    src = "Markt" if specs.gpu in GPU_MARKET else "Schätzung"
    pros = [f"{specs.gpu} (~{specs.gpu_index} % einer 4070)"]
    cons: list[str] = []
    if new:
        cons.append("als neu gelistet")
    if price is None or price <= 0:
        return Assessment(True, "fair", None, fair, None, specs, pros, cons + ["Preis fehlt"])
    ratio = price / value
    if ratio < 0.55:
        verdict = "verdaechtig"
        cons.insert(0, "sehr weit unter Wert – nur mit Käuferschutz/Abholung kaufen")
    elif ratio <= 0.8:
        verdict = "top"
    elif ratio <= 0.92:
        verdict = "gut"
    elif ratio <= 1.05:
        verdict = "fair"
    else:
        verdict = "teuer"
    diff = int(round((ratio - 1) * 100))
    (pros if diff <= 0 else cons).append(f"{abs(diff)} % {'unter' if diff <= 0 else 'über'} {src} (~{fair} €)")
    return Assessment(True, verdict, None, fair, round(ratio, 3), specs, pros, cons)


def assess(title: str, price: float | None, *, description: str = "", req: Requirements | None = None,
           new: bool | None = None, dealer: bool = False, market: list[float] | None = None) -> Assessment:
    req = req or Requirements()
    if price is not None and price < 10:
        price = None
    specs = extract_specs(title, description)
    specs.new, specs.dealer = new, dealer
    text = _norm(title + " " + description)
    pros: list[str] = []
    cons: list[str] = []

    def reject(reason: str, verdict: str = "ungeeignet") -> Assessment:
        return Assessment(False, verdict, reason, fair_price(specs, text=text), None, specs, pros, cons)

    for pattern, label in REJECT_TERMS.items():
        if re.search(pattern, _norm(title)):
            return reject(label)
    if new and not req.allow_new:
        return reject("neu (Händler-PC)")
    if specs.gpu is None:
        return reject("keine Grafikkarte erkennbar")
    if specs.gpu_index < req.min_gpu_index:
        return reject(f"{specs.gpu} zu schwach")

    # --- Leistung
    tier = "stark" if specs.gpu_index >= 110 else "gut" if specs.gpu_index >= 95 else "ok"
    pros.append(f"{specs.gpu} ({tier}, ~{specs.gpu_index} % einer 4070)")
    if specs.cpu_score is None:
        cons.append(f"CPU {specs.cpu or 'nicht angegeben'} – Generation prüfen")
    elif specs.cpu_score < req.min_cpu_score:
        return reject(f"{specs.cpu} bremst die {specs.gpu}")
    elif specs.cpu_score < 85:
        cons.append(f"{specs.cpu} ist älter – bremst in manchen Spielen")
    else:
        pros.append(f"{specs.cpu} passt dazu")
    if specs.ram_gb is None:
        cons.append("RAM nicht angegeben")
    elif specs.ram_gb < req.min_ram_gb:
        return reject(f"nur {specs.ram_gb} GB RAM")
    elif specs.ram_gb >= req.want_ram_gb:
        pros.append(f"{specs.ram_gb} GB RAM")
    else:
        cons.append(f"nur {specs.ram_gb} GB RAM (Aufrüsten ~50 €)")
    if specs.ssd_tb is not None:
        (pros if specs.ssd_tb >= 1 else cons).append(f"SSD {_tb(specs.ssd_tb)}")
    if specs.psu_quality == "billig":
        cons.append("Billig-Netzteil")
    elif specs.psu_quality == "gut":
        pros.append("Marken-Netzteil")
    if specs.psu_w and specs.gpu_index >= 95 and specs.psu_w < 550:
        cons.append(f"Netzteil nur {specs.psu_w} W")
    if dealer:
        cons.append("gewerblicher Verkäufer")
    if re.search(r"finanzierung|ratenkauf|\b0\s*%", text):
        cons.append("Händler mit Finanzierung – meist Aufpreis")
    if re.search(r"\bovp\b|\bneu\b(?!wertig)", _norm(title)):
        cons.append("als „neu“ beworben")

    # --- Preis
    fair = fair_price(specs, text=text)
    if price is None or price <= 0 or fair is None:
        a = Assessment(True, "fair", None, fair, None, specs, pros, cons + ["Preis fehlt (VB?)"])
        a.market = market_note(price, market)
        return a
    ratio = price / fair
    # Unter ~60 % des Teilewerts ist es auf Kleinanzeigen fast immer ein
    # Fake-Angebot (Vorkasse-Betrug) oder etwas stimmt nicht -- trotzdem melden,
    # aber deutlich: ein echter Glücksfall ist genau das, was man sehen will.
    if price < (gpu_value(specs) or specs.gpu_price) * 0.8 or ratio < 0.62:
        verdict = "verdaechtig"
        cons.insert(0, "sehr weit unter Wert – nur ansehen und bei Abholung bar zahlen, nie Vorkasse")
    elif ratio <= 0.85:
        verdict = "top"
    elif ratio <= 1.0:
        verdict = "gut"
    elif ratio <= 1.12:
        verdict = "fair"
    else:
        verdict = "teuer"
    diff = int(round((ratio - 1) * 100))
    pros_or_cons = pros if diff <= 0 else cons
    pros_or_cons.append(f"{abs(diff)} % {'unter' if diff <= 0 else 'über'} Teilewert (~{fair} €)")
    a = Assessment(True, verdict, None, fair, round(ratio, 3), specs, pros, cons)
    a.market = market_note(price, market)
    return a


def market_note(price: float | None, market: list[float] | None) -> str | None:
    """Vergleich mit dem, was der Tracker selbst an PCs mit derselben GPU gesehen hat."""
    if not price or not market or len(market) < 4:
        return None
    med = median(market)
    diff = int(round((price / med - 1) * 100))
    word = "unter" if diff < 0 else "über"
    return f"{abs(diff)} % {word} Markt ({len(market)} Angebote, Median {int(med)} €)"
