"""Die iframe-Oberfläche: serverseitig gerendertes HTML, kein Build-Schritt.

Randbedingungen aus Nests Sandbox (PLUGIN-API §7), die das Design bestimmen:

* Nest reicht auf dem Plugin-Host nur GET/HEAD durch. Die Oberfläche liest also
  nur; Ansicht und Filter sind Query-Parameter, Schreiben geht über Nests
  Einstellungsformular und die Aktionsknöpfe.
* ``img-src 'self' data:`` -- Bilder von eBay/Kleinanzeigen laufen über ``/img/<key>``.
* Kein ``allow-popups``, und ``nest:open`` darf nur die *eigene* Adresse öffnen.
  Ein Klick auf ein Angebot öffnet deshalb ``/go/<key>`` im neuen Tab, und diese
  Seite leitet zum Marktplatz weiter.
* Zwei Adressen (Arthur 2026-09-25): die Plugin-Subdomain (``plugin-arthur-tracker.
  …``) und Nests eigener Host unter ``/plugins/frame/arthur-tracker/…`` -- der
  Pfad ist der Fallback, wenn die Subdomain nicht auflöst (remote, kein
  Wildcard-DNS). Der Server sagt über ``X-Nest-Frame-Base``, welche der beiden
  gerade bedient wird; alle relativen URLs der Seite werden damit präfixiert,
  damit sie im Pfad-Modus wieder beim Plugin landen.
* ``scrolling="no"`` am iframe: gescrollt wird innerhalb der Seite.
* Ohne ``allow-same-origin`` ist der Frame eine opake Origin -- er kann seine
  eigene Origin nicht per fetch lesen (kein CORS). Aktualisiert wird per Reload.
"""
from __future__ import annotations

import html
import json
import os
import threading
from datetime import timedelta
from typing import Any
from urllib.parse import quote, urlencode

from ebay_tracker.models import SOURCE_LABELS, format_money
from ebay_tracker.sources.fetch import STATS

from . import __version__

from .engine import Engine
from .store import Match, now, parse_iso
from .widgets import ago, source_checks, until

NEST_ORIGIN = os.environ.get("NEST_DASHBOARD_ORIGIN", "https://nest.mini.shibusa.moe")

# Der Pfad, unter dem Nest diese Oberfläche im Pfad-Modus ausliefert
# (/plugins/frame/<label>/…). Der Proxy setzt X-Nest-Frame-Base nur dort, wo
# ein Frame wirklich unter Nests Host liegt; auf der Subdomain bleibt der
# Header weg und die Seite bedient sich selbst. Ohne Header ist der Basis-
# pfad leer -- alles bleibt bei den absoluten Pfaden der Subdomain-Form.
FRAME_BASE_HEADER = "X-Nest-Frame-Base"


def frame_base(headers: Any) -> str:
    """Der Basispfad der Oberfläche: "" auf der Subdomain, sonst der Mount
    unter Nests Host -- `/arthur-tracker/`, mit führendem und schließendem
    Slash (Nest sendet den Mount, nicht den gemeinsamen /plugins/frame/
    Präfix; die relativen URLs brauchen nur den Teil, der zwischen Host und
    eigenem Pfad liegt). Fremde oder falsch geformte Werte werden verworfen:
    der Header kommt von Nest, nicht vom Browser -- und Nest streicht einen
    vom Browser mitgeschickten gleichnamigen Header vorher, also ist das
    hier die dritte Sperre, weil sie nichts kostet."""
    raw = ""
    try:
        raw = headers.get(FRAME_BASE_HEADER, "") or ""
    except Exception:
        return ""
    if not raw.startswith("/") or not raw.endswith("/"):
        return ""
    label = raw[1:-1]
    if not label or "/" in label:
        return ""
    return raw


def esc(value: Any) -> str:
    return html.escape("" if value is None else str(value), quote=True)


# ---------------------------------------------------------------- Design
# Nests Tokens (src/index.css, DESIGN.md §2): Teal-Akzent, ruhige Flächen,
# weiche Schatten. Dunkel ist Standard, Nest schickt das Theme per postMessage.
CSS = """
:root{
  --bg:hsl(222 24% 9%);--surface:hsl(222 20% 12%);--elev:hsl(222 18% 15%);--sunk:hsl(222 22% 10%);
  --t1:hsl(210 22% 96%);--t2:hsl(214 14% 72%);--t3:hsl(214 12% 52%);
  --b1:hsl(222 16% 17%);--b2:hsl(222 14% 22%);--b3:hsl(222 12% 32%);
  --acc:hsl(178 58% 46%);--acc-fg:hsl(222 28% 10%);
  --ok:hsl(152 52% 46%);--warn:hsl(32 84% 58%);--err:hsl(0 64% 66%);--amber:hsl(38 82% 56%);
  --violet:hsl(258 58% 70%);--mute:hsl(214 12% 52%);
  --ebay:hsl(199 76% 60%);--kleinanzeigen:hsl(152 52% 50%);--vinted:hsl(178 58% 50%);--mydealz:hsl(22 88% 60%);
  --sh1:0 1px 0 hsl(0 0% 100% / .03) inset,0 1px 2px hsl(0 0% 0% / .35);
  --sh2:0 1px 0 hsl(0 0% 100% / .04) inset,0 10px 30px -10px hsl(0 0% 0% / .6);
  --r:18px;--r-s:12px;
  color-scheme:dark;
}
html.light{
  --bg:hsl(210 20% 97%);--surface:hsl(0 0% 100%);--elev:hsl(0 0% 100%);--sunk:hsl(210 20% 95%);
  --t1:hsl(215 28% 14%);--t2:hsl(215 14% 38%);--t3:hsl(215 12% 55%);
  --b1:hsl(214 20% 93%);--b2:hsl(214 18% 87%);--b3:hsl(214 16% 76%);
  --acc:hsl(178 62% 36%);--acc-fg:#fff;
  --ok:hsl(152 58% 34%);--warn:hsl(32 90% 42%);--err:hsl(0 66% 48%);--amber:hsl(38 88% 40%);
  --violet:hsl(258 62% 56%);--mute:hsl(215 12% 50%);
  --ebay:hsl(199 82% 42%);--kleinanzeigen:hsl(152 58% 36%);--vinted:hsl(178 62% 36%);--mydealz:hsl(22 88% 48%);
  --sh1:0 1px 2px rgb(16 24 40 / .04),0 1px 3px rgb(16 24 40 / .06);
  --sh2:0 4px 8px -2px rgb(16 24 40 / .06),0 12px 24px -6px rgb(16 24 40 / .12);
  color-scheme:light;
}
*{box-sizing:border-box}
html,body{margin:0;height:100%;background:transparent}
body{font:14px/1.5 ui-sans-serif,-apple-system,BlinkMacSystemFont,"SF Pro Text","Segoe UI",Inter,Roboto,sans-serif;
  color:var(--t1);-webkit-font-smoothing:antialiased;overflow:hidden;font-feature-settings:"tnum" 1,"cv11" 1}
a{color:inherit;text-decoration:none}
svg.i{width:16px;height:16px;flex:none;display:block}
.tint{background:color-mix(in srgb,var(--c) 14%,transparent);color:var(--c)}
.pill{display:inline-flex;align-items:center;gap:6px;height:24px;padding:0 10px;border-radius:999px;
  font-size:12px;font-weight:600;white-space:nowrap;line-height:1}
.pill svg.i{width:13px;height:13px}
.dot{width:8px;height:8px;border-radius:50%;background:var(--c,var(--mute));flex:none}
.pulse{position:relative}
.pulse::after{content:"";position:absolute;inset:-4px;border-radius:50%;background:var(--c);opacity:.35;
  animation:pulse 2s ease-out infinite}
@keyframes pulse{0%{transform:scale(.6);opacity:.5}100%{transform:scale(1.6);opacity:0}}
.s-ok{--c:var(--ok)}.s-warn{--c:var(--warn)}.s-error{--c:var(--err)}.s-loading{--c:var(--acc)}.s-paused{--c:var(--mute)}
.v-top{--c:var(--ok)}.v-gut{--c:var(--acc)}.v-fair{--c:var(--mute)}.v-teuer{--c:var(--warn)}
.v-verdaechtig{--c:var(--violet)}.v-ungeeignet{--c:var(--mute)}.v-neu{--c:var(--acc)}
.src-ebay{--c:var(--ebay)}.src-kleinanzeigen{--c:var(--kleinanzeigen)}.src-vinted{--c:var(--vinted)}.src-mydealz{--c:var(--mydealz)}
.chips{display:flex;gap:6px;overflow-x:auto;scrollbar-width:none;min-width:0}
.chips::-webkit-scrollbar{display:none}
.chip{flex:none;display:inline-flex;align-items:center;gap:7px;height:32px;padding:0 14px;border-radius:999px;
  background:var(--sunk);border:1px solid var(--b1);color:var(--t2);font-size:13px;font-weight:550;white-space:nowrap;
  transition:background .15s,color .15s,border-color .15s}
.chip:hover{color:var(--t1);border-color:var(--b2)}
.chip.on{background:var(--acc);border-color:var(--acc);color:var(--acc-fg)}
.chip.on .dot{background:currentColor}
.seg{display:inline-flex;padding:3px;border-radius:12px;background:var(--sunk);border:1px solid var(--b1);flex:none}
.seg a{height:28px;display:inline-flex;align-items:center;padding:0 12px;border-radius:9px;color:var(--t2);
  font-size:13px;font-weight:550;white-space:nowrap}
.seg a:hover{color:var(--t1)}
.seg a.on{background:var(--elev);color:var(--t1);box-shadow:var(--sh1)}
.btn{flex:none;display:inline-flex;align-items:center;gap:6px;height:32px;padding:0 14px;border-radius:999px;
  border:1px solid var(--b2);background:transparent;color:var(--t2);font:inherit;font-size:13px;font-weight:550;cursor:pointer;
  white-space:nowrap;transition:color .15s,border-color .15s}
.btn:hover{color:var(--t1);border-color:var(--b3)}
.btn svg.i{width:14px;height:14px}

/* ---------------- Karte ---------------- */
.card{position:relative;display:flex;flex-direction:column;min-width:0;background:var(--surface);border:1px solid var(--b1);
  border-radius:var(--r);overflow:hidden;cursor:pointer;box-shadow:var(--sh1);
  transition:transform .2s cubic-bezier(.2,.8,.2,1),box-shadow .2s,border-color .2s}
.card:hover{transform:translateY(-3px);box-shadow:var(--sh2);border-color:var(--b2)}
.card.deal{border-color:color-mix(in srgb,var(--c) 40%,var(--b1))}
.media{position:relative;aspect-ratio:4/3;background:var(--sunk);overflow:hidden;flex:none}
.media img{width:100%;height:100%;object-fit:cover;display:block;transition:transform .5s cubic-bezier(.2,.8,.2,1)}
.card:hover .media img{transform:scale(1.04)}
.media::after{content:"";position:absolute;inset:auto 0 0 0;height:45%;
  background:linear-gradient(to top,hsl(222 30% 6% / .55),transparent);pointer-events:none}
.ph{position:absolute;inset:0;display:grid;place-items:center;color:var(--c);
  background:radial-gradient(120% 90% at 30% 20%,color-mix(in srgb,var(--c) 18%,transparent),transparent 70%)}
.ph svg.i{width:34px;height:34px;opacity:.7}
.tags{position:absolute;left:10px;right:10px;top:10px;display:flex;justify-content:space-between;gap:6px;z-index:1}
.glass{display:inline-flex;align-items:center;gap:6px;height:24px;padding:0 9px;border-radius:999px;font-size:11.5px;
  font-weight:650;color:#fff;background:hsl(222 30% 8% / .55);backdrop-filter:blur(10px) saturate(1.4);
  -webkit-backdrop-filter:blur(10px) saturate(1.4);border:1px solid hsl(0 0% 100% / .08)}
.glass .dot{width:7px;height:7px}
.glass.new{background:var(--acc);color:var(--acc-fg);border-color:transparent}
.verdict{position:absolute;left:10px;bottom:10px;z-index:1;height:24px;padding:0 10px;border-radius:999px;
  display:inline-flex;align-items:center;font-size:11.5px;font-weight:700;color:#fff;
  background:color-mix(in srgb,var(--c) 88%,black)}
.info{padding:14px 16px 16px;display:flex;flex-direction:column;gap:4px;min-width:0;flex:1}
.priceline{display:flex;align-items:baseline;gap:8px;min-width:0}
.price{font-size:19px;font-weight:720;letter-spacing:-.02em;white-space:nowrap}
.was{font-size:12.5px;color:var(--t3);text-decoration:line-through;white-space:nowrap}
.drop{color:var(--ok);font-size:12px;font-weight:650;white-space:nowrap}
.specs{font-size:12.5px;font-weight:600;color:var(--t1);white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.ttl{font-size:13px;color:var(--t2);line-height:1.4;display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical;
  overflow:hidden;overflow-wrap:anywhere}
.foot{margin-top:auto;padding-top:8px;display:flex;align-items:center;gap:6px;color:var(--t3);font-size:12px;min-width:0}
.foot span{white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.foot svg.i{width:13px;height:13px}
.meter{margin-top:8px;display:flex;flex-direction:column;gap:5px}
.meter .bar{position:relative;height:6px;border-radius:99px;background:var(--sunk);overflow:hidden}
.meter .bar i{position:absolute;inset:0 auto 0 0;border-radius:99px;background:var(--c)}
.meter .bar b{position:absolute;top:-2px;bottom:-2px;width:2px;background:var(--t3);border-radius:2px}
.meter .lbl{display:flex;justify-content:space-between;font-size:11.5px;color:var(--t3)}
.meter .lbl strong{color:var(--c);font-weight:650}
.pc{list-style:none;margin:8px 0 0;padding:0;display:flex;flex-direction:column;gap:4px;font-size:12.5px}
.pc li{display:flex;gap:7px;align-items:flex-start;color:var(--t2)}
.pc li span{display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical;overflow:hidden}
.pc li svg.i{width:14px;height:14px;margin-top:2px}
.pc .p svg{color:var(--ok)}.pc .c svg{color:var(--warn)}

/* ---------------- Kachel ---------------- */
.tile{height:100%;display:flex;flex-direction:column;gap:12px;padding:16px 18px}
.tile .top{display:flex;align-items:center;gap:12px;min-width:0}
.tile .brand{display:flex;align-items:center;gap:10px;min-width:0;flex:none}
.logo{width:30px;height:30px;border-radius:9px;display:grid;place-items:center;flex:none;color:var(--acc-fg);
  background:linear-gradient(135deg,var(--acc),color-mix(in srgb,var(--acc) 55%,var(--violet)));
  box-shadow:0 4px 14px -4px color-mix(in srgb,var(--acc) 60%,transparent)}
.logo svg.i{width:17px;height:17px}
.tile .name{font-weight:680;font-size:15px;letter-spacing:-.01em}
.tile .when{display:flex;align-items:center;gap:7px;color:var(--t3);font-size:12.5px;white-space:nowrap}
.tile .chips{flex:1}
.strip{flex:1;min-height:0;display:flex;gap:12px;overflow-x:auto;overflow-y:hidden;scroll-snap-type:x proximity;
  scrollbar-width:none;margin:-4px -18px 0;padding:4px 18px 2px;scroll-padding-inline:18px}
.strip::-webkit-scrollbar{display:none}
.strip .card{width:212px;flex:none;scroll-snap-align:start}
.strip .media{aspect-ratio:auto;flex:1;min-height:56px}
.strip .info{flex:none;padding:10px 12px 12px;gap:2px}
.strip .price{font-size:16px}
.strip .ttl{-webkit-line-clamp:1;font-size:12px}
.strip .specs + .ttl{display:none}
.strip .foot{padding-top:4px}
.empty{flex:1;display:grid;place-items:center;text-align:center;color:var(--t3);padding:16px 24px;font-size:13px}
.empty .ic{width:44px;height:44px;border-radius:14px;display:grid;place-items:center;margin:0 auto 10px;
  background:var(--sunk);color:var(--t3)}
.empty .ic svg.i{width:22px;height:22px}
.empty b{display:block;color:var(--t1);font-size:14.5px;font-weight:650;margin-bottom:3px}
.empty code{font:12px ui-monospace,"SF Mono",Menlo,monospace;background:var(--sunk);padding:2px 6px;border-radius:6px}

/* ---------------- Vollbild-Seite ---------------- */
body.page{overflow:auto;background:var(--bg)}
body.page::before{content:"";position:fixed;inset:0 0 auto 0;height:420px;pointer-events:none;z-index:-1;
  background:radial-gradient(60% 100% at 15% 0%,color-mix(in srgb,var(--acc) 12%,transparent),transparent 70%),
             radial-gradient(40% 80% at 90% 0%,color-mix(in srgb,var(--violet) 8%,transparent),transparent 70%)}
.wrap{max-width:1320px;margin:0 auto;padding:44px 40px 80px;display:flex;flex-direction:column;gap:44px}
.hdr{display:flex;align-items:center;gap:20px;flex-wrap:wrap}
.hdr .logo{width:52px;height:52px;border-radius:16px}
.hdr .logo svg.i{width:28px;height:28px}
.hdr h1{margin:0;font-size:30px;font-weight:750;letter-spacing:-.025em;line-height:1.1}
.hdr .status{display:flex;align-items:center;gap:9px;color:var(--t2);font-size:13.5px;margin-top:5px}
.hdr .sp{flex:1}
.hdr .srcs{display:flex;gap:8px;flex-wrap:wrap}
.sec{display:flex;flex-direction:column;gap:18px}
.sech{display:flex;align-items:center;gap:14px;flex-wrap:wrap;min-width:0}
.sech h2{margin:0;font-size:19px;font-weight:700;letter-spacing:-.015em;display:flex;align-items:center;gap:10px}
.sech h2 svg.i{width:19px;height:19px;color:var(--acc)}
.sech .cnt{color:var(--t3);font-weight:550;font-size:15px}
.sech .sp{flex:1}
.kpis{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:18px}
.kpi{position:relative;background:var(--surface);border:1px solid var(--b1);border-radius:var(--r);padding:20px 22px;
  box-shadow:var(--sh1);overflow:hidden;min-width:0}
.kpi .l{display:flex;align-items:center;gap:8px;color:var(--t2);font-size:13px;font-weight:550}
.kpi .l .ic{width:28px;height:28px;border-radius:9px;display:grid;place-items:center}
.kpi .l .ic svg.i{width:15px;height:15px}
.kpi .v{font-size:34px;font-weight:750;letter-spacing:-.03em;margin-top:12px;line-height:1}
.kpi .v small{font-size:15px;font-weight:600;color:var(--t3);letter-spacing:0;margin-left:4px}
.kpi .h{color:var(--t3);font-size:12.5px;margin-top:6px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.bars{position:absolute;right:20px;bottom:20px;display:flex;align-items:flex-end;gap:4px;height:40px}
.bars i{width:7px;border-radius:3px;background:color-mix(in srgb,var(--acc) 35%,transparent);min-height:3px}
.bars i:last-child{background:var(--acc)}
.heroes{display:grid;grid-template-columns:repeat(auto-fill,minmax(520px,1fr));gap:20px}
.hero{display:grid;grid-template-columns:44% 1fr}
.hero .media{aspect-ratio:auto;height:auto;min-height:260px}
.hero .media img{position:absolute;inset:0}
.hero .info{padding:20px 22px}
.hero .price{font-size:26px}
.hero .ttl{-webkit-line-clamp:2}
.nodeal{display:flex;align-items:center;gap:18px;background:var(--surface);border:1px dashed var(--b2);
  border-radius:var(--r);padding:24px 26px;color:var(--t2)}
.nodeal .ic{width:46px;height:46px;border-radius:14px;display:grid;place-items:center;flex:none}
.nodeal .ic svg.i{width:22px;height:22px}
.nodeal b{display:block;color:var(--t1);font-size:15px;margin-bottom:2px}
.searches{display:grid;grid-template-columns:repeat(auto-fill,minmax(360px,1fr));gap:18px}
.srch{position:relative;display:flex;flex-direction:column;gap:12px;background:var(--surface);border:1px solid var(--b1);
  border-radius:var(--r);padding:20px 22px;box-shadow:var(--sh1);min-width:0;transition:border-color .15s,transform .2s}
a.srch:hover{border-color:var(--b2);transform:translateY(-2px)}
.srch.on{border-color:var(--acc)}
.srch .h{display:flex;align-items:center;gap:10px;min-width:0}
.srch .h .n{font-weight:650;font-size:15px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;flex:1}
.srch .row{display:flex;align-items:center;gap:6px;flex-wrap:wrap}
.srch .nums{display:flex;gap:22px;min-width:0}
.srch .nums div{display:flex;flex-direction:column}
.srch .nums b{white-space:nowrap;font-size:18px;font-weight:700;letter-spacing:-.02em}
.srch .nums span{font-size:12px;color:var(--t3)}
.srch code{font:11.5px/1.5 ui-monospace,"SF Mono",Menlo,monospace;color:var(--t3);background:var(--sunk);
  border-radius:9px;padding:8px 10px;overflow-wrap:anywhere;display:block}
.srch code span{display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical;overflow:hidden}
.srch .err{display:flex;gap:8px;align-items:flex-start;font-size:12.5px;color:var(--err);
  background:color-mix(in srgb,var(--err) 10%,transparent);border-radius:9px;padding:8px 10px;overflow-wrap:anywhere}
.srch .err.w{color:var(--warn);background:color-mix(in srgb,var(--warn) 10%,transparent)}
.srch .err svg.i{width:14px;height:14px;margin-top:2px}
.spark{margin-left:auto;align-self:center;width:34%;max-width:150px;height:34px;color:var(--acc);flex:none}
.toolbar{display:flex;align-items:center;gap:12px;flex-wrap:wrap}
.toolbar .chips{flex:1;min-width:200px}
.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(236px,1fr));gap:20px}
.more{display:flex;justify-content:center;margin-top:6px}
.more .btn{height:40px;padding:0 22px}
.duo{display:grid;grid-template-columns:repeat(auto-fit,minmax(420px,1fr));gap:20px}
.panel{background:var(--surface);border:1px solid var(--b1);border-radius:var(--r);padding:8px;box-shadow:var(--sh1)}
.li{display:flex;align-items:center;gap:14px;padding:14px 14px;border-radius:12px}
.li + .li{border-top:1px solid var(--b1);border-radius:0}
.li .ic{width:36px;height:36px;border-radius:11px;display:grid;place-items:center;flex:none;font-weight:750;font-size:14px}
.li .tx{flex:1;min-width:0}
.li .tx b{display:block;font-weight:620;font-size:14px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.li .tx span{display:block;color:var(--t3);font-size:12.5px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.stack{display:flex;height:6px;border-radius:99px;overflow:hidden;background:var(--sunk);margin-top:8px;gap:2px}
.stack i{display:block;height:100%}
.li .r{text-align:right;flex:none}
.li .r b{display:block;font-weight:650}
footer{color:var(--t3);font-size:12.5px;text-align:center}
@media (max-width:1100px){.kpis{grid-template-columns:repeat(2,minmax(0,1fr))}.heroes{grid-template-columns:1fr}}
@media (max-width:720px){.hero{grid-template-columns:1fr}.hero .media{aspect-ratio:16/10;min-height:0}}
@media (max-width:640px){.wrap{padding:28px 18px 60px;gap:34px}.heroes{grid-template-columns:1fr}
  .searches,.duo{grid-template-columns:1fr}
  .kpis{gap:12px}.kpi .v{font-size:28px}.bars{display:none}.hdr h1{font-size:25px}}
@media (prefers-reduced-motion:reduce){*{transition:none!important;animation:none!important}}
"""

_I = {
    "radar": '<path d="M19.07 4.93A10 10 0 0 0 6.99 3.34"/><path d="M4 6h.01"/><path d="M2.29 9.62A10 10 0 1 0 21.31 8.35"/>'
             '<path d="M16.24 7.76A6 6 0 1 0 8.23 16.67"/><path d="M12 18h.01"/><path d="M17.99 11.66A6 6 0 0 1 15.77 16.67"/>'
             '<circle cx="12" cy="12" r="2"/><path d="m13.41 10.59 5.66-5.66"/>',
    "zap": '<path d="M4 14a1 1 0 0 1-.78-1.63l9.9-10.2a.5.5 0 0 1 .86.46l-1.92 6.02A1 1 0 0 0 13 10h7a1 1 0 0 1 '
           '.78 1.63l-9.9 10.2a.5.5 0 0 1-.86-.46l1.92-6.02A1 1 0 0 0 11 14z"/>',
    "search": '<circle cx="11" cy="11" r="8"/><path d="m21 21-4.3-4.3"/>',
    "clock": '<circle cx="12" cy="12" r="10"/><polyline points="12 6 12 12 16 14"/>',
    "down": '<polyline points="22 17 13.5 8.5 8.5 13.5 2 7"/><polyline points="16 17 22 17 22 11"/>',
    "eye": '<path d="M2.06 12.35a1 1 0 0 1 0-.7 10.75 10.75 0 0 1 19.88 0 1 1 0 0 1 0 .7 10.75 10.75 0 0 1-19.88 0"/>'
           '<circle cx="12" cy="12" r="3"/>',
    "out": '<path d="M7 7h10v10"/><path d="M7 17 17 7"/>',
    "pin": '<path d="M20 10c0 6-8 12-8 12s-8-6-8-12a8 8 0 0 1 16 0Z"/><circle cx="12" cy="10" r="3"/>',
    "shield": '<path d="M20 13c0 5-3.5 7.5-7.66 8.95a1 1 0 0 1-.67-.01C7.5 20.5 4 18 4 13V6a1 1 0 0 1 1-1c2 0 4.5-1.2 '
              '6.24-2.72a1.17 1.17 0 0 1 1.52 0C14.51 3.81 17 5 19 5a1 1 0 0 1 1 1z"/>',
    "image": '<rect width="18" height="18" x="3" y="3" rx="2"/><circle cx="9" cy="9" r="2"/>'
             '<path d="m21 15-3.09-3.09a2 2 0 0 0-2.82 0L6 21"/>',
    "alert": '<path d="m21.73 18-8-14a2 2 0 0 0-3.48 0l-8 14A2 2 0 0 0 4 21h16a2 2 0 0 0 1.73-3"/><path d="M12 9v4"/>'
             '<path d="M12 17h.01"/>',
    "check": '<path d="M20 6 9 17l-5-5"/>',
    "x": '<path d="M18 6 6 18"/><path d="m6 6 12 12"/>',
    "game": '<line x1="6" x2="10" y1="12" y2="12"/><line x1="8" x2="8" y1="10" y2="14"/><line x1="15" x2="15.01" y1="13" '
            'y2="13"/><line x1="18" x2="18.01" y1="11" y2="11"/><rect width="20" height="12" x="2" y="6" rx="2"/>',
    "list": '<path d="M3 12h.01"/><path d="M3 18h.01"/><path d="M3 6h.01"/><path d="M8 12h13"/><path d="M8 18h13"/>'
            '<path d="M8 6h13"/>',
    "sparkles": '<path d="M9.94 14.06 8.5 20l-1.44-5.94L1 12.5l6.06-1.44L8.5 5l1.44 6.06L16 12.5z"/><path d="M20 3v4"/>'
                '<path d="M22 5h-4"/>',
    "truck": '<path d="M14 18V6a2 2 0 0 0-2-2H4a2 2 0 0 0-2 2v11a1 1 0 0 0 1 1h2"/><path d="M15 18H9"/>'
             '<path d="M19 18h2a1 1 0 0 0 1-1v-3.65a1 1 0 0 0-.22-.62l-3.48-4.35A1 1 0 0 0 17.52 8H14"/>'
             '<circle cx="17" cy="18" r="2"/><circle cx="7" cy="18" r="2"/>',
}


def icon(name: str) -> str:
    return ("<svg class=i viewBox='0 0 24 24' fill=none stroke=currentColor stroke-width=1.9 "
            f"stroke-linecap=round stroke-linejoin=round aria-hidden=true>{_I[name]}</svg>")


DEAL_VERDICTS = ("top", "gut", "verdaechtig")
PAGE_SIZE = 48
PAGE_MAX = 240
PAGE_SCAN = 400  # so viele Treffer werden fürs Filtern/Sortieren gelesen
FRESH = timedelta(hours=24)
SOURCE_SHORT = {"ebay": "eB", "kleinanzeigen": "KA", "vinted": "Vi", "mydealz": "md"}

JS = """
(function(){
  var NEST = %(nest)s, BASE = %(base)s, root = document.documentElement;
  var embedded = window.parent !== window;
  // Absolute Adresse dieser Oberfläche: Subdomain-Form ohne Basispfad, im
  // Pfad-Modus Nests Origin + Basispfad. Alle relativen Pfade laufen darüber.
  var self = location.protocol + '//' + location.host + BASE;
  function theme(t){ if(t === 'light'){root.classList.add('light')} else if(t === 'dark'){root.classList.remove('light')} }
  if(!embedded && window.matchMedia && matchMedia('(prefers-color-scheme: light)').matches){theme('light')}
  var qs = new URLSearchParams(location.search); if(qs.get('theme')){theme(qs.get('theme'))}
  window.addEventListener('message', function(e){
    // Doppelt prüfen wie Nest selbst: wirklich der Einbetter UND die richtige
    // Absender-Origin. Eine sandboxed Frame-Origin ist opak -- der Browser
    // meldet "null" statt der URL-Origin (nachgemessen, Playwright 2026-09-25).
    // Nest akzeptiert deshalb genau: source === dieser Frame, und origin
    // NEST (Subdomain-Form) oder "null" (Pfad-Form, opak). Alles andere ist
    // nicht unser Einbetter.
    if(e.source !== window.parent) return;
    if(e.origin !== NEST && e.origin !== 'null') return;
    var d = e.data || {};
    if(d.type === 'nest:init' || d.type === 'nest:theme'){ theme(d.theme); try{sessionStorage.theme=d.theme}catch(_){} }
  });
  if(embedded){
    // Outbound ebenfalls '*': eine opake Origin bekommt postMessage mit
    // konkretem Ziel nie zugestellt (nachgemessen). Sicher, weil der Frame
    // sandboxed ist und Nest jede Nachricht an source und origin prüft.
    window.parent.postMessage({type:'nest:ready'}, '*');
  }
  function open(path){
    // Ohne Basis ist der Pfad absolut auf der Subdomain (/?layout=page,
    // /go/…). Mit Basis (Pfad-Modus) landet jeder Pfad unter dem Mount:
    // führenden Slash ab, dann ans Basis-Ende (BASE endet mit /).
    var url = BASE ? self + path.replace(/^[/]/, '') : self + path;
    if(embedded){ window.parent.postMessage({type:'nest:open', url:url}, '*'); }
    else { window.open(url, '_blank', 'noopener'); }
  }
  document.addEventListener('click', function(e){
    var el = e.target.closest('[data-open]');
    if(!el) return;
    e.preventDefault(); open(el.getAttribute('data-open'));
  });
  // Nachladen statt fetch: eine opake Origin kann sich selbst nicht lesen.
  var idle = Date.now();
  ['pointermove','wheel','keydown','touchstart'].forEach(function(n){addEventListener(n,function(){idle=Date.now()},{passive:true})});
  setInterval(function(){ if(Date.now()-idle > 20000 && !document.hidden){ location.reload() } }, %(reload)d);
})();
"""


def _page(body: str, *, page: bool = False, reload_ms: int = 90_000, base: str = "") -> str:
    script = JS % {"nest": json.dumps(NEST_ORIGIN), "base": json.dumps(base), "reload": reload_ms}
    return (
        "<!doctype html><html lang=de><head><meta charset=utf-8>"
        "<meta name=viewport content='width=device-width,initial-scale=1'>"
        f"<title>Tracker</title><style>{CSS}</style></head>"
        f"<body class='{'page' if page else 'tilebody'}'>{body}<script>{script}</script></body></html>"
    )


# Der Basispfad, den die Seitenfunktionen gerade verwenden. Wird pro Anfrage
# von widget()/page() gesetzt (thread-lokal, weil ThreadingHTTPServer jede
# Anfrage in ihrem Thread bedient) und von den relativen URL-Helfern gelesen.
_base = threading.local()


def _b() -> str:
    """Der Basispfad der aktuellen Anfrage: "" auf der Subdomain, sonst der Mount."""
    return getattr(_base, "path", "") or ""


def _qs(**params: Any) -> str:
    """Eine relative URL auf dieser Oberfläche (mit Basis im Pfad-Modus, ohne doppelten Slash)."""
    clean = {k: v for k, v in params.items() if v not in (None, "")}
    if clean:
        return _b() + "?" + urlencode(clean) if _b() else "/?" + urlencode(clean)
    return _b() if _b() else "/"


# ---------------------------------------------------------------- Bausteine
def _verdict(m: Match) -> str | None:
    return (m.listing.assessment or {}).get("verdict")


def is_deal(m: Match) -> bool:
    """Ein Deal: laut Bewertung gut/top/verdächtig -- ohne Bewertung jeder gemeldete Treffer."""
    v = _verdict(m)
    return v in DEAL_VERDICTS if v else not m.silent


def _is_new(m: Match) -> bool:
    return bool(not m.silent and m.first_seen and (now() - m.first_seen) < FRESH)


def _money(v: float | None) -> str:
    return format_money(v) if v is not None else "Preis?"


def _src(source: str, *, glass: bool = True) -> str:
    label = SOURCE_LABELS.get(source, source)
    cls = "glass" if glass else "pill tint"
    return f"<span class='{cls} src-{esc(source)}'><span class=dot></span>{esc(label)}</span>"


def _meter(a: dict, price: float | None) -> str:
    fair = a.get("fair_price")
    if not fair or not price:
        return ""
    ratio = price / fair
    width = max(4.0, min(ratio / 1.5, 1.0) * 100)
    marker = 100 / 1.5
    pct = round(abs(1 - ratio) * 100)
    word = f"{pct} % unter Wert" if ratio < 1 else (f"{pct} % über Wert" if pct else "genau Marktwert")
    cls = "v-top" if ratio <= .85 else "v-gut" if ratio < 1 else "v-teuer"
    return (f"<div class='meter {cls}'><div class=bar><i style='width:{width:.0f}%'></i>"
            f"<b style='left:{marker:.1f}%'></b></div><div class=lbl><strong>{esc(word)}</strong>"
            f"<span>Wert ~{esc(format_money(fair))}</span></div></div>")


def _card(m: Match, *, detail: bool = False, hero: bool = False) -> str:
    l = m.listing
    a = l.assessment or {}
    v = a.get("verdict")
    deal = is_deal(m) and v in DEAL_VERDICTS
    key = quote(m.key, safe="")
    img = (f"<img src='{_b()}img/{key}' alt='' loading={'eager' if hero else 'lazy'} decoding=async>"
           if l.image_url else f"<div class='ph src-{esc(l.source)}'>{icon('image')}</div>")
    new = f"<span class='glass new'>Neu</span>" if _is_new(m) else ""
    verdict = f"<span class='verdict v-{esc(v)}'>{esc(a.get('label') or v)}</span>" if v else ""
    drop = ""
    if l.price_before and l.price and l.price_before > l.price:
        drop = (f"<span class=was>{esc(format_money(l.price_before))}</span>"
                f"<span class=drop>−{round((1 - l.price / l.price_before) * 100)} %</span>")
    specs = f"<div class=specs>{esc(a.get('summary'))}</div>" if a.get("summary") else ""
    where = l.location or (l.shipping_note if l.source == "mydealz" else None)
    foot_bits = [x for x in (where, ago(m.first_seen)) if x]
    foot = (f"<div class=foot>{icon('pin') if l.location else icon('clock')}<span>{esc(' · '.join(foot_bits))}</span></div>"
            if foot_bits else "")
    extra = ""
    if detail or hero:
        extra = _meter(a, l.price)
        pros = a.get("pros", [])[1:4] if not hero else a.get("pros", [])[:2]
        cons = a.get("cons", [])[:2]
        items = [f"<li class=p>{icon('check')}<span>{esc(x)}</span></li>" for x in (pros if hero else pros[:2])]
        items += [f"<li class=c>{icon('x')}<span>{esc(x)}</span></li>" for x in cons]
        if items and (hero or detail):
            extra += f"<ul class=pc>{''.join(items)}</ul>"
    cls = "card" + (" deal v-" + v if deal else "") + (" hero" if hero else "")
    return (
        f"<a class='{cls}' href='{esc(l.url)}' data-open='{_b()}go/{key}' title='{esc(l.title)}'>"
        f"<div class=media>{img}<div class=tags>{_src(l.source)}{new}</div>{verdict}</div>"
        f"<div class=info><div class=priceline><span class=price>{esc(_money(l.price))}</span>{drop}</div>"
        f"{specs}<div class=ttl>{esc(l.title)}</div>{extra}{foot}</div></a>"
    )


def _search_state(engine: Engine, name: str) -> str:
    """Zustand für den Punkt: ok / warn / error / loading / paused."""
    search = next((s for s in engine.searches if s.name == name), None)
    if search is not None and not search.enabled:
        return "paused"
    row = engine.store.all_search_states().get(name)
    if row is None or row["last_run"] is None:
        return "loading"
    if row["last_error"]:
        return "error"
    return "warn" if row["source_errors"] else "ok"


def _status_line(engine: Engine) -> tuple[str, str]:
    """(Zustandsklasse, Text) für den Kopf."""
    last = engine.store.get_meta("last_run") or {}
    if engine.running:
        return "loading", "Prüft gerade …"
    bits = []
    if engine.next_run_at:
        bits.append(f"Nächste Prüfung {until(engine.next_run_at)}")
    if last:
        bits.append(f"zuletzt {ago(parse_iso(last.get('finished')))}")
    states = [_search_state(engine, s.name) for s in engine.active_searches]
    state = "error" if "error" in states else "warn" if "warn" in states else "ok"
    return state, " · ".join(bits) or "Wartet auf den ersten Lauf"


def _sparkline(points: list[float]) -> str:
    if len(points) < 2:
        return ""
    if len(points) > 8:  # Läufe schwanken stark -- gleitender Mittelwert statt Zacken
        k = 4
        points = [sum(points[max(0, i - k):i + 1]) / len(points[max(0, i - k):i + 1]) for i in range(len(points))]
        step = max(1, len(points) // 30)
        points = points[::step] + ([points[-1]] if (len(points) - 1) % step else [])
    lo, hi = min(points), max(points)
    span = (hi - lo) or 1
    n = len(points) - 1
    xy = [(i * 120 / n, 36 - (p - lo) / span * 32) for i, p in enumerate(points)]
    line = " ".join(f"{x:.1f},{y:.1f}" for x, y in xy)
    area = f"0,40 {line} 120,40"
    return ("<svg class='spark' viewBox='0 0 120 40' preserveAspectRatio='none' aria-hidden='true'>"
            "<defs><linearGradient id='g' x1='0' x2='0' y1='0' y2='1'>"
            "<stop offset='0' stop-color='currentColor' stop-opacity='.22'></stop>"
            "<stop offset='1' stop-color='currentColor' stop-opacity='0'></stop></linearGradient></defs>"
            f"<polygon points='{area}' fill='url(#g)'></polygon><polyline points='{line}' fill='none' "
            "stroke='currentColor' stroke-width='1.8' stroke-linejoin='round' stroke-linecap='round' "
            "vector-effect='non-scaling-stroke'></polyline></svg>")


# ---------------------------------------------------------------- Kachel
def widget(engine: Engine, params: dict[str, str], base: str = "") -> str:
    """Die Kachel: Kopf mit Status und Filtern, darunter eine Kartenleiste -- Deals zuerst."""
    _base.path = base
    sel = params.get("s") or ""
    names = [s.name for s in engine.searches]
    if sel not in names:
        sel = ""
    chips = [f"<a class='chip {'on' if not sel else ''}' href='{_qs()}'>Alle</a>"]
    for name in names:
        chips.append(f"<a class='chip {'on' if name == sel else ''}' href='{esc(_qs(s=name))}'>"
                     f"<span class='dot s-{_search_state(engine, name)}'></span>{esc(name)}</a>")
    state, when = _status_line(engine)
    matches = engine.recent_matches(limit=60, search=sel or None, include_silent=True)
    matches.sort(key=lambda m: (not is_deal(m), not _is_new(m)))  # stabil: innerhalb neueste zuerst
    matches = matches[:24]
    if not engine.searches:
        content = (f"<div class=empty><div><div class=ic>{icon('search')}</div><b>Noch keine Suche</b>"
                   "In den Tracker-Einstellungen eine Zeile wie <code>thinkpad t14 max:600 -defekt</code> anlegen.</div></div>")
    elif not matches:
        content = (f"<div class=empty><div><div class=ic>{icon('radar')}</div><b>Noch nichts Passendes</b>"
                   f"{'Die erste Suche läuft gerade.' if engine.running else 'Sobald etwas auftaucht, steht es hier.'}</div></div>")
    else:
        content = "<div class=strip>" + "".join(_card(m) for m in matches) + "</div>"
    body = (
        "<div class=tile><div class=top>"
        f"<div class=brand><div class=logo>{icon('radar')}</div><div class=name>Tracker</div></div>"
        f"<div class=when><span class='dot s-{state}{' pulse' if state == 'loading' else ''}'></span>{esc(when)}</div>"
        f"<div class=chips>{''.join(chips)}</div>"
        f"<button class=btn data-open='{_b()}?layout=page'>Alles {icon('out')}</button>"
        f"</div>{content}</div>"
    )
    return _page(body, base=base)


# ---------------------------------------------------------------- Vollbild
def _per_day(engine: Engine, days: int = 7) -> list[int]:
    cum = [engine.count_new_since(now() - timedelta(days=d)) for d in range(days, -1, -1)]  # d=7 … 0
    # cum[i] = seit (days-i) Tagen; Tag i = cum[i] - cum[i+1]
    return [max(0, cum[i] - cum[i + 1]) for i in range(days)]


def _kpis(engine: Engine, all_matches: list[Match]) -> str:
    daily = _per_day(engine)
    peak = max(daily) or 1
    bars = "".join(f"<i style='height:{max(8, round(n / peak * 100))}%'></i>" for n in daily)
    deals = [m for m in all_matches if is_deal(m) and _verdict(m) in DEAL_VERDICTS]
    # "Bester Deal" nur aus top/gut: ein Verdachtsfall (evtl. Betrug) ist kein Rekord.
    ratios = [m.listing.price / m.listing.assessment["fair_price"] for m in deals
              if _verdict(m) in ("top", "gut") and m.listing.price and (m.listing.assessment or {}).get("fair_price")]
    best = f"{round((1 - min(ratios)) * 100)}<small>%</small>" if ratios and min(ratios) < 1 else "–"
    tracked = engine.store.count_tracked()
    active = len(engine.active_searches)
    paused = len(engine.searches) - active

    def kpi(ic: str, cls: str, label: str, value: str, hint: str, extra: str = "") -> str:
        return (f"<div class=kpi><div class=l><span class='ic tint {cls}'>{icon(ic)}</span>{esc(label)}</div>"
                f"<div class=v>{value}</div><div class=h>{esc(hint)}</div>{extra}</div>")

    return "<div class=kpis>" + "".join((
        kpi("zap", "v-gut", "Neu in 24 h", str(daily[-1]),
            f"{sum(daily)} in 7 Tagen", f"<div class=bars title='Neue Treffer je Tag'>{bars}</div>"),
        kpi("sparkles", "v-top", "Deals gerade", str(len(deals)), "gut, top oder verdächtig"),
        kpi("down", "v-verdaechtig", "Bester Deal", best, "unter Wert (ohne Verdachtsfälle)"),
        kpi("eye", "src-ebay", "Beobachtet", f"{tracked}", f"{active} Suchen aktiv" + (f" · {paused} pausiert" if paused else "")),
    )) + "</div>"


def _heroes(all_matches: list[Match]) -> str:
    deals = [m for m in all_matches if _verdict(m) in DEAL_VERDICTS]

    def score(m: Match) -> float:
        a = m.listing.assessment or {}
        fair, p = a.get("fair_price"), m.listing.price
        ratio = p / fair if fair and p else 1.0
        return ratio + (0.5 if a.get("verdict") == "verdaechtig" else 0)

    deals.sort(key=score)
    head = (f"<div class=sech><h2>{icon('sparkles')}Beste Deals</h2>"
            f"<span class=cnt>{len(deals) or ''}</span></div>")
    if not deals:
        return (f"<section class=sec>{head}<div class=nodeal><div class='ic tint v-gut'>{icon('radar')}</div><div>"
                "<b>Gerade kein Deal unter Wert</b>Der Tracker schaut weiter alle paar Minuten. Taucht etwas "
                "Gutes auf, steht es hier – und kommt direkt aufs Handy.</div></div></section>")
    cards = "".join(_card(m, hero=True) for m in deals[:4])
    return f"<section class=sec>{head}<div class=heroes>{cards}</div></section>"


def _search_cards(engine: Engine, sel: str) -> str:
    states = engine.store.all_search_states()
    week = now() - timedelta(days=7)
    cards = []
    for p in engine.parsed:
        if p.search is None:
            cards.append(f"<div class=srch><div class=h><span class='dot s-error'></span><span class=n>Nicht verstanden"
                         f"</span></div><code>{esc(p.line)}</code><div class=err>{icon('alert')}<span>{esc(p.error)}"
                         "</span></div></div>")
            continue
        s = p.search
        row = states.get(s.name)
        st = _search_state(engine, s.name)
        label = {"ok": "Läuft", "warn": "Teilweise", "error": "Fehler", "loading": "Startet", "paused": "Pausiert"}[st]
        price = ""
        if s.min_price is not None or s.max_price is not None:
            if s.min_price is None:
                price = f"bis {format_money(s.max_price)}"
            elif s.max_price is None:
                price = f"ab {format_money(s.min_price)}"
            else:
                price = f"{format_money(s.min_price)} – {format_money(s.max_price)}"
        srcs = "".join(_src(x, glass=False) for x in s.sources)
        price_pill = f"<span class='pill tint s-paused'>{esc(price)}</span>" if price else ""
        tracked = engine.store.count_tracked(s.name)
        nums = (f"<div class=nums><div><b>{row['last_matched'] if row else 0}</b><span>passend</span></div>"
                f"<div><b>{tracked}</b><span>gemerkt</span></div>"
                f"<div><b>{esc(ago(parse_iso(row['last_run'])) if row and row['last_run'] else '–')}</b>"
                "<span>geprüft</span></div>")
        err = ""
        if row and row["last_error"]:
            err = f"<div class=err>{icon('alert')}<span>{esc(row['last_error'])}</span></div>"
        elif row and row["source_errors"]:
            txt = "; ".join(f"{SOURCE_LABELS.get(k, k)}: {v}" for k, v in json.loads(row["source_errors"]).items())
            err = f"<div class='err w'>{icon('alert')}<span>{esc(txt)}</span></div>"
        warn = "".join(f"<div class='err w'>{icon('alert')}<span>{esc(w)}</span></div>" for w in p.warnings)
        spark = _sparkline([v for _, v in engine.store.price_series(s.name, week)])
        cards.append(
            f"<a class='srch{' on' if s.name == sel else ''}' href='{esc(_qs(layout='page', s=s.name))}#angebote'>"
            f"<div class=h><span class='pill tint s-{st}'><span class='dot{' pulse' if st == 'loading' else ''}'>"
            f"</span>{label}</span><span class=n>{esc(s.name)}</span></div>"
            f"{nums}{spark}</div><div class=row>{srcs}{price_pill}</div>"
            f"<code><span>{esc(p.line)}</span></code>{err}{warn}</a>"
        )
    if not cards:
        cards.append(f"<div class=nodeal><div class='ic tint v-gut'>{icon('search')}</div><div><b>Noch keine Suche</b>"
                     "In Nest → Plugins → Tracker → Einstellungen eine Zeile pro Suche anlegen.</div></div>")
    return (f"<section class=sec><div class=sech><h2>{icon('search')}Suchen</h2><span class=cnt>{len(engine.parsed)}"
            f"</span></div><div class=searches>{''.join(cards)}</div></section>")


FILTERS = {"alle": "Alle", "deals": "Deals", "neu": "Heute neu"}
ORDERS = {"neu": "Neueste", "preis": "Preis", "wert": "Bester Wert"}


def _offers(engine: Engine, matches: list[Match], sel: str, flt: str, order: str, limit: int) -> str:
    if flt == "deals":
        matches = [m for m in matches if is_deal(m)]
    elif flt == "neu":
        matches = [m for m in matches if _is_new(m)]
    if order == "preis":
        matches = sorted(matches, key=lambda m: (m.listing.price is None, m.listing.price or 0))
    elif order == "wert":
        def ratio(m: Match) -> float:
            a = m.listing.assessment or {}
            return m.listing.price / a["fair_price"] if a.get("fair_price") and m.listing.price else 9.0
        matches = sorted(matches, key=ratio)
    total = len(matches)

    def link(**kw: str) -> str:
        q = {"layout": "page", "s": sel, "f": flt if flt != "alle" else "", "o": order if order != "neu" else ""}
        q.update(kw)
        return esc(_qs(**q)) + "#angebote"

    seg_f = "".join(f"<a class='{'on' if k == flt else ''}' href='{link(f=k if k != 'alle' else '')}'>{v}</a>"
                    for k, v in FILTERS.items())
    seg_o = "".join(f"<a class='{'on' if k == order else ''}' href='{link(o=k if k != 'neu' else '')}'>{v}</a>"
                    for k, v in ORDERS.items())
    names = [s.name for s in engine.searches]
    chips = [f"<a class='chip {'on' if not sel else ''}' href='{link(s='')}'>Alle Suchen</a>"]
    chips += [f"<a class='chip {'on' if n == sel else ''}' href='{link(s=n)}'>{esc(n)}</a>" for n in names]
    if matches:
        grid = "<div class=grid>" + "".join(_card(m, detail=True) for m in matches[:limit]) + "</div>"
    else:
        grid = (f"<div class=nodeal><div class='ic tint s-paused'>{icon('list')}</div><div><b>Nichts in dieser Ansicht</b>"
                "Anderen Filter wählen – oder abwarten, der Tracker sucht weiter.</div></div>")
    more = ""
    if total > limit:
        more = (f"<div class=more><a class=btn href='{link(n=str(min(limit + PAGE_SIZE, PAGE_MAX)))}'>"
                f"Mehr anzeigen · noch {total - limit}</a></div>")
    return (f"<section class=sec id=angebote><div class=sech><h2>{icon('list')}Angebote</h2>"
            f"<span class=cnt>{total}</span><span class=sp></span><div class=seg>{seg_f}</div>"
            f"<div class=seg>{seg_o}</div></div><div class=toolbar><div class=chips>{''.join(chips)}</div></div>"
            f"{grid}{more}</section>")


def _source_rows() -> list[dict[str, Any]]:
    rows = source_checks()
    snap = STATS.snapshot()
    by_label = {SOURCE_LABELS.get(k, k): (k, v) for k, v in snap.items()}
    for r in rows:
        name, s = by_label.get(r["label"], (r["label"], None))
        r["name"] = name
        r["parts"] = ([(s.http_ok + s.browser_ok, "var(--ok)"), (s.rescued, "var(--acc)"), (s.blocked, "var(--err)")]
                      if s else [])
    return rows


def _sources_section() -> str:
    """Abrufstatistik je Quelle seit dem Start (Stufe 1 / Browser-Rettung / Sperre)."""
    rows = _source_rows()
    if not rows:
        return ""
    items = []
    for r in rows:
        total = sum(n for n, _ in r["parts"]) or 1
        stack = "".join(f"<i style='width:{n / total * 100:.1f}%;background:{c}'></i>" for n, c in r["parts"] if n)
        items.append(
            f"<div class=li><div class='ic tint src-{esc(r['name'])}'>{esc(SOURCE_SHORT.get(r['name'], r['label'][:2]))}</div>"
            f"<div class=tx><b>{esc(r['label'])}</b><span>{esc(r['detail'])}</span><div class=stack>{stack}</div></div>"
            f"<span class='pill tint s-{esc(r['state'])}'><span class=dot></span>"
            f"{ {'ok': 'Stabil', 'warn': 'Wackelt', 'error': 'Gesperrt'}.get(r['state'], r['state']) }</span></div>")
    return (f"<section class=sec><div class=sech><h2>{icon('shield')}Quellen</h2><span class=cnt>seit dem Start</span>"
            f"</div><div class=panel>{''.join(items)}</div></section>")


def _restock_section(engine: Engine) -> str:
    rows = engine.store.restock_rows()
    if not rows:
        return ""
    items = []
    for r in rows:
        ok = bool(r["available"])
        price = format_money(r["price"]) if r["price"] else "–"
        items.append(
            f"<a class=li href='{esc(r['url'])}' target=_blank rel=noopener><div class='ic tint {'s-ok' if ok else 's-paused'}'>"
            f"{icon('game')}</div><div class=tx><b>{esc(r['shop'])}</b><span>{esc(r['note'] or r['name'])}</span></div>"
            f"<div class=r><b>{esc(price)}</b></div><span class='pill tint {'s-ok' if ok else 's-paused'}'>"
            f"<span class='dot{' pulse' if ok else ''}'></span>{'Verfügbar' if ok else 'Ausverkauft'}</span></a>")
    avail = sum(1 for r in rows if r["available"])
    return (f"<section class=sec><div class=sech><h2>{icon('game')}Zelda-Restock</h2><span class=cnt>"
            f"{avail} von {len(rows)} Shops</span></div><div class=panel>{''.join(items)}</div></section>")


def page(engine: Engine, params: dict[str, str], base: str = "") -> str:
    """Die Vollbild-Seite: Status, Kennzahlen, beste Deals, Suchen, Angebote, Quellen."""
    _base.path = base
    names = [s.name for s in engine.searches]
    sel = params.get("s") or ""
    if sel not in names:
        sel = ""
    flt = params.get("f") if params.get("f") in FILTERS else "alle"
    order = params.get("o") if params.get("o") in ORDERS else "neu"
    try:
        limit = max(PAGE_SIZE, min(int(params.get("n") or PAGE_SIZE), PAGE_MAX))
    except ValueError:
        limit = PAGE_SIZE

    everything = engine.recent_matches(limit=PAGE_SCAN, include_silent=True)
    scoped = [m for m in everything if m.search == sel] if sel else everything
    # Dieselbe Anzeige kann in mehreren Suchen stecken: im Raster nur einmal.
    seen: set[str] = set()
    unique = [m for m in scoped if not (m.key in seen or seen.add(m.key))]
    seen = set()
    unique_all = [m for m in everything if not (m.key in seen or seen.add(m.key))]

    state, status = _status_line(engine)
    srcs = "".join(
        f"<span class='pill tint s-{esc(r['state'])}'><span class=dot></span>{esc(r['label'])}</span>" for r in _source_rows())
    duo = _sources_section() + _restock_section(engine)
    body = (
        "<div class=wrap>"
        f"<header class=hdr><div class=logo>{icon('radar')}</div><div><h1>Tracker</h1>"
        f"<div class=status><span class='dot s-{state} pulse'></span>{esc(status)}</div></div>"
        f"<span class=sp></span><div class=srcs>{srcs}</div></header>"
        f"{_kpis(engine, unique_all)}"
        f"{_heroes(unique_all)}"
        f"{_search_cards(engine, sel)}"
        f"{_offers(engine, unique, sel, flt, order, limit)}"
        f"{f'<div class=duo>{duo}</div>' if duo else ''}"
        f"<footer>Tracker {esc(__version__)} · aktualisiert sich von selbst</footer>"
        "</div>"
    )
    return _page(body, page=True, reload_ms=120_000, base=base)


GO_PREFIXES = ("https://www.ebay.", "https://www.kleinanzeigen.de/", "https://www.vinted.", "https://www.mydealz.de/")


def go(target_url: str | None) -> str:
    """Weiterleitung in neuem Tab: Nest verwirft 3xx auf Plugin-Hosts, also per Meta/JS."""
    if not target_url or not target_url.startswith(GO_PREFIXES):
        return _page(f"<div class=tile><div class=empty><div><div class=ic>{icon('alert')}</div>"
                     "<b>Angebot nicht gefunden</b>Vielleicht ist es schon weg.</div></div></div>")
    u = esc(target_url)
    return (
        "<!doctype html><html lang=de><head><meta charset=utf-8>"
        f"<meta http-equiv=refresh content='0;url={u}'><title>Weiter …</title></head>"
        f"<body><a href='{u}'>Weiter zum Angebot</a>"
        f"<script>location.replace({json.dumps(target_url)})</script></body></html>"
    )
