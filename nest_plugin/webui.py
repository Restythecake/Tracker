"""Die Web-Oberfläche des Trackers: serverseitig gerendertes HTML, kein Build-Schritt.

Eine normale, eigenständige Website an der Wurzel des Plugin-Ports (hinter Caddy
und Authelia unter eigener Domain). Ansicht und Filter sind Query-Parameter;
geschrieben wird per ``POST /api/edit`` an den eigenen Server (``manage.handle``).

* Bilder von eBay/Kleinanzeigen laufen über ``/img/<key>`` (Cache, kein Hotlinking).
* Ein Klick auf ein Angebot öffnet ``/go/<key>`` im neuen Tab; diese Seite leitet
  zum Marktplatz weiter, ohne ihm die Tracker-Adresse als Referrer zu zeigen.
"""
from __future__ import annotations

import html
import json
from datetime import timedelta
from typing import Any
from urllib.parse import quote, urlencode

from ebay_tracker.models import SOURCE_LABELS, format_money
from ebay_tracker.sources.fetch import STATS

from . import __version__

from .engine import Engine
from . import manage, manual
from .manage import TIER_HINT, TIER_LABEL, TIERS
from .store import Match, now, parse_iso
from .widgets import DEAL_VERDICTS, ago, is_deal, is_new as _is_new, source_checks, until

def esc(value: Any) -> str:
    return html.escape("" if value is None else str(value), quote=True)


# ---------------------------------------------------------------- Design
# Nests Tokens (src/index.css, DESIGN.md §2): Teal-Akzent, ruhige Flächen,
# weiche Schatten. Dunkel ist Standard, hell folgt dem System (oder ?theme=light).
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
.heroes{display:grid;grid-template-columns:repeat(auto-fill,minmax(min(520px,100%),1fr));gap:20px}
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
.searches{display:grid;grid-template-columns:repeat(auto-fill,minmax(min(360px,100%),1fr));gap:18px}
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
.spark{margin-left:auto;align-self:center;flex:0 1 150px;min-width:48px;height:34px;color:var(--acc)}
@media (max-width:480px){.srch .nums{gap:14px}.srch .nums b{font-size:16px}}
.toolbar{display:flex;align-items:center;gap:12px;flex-wrap:wrap}
.toolbar .chips{flex:1;min-width:200px}
.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(min(236px,100%),1fr));gap:20px}
.more{display:flex;justify-content:center;margin-top:6px}
.more .btn{height:40px;padding:0 22px}
.duo{display:grid;grid-template-columns:repeat(auto-fit,minmax(min(420px,100%),1fr));gap:20px}
.duo>*,.heroes>*,.grid>*,.searches>*,.tiers>*,.kpis>*{min-width:0}
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
/* ---------------- Navigation ---------------- */
.nav{position:sticky;top:0;z-index:20;margin:-10px -12px 0;padding:10px 12px;
  background:color-mix(in srgb,var(--bg) 82%,transparent);backdrop-filter:blur(14px);-webkit-backdrop-filter:blur(14px);
  border-bottom:1px solid transparent}
.tabs{display:flex;gap:6px;overflow-x:auto;scrollbar-width:none;padding:2px}
.tabs::-webkit-scrollbar{display:none}
.tabs a{flex:none;display:inline-flex;align-items:center;gap:8px;height:38px;padding:0 15px;border-radius:12px;
  color:var(--t2);font-weight:600;font-size:13.5px;border:1px solid transparent;transition:background .15s,color .15s}
.tabs a:hover{color:var(--t1);background:var(--sunk)}
.tabs a.on{background:var(--surface);color:var(--t1);border-color:var(--b1);box-shadow:var(--sh1)}
.tabs a .n{min-width:22px;height:20px;padding:0 6px;border-radius:999px;display:inline-grid;place-items:center;
  font-size:11.5px;font-weight:700;background:color-mix(in srgb,var(--c,var(--mute)) 16%,transparent);color:var(--c,var(--t2))}
.tabs a svg.i{width:15px;height:15px;color:var(--c,var(--t3))}
.tabs .gap{flex:none;width:1px;background:var(--b1);margin:8px 4px}

/* ---------------- Preisklassen ---------------- */
.t-billig{--c:var(--ok)}.t-mittel{--c:var(--acc)}.t-teuer{--c:var(--warn)}.t-verdaechtig{--c:var(--violet)}
.tiers{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:18px}
.tier{position:relative;display:flex;flex-direction:column;gap:10px;padding:20px 22px;background:var(--surface);
  border:1px solid var(--b1);border-radius:var(--r);box-shadow:var(--sh1);overflow:hidden;
  transition:transform .2s cubic-bezier(.2,.8,.2,1),box-shadow .2s,border-color .2s}
.tier:hover{transform:translateY(-2px);box-shadow:var(--sh2);border-color:color-mix(in srgb,var(--c) 40%,var(--b1))}
.tier::before{content:"";position:absolute;inset:0 0 auto 0;height:3px;background:var(--c)}
.tier .top{display:flex;align-items:center;gap:10px;font-weight:650}
.tier .top .ic{width:30px;height:30px;border-radius:10px;display:grid;place-items:center}
.tier .top .go{margin-left:auto;color:var(--t3)}
.tier .v{font-size:32px;font-weight:750;letter-spacing:-.03em;line-height:1}
.tier .h{color:var(--t3);font-size:12.5px;line-height:1.4}
.tier .thumbs{display:flex;margin-top:2px}
.tier .thumbs img,.tier .thumbs span{width:34px;height:34px;border-radius:10px;object-fit:cover;border:2px solid var(--surface);
  background:var(--sunk);margin-right:-8px}
.phead{display:flex;align-items:flex-start;gap:18px;flex-wrap:wrap}
.phead .ic{width:48px;height:48px;border-radius:15px;display:grid;place-items:center;flex:none}
.phead .ic svg.i{width:24px;height:24px}
.phead h2{margin:0;font-size:24px;font-weight:750;letter-spacing:-.02em}
.phead p{margin:3px 0 0;color:var(--t2);font-size:13.5px;max-width:640px}
.phead .sp{flex:1}
.tbadge{position:absolute;left:10px;bottom:10px;z-index:1;height:24px;padding:0 10px;border-radius:999px;display:inline-flex;
  align-items:center;font-size:11.5px;font-weight:700;color:#fff;background:color-mix(in srgb,var(--c) 88%,black)}

/* ---------------- Aktionen auf Karten ---------------- */
.act{position:absolute;right:10px;bottom:10px;z-index:2;width:32px;height:32px;border-radius:10px;display:grid;place-items:center;
  background:hsl(222 30% 8% / .62);color:#fff;border:1px solid hsl(0 0% 100% / .14);backdrop-filter:blur(8px);
  -webkit-backdrop-filter:blur(8px);cursor:pointer;opacity:0;transform:translateY(4px);transition:opacity .15s,transform .15s,background .15s}
.act svg.i{width:15px;height:15px}
.card:hover .act,.act:focus-visible{opacity:1;transform:none}
.act:hover{background:var(--acc);color:var(--acc-fg);border-color:transparent}
@media (hover:none){.act{opacity:1;transform:none}}
.card.gone{opacity:0;transform:scale(.96);transition:opacity .25s,transform .25s}
.busy{pointer-events:none;opacity:.55}
.why{display:flex;align-items:center;gap:6px;color:var(--t3);font-size:12px}
.why svg.i{width:13px;height:13px}

/* ---------------- Formulare ---------------- */
.btn.pri{background:var(--acc);border-color:var(--acc);color:var(--acc-fg);font-weight:650}
.btn.pri:hover{filter:brightness(1.08);color:var(--acc-fg)}
.btn.dng:hover{color:var(--err);border-color:color-mix(in srgb,var(--err) 50%,var(--b2))}
.btn.sm{height:30px;padding:0 12px;font-size:12.5px}
.btn.sure{background:var(--err);border-color:var(--err);color:#fff}
.form{background:var(--surface);border:1px solid var(--b1);border-radius:var(--r);box-shadow:var(--sh1);padding:26px 28px;
  display:flex;flex-direction:column;gap:22px}
.form h3{margin:0;font-size:17px;font-weight:700;display:flex;align-items:center;gap:10px}
.form h3 svg.i{color:var(--acc)}
.form .note{margin:-12px 0 0;color:var(--t3);font-size:12.5px}
.fg{display:grid;grid-template-columns:repeat(12,minmax(0,1fr));gap:16px 18px}
.f{display:flex;flex-direction:column;gap:7px;grid-column:span 6;min-width:0}
.f.w12{grid-column:span 12}.f.w4{grid-column:span 4}.f.w3{grid-column:span 3}.f.w8{grid-column:span 8}
.f label,.f .lab{font-size:12.5px;font-weight:620;color:var(--t2)}
.f small{color:var(--t3);font-size:11.5px}
.f input,.f select,.f textarea{font:inherit;font-size:14px;color:var(--t1);background:var(--sunk);border:1px solid var(--b1);
  border-radius:11px;height:40px;padding:0 13px;outline:none;transition:border-color .15s,box-shadow .15s;min-width:0;width:100%}
.f textarea{height:auto;min-height:74px;padding:10px 13px;resize:vertical;font-family:ui-monospace,SFMono-Regular,Menlo,monospace;font-size:12.5px}
.f input:focus,.f select:focus,.f textarea:focus{border-color:var(--acc);box-shadow:0 0 0 3px color-mix(in srgb,var(--acc) 22%,transparent)}
.f .unit{position:relative}.f .unit input{padding-right:34px}
.f .unit span{position:absolute;right:12px;top:50%;transform:translateY(-50%);color:var(--t3);font-size:13px;pointer-events:none}
.checks{display:flex;gap:8px;flex-wrap:wrap}
.checks label{display:inline-flex;align-items:center;gap:8px;height:36px;padding:0 13px;border-radius:999px;cursor:pointer;
  background:var(--sunk);border:1px solid var(--b1);color:var(--t2);font-size:13px;font-weight:560;user-select:none}
.checks label{position:relative}
.checks input{position:absolute;opacity:0;pointer-events:none;width:1px!important;height:1px!important;inset:0}
.checks label:has(input:checked){background:color-mix(in srgb,var(--c,var(--acc)) 16%,transparent);
  border-color:color-mix(in srgb,var(--c,var(--acc)) 50%,transparent);color:var(--t1)}
.switch{display:inline-flex;align-items:center;gap:10px;cursor:pointer;font-size:13.5px;color:var(--t2);user-select:none}
.switch input{appearance:none;-webkit-appearance:none;width:38px;height:22px;border-radius:999px;background:var(--b2);position:relative;
  cursor:pointer;transition:background .15s;flex:none;margin:0}
.switch input::after{content:"";position:absolute;top:3px;left:3px;width:16px;height:16px;border-radius:50%;background:#fff;
  transition:transform .15s;box-shadow:0 1px 2px rgb(0 0 0 / .3)}
.switch input:checked{background:var(--acc)}.switch input:checked::after{transform:translateX(16px)}
details.pro summary{cursor:pointer;color:var(--t2);font-size:13px;font-weight:600;list-style:none;display:flex;align-items:center;gap:8px}
details.pro summary::-webkit-details-marker{display:none}
details.pro summary::before{content:"›";display:inline-block;transition:transform .15s;font-size:16px}
details.pro[open] summary::before{transform:rotate(90deg)}
details.pro .f{margin-top:12px}
.formfoot{display:flex;align-items:center;gap:10px;flex-wrap:wrap;padding-top:4px}
.formfoot .msg{color:var(--err);font-size:13px;font-weight:560}
.formfoot .sp{flex:1}

/* ---------------- Suchen-Liste ---------------- */
.slist{display:flex;flex-direction:column;gap:12px}
.srow{display:grid;grid-template-columns:auto minmax(0,1fr) auto;gap:16px;align-items:center;padding:18px 20px;
  background:var(--surface);border:1px solid var(--b1);border-radius:var(--r);box-shadow:var(--sh1)}
.srow.off{opacity:.72}
.srow .st{width:40px;height:40px;border-radius:12px;display:grid;place-items:center}
.srow .mid{min-width:0;display:flex;flex-direction:column;gap:7px}
.srow .t{display:flex;align-items:center;gap:10px;flex-wrap:wrap}
.srow .t b{font-size:15px;font-weight:650}
.srow code{font:12px/1.5 ui-monospace,SFMono-Regular,Menlo,monospace;color:var(--t3);white-space:nowrap;overflow:hidden;
  text-overflow:ellipsis;display:block}
.srow .meta{display:flex;gap:6px;flex-wrap:wrap;align-items:center;color:var(--t3);font-size:12.5px}
.srow .btns{display:flex;gap:6px;flex-wrap:wrap;justify-content:flex-end}
.origin{height:20px;padding:0 8px;border-radius:6px;font-size:10.5px;font-weight:700;letter-spacing:.03em;text-transform:uppercase;
  display:inline-flex;align-items:center;background:var(--sunk);color:var(--t3);border:1px solid var(--b1)}
.origin.own{color:var(--acc);border-color:color-mix(in srgb,var(--acc) 35%,transparent);background:color-mix(in srgb,var(--acc) 10%,transparent)}
.hint{display:flex;gap:12px;align-items:flex-start;padding:14px 16px;border-radius:14px;background:var(--sunk);border:1px solid var(--b1);
  color:var(--t2);font-size:13px}
.hint svg.i{color:var(--acc);margin-top:2px}
.rules{display:grid;grid-template-columns:repeat(auto-fit,minmax(min(200px,100%),1fr));gap:16px 18px;align-items:end}
.rules .f{grid-column:auto}

.toast{position:fixed;left:50%;bottom:24px;transform:translate(-50%,20px);opacity:0;z-index:50;max-width:min(520px,90vw);
  padding:12px 18px;border-radius:14px;background:var(--elev);border:1px solid var(--b2);box-shadow:var(--sh2);
  font-weight:560;font-size:13.5px;transition:opacity .2s,transform .2s;pointer-events:none}
.toast.on{opacity:1;transform:translate(-50%,0)}
.toast.err{border-color:color-mix(in srgb,var(--err) 50%,var(--b2));color:var(--err)}

/* ---------------- Suchleiste ---------------- */
.bar{display:flex;gap:10px;align-items:center;flex:1;min-width:260px}
.bar form{display:flex;gap:8px;align-items:center;flex:1;min-width:0}
.bar .inp{flex:1;min-width:0;position:relative;display:flex;align-items:center}
.bar input{font:inherit;font-size:14px;color:var(--t1);background:var(--surface);border:1px solid var(--b1);
  border-radius:999px;height:42px;padding:0 40px 0 16px;outline:none;width:100%;min-width:0;
  transition:border-color .15s,box-shadow .15s}
.bar .inp svg.i{position:absolute;right:15px;width:15px;height:15px;color:var(--t3);pointer-events:none}
.bar input:focus{border-color:var(--acc);box-shadow:0 0 0 3px color-mix(in srgb,var(--acc) 22%,transparent)}
.bar .btn{height:42px;border-radius:999px}
.bar .btn svg.i{width:15px;height:15px}
.runbox{display:flex;align-items:center;gap:12px;padding:14px 18px;border-radius:var(--r);background:var(--surface);
  border:1px solid var(--b1);box-shadow:var(--sh1);color:var(--t2);font-size:13.5px;flex-wrap:wrap}
.runbox .dot{width:10px;height:10px}
.runbox code{font:12px/1.4 ui-monospace,SFMono-Regular,Menlo,monospace;color:var(--t3);word-break:break-all}
@media (max-width:760px){.bar form{flex-wrap:wrap}}
@media (max-width:1100px){.tiers{grid-template-columns:repeat(2,minmax(0,1fr))}}
@media (max-width:760px){.f,.f.w4,.f.w3,.f.w8{grid-column:span 12}.srow{grid-template-columns:auto minmax(0,1fr)}
  .srow .btns{grid-column:1 / -1;justify-content:flex-start}.form{padding:20px}}
@media (max-width:520px){.tiers{grid-template-columns:1fr}}
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
    "archive": '<rect width="20" height="5" x="2" y="3" rx="1"/><path d="M4 8v11a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8"/>'
               '<path d="M10 12h4"/>',
    "undo": '<path d="M9 14 4 9l5-5"/><path d="M4 9h10.5a5.5 5.5 0 0 1 0 11H11"/>',
    "plus": '<path d="M5 12h14"/><path d="M12 5v14"/>',
    "edit": '<path d="M21.17 6.81a1 1 0 0 0-3.99-3.99L3.84 16.17a2 2 0 0 0-.5.83l-1.32 4.35a.5.5 0 0 0 .62.62l4.35-1.32'
            'a2 2 0 0 0 .83-.5z"/>',
    "pause": '<rect x="14" y="4" width="4" height="16" rx="1"/><rect x="6" y="4" width="4" height="16" rx="1"/>',
    "play": '<polygon points="6 3 20 12 6 21 6 3"/>',
    "trash": '<path d="M3 6h18"/><path d="M19 6v14c0 1-1 2-2 2H7c-1 0-2-1-2-2V6"/><path d="M8 6V4c0-1 1-2 2-2h4c1 0 2 1 2 2v2"/>',
    "home": '<path d="M15 21v-8a1 1 0 0 0-1-1h-4a1 1 0 0 0-1 1v8"/><path d="M3 10a2 2 0 0 1 .71-1.53l7-6a2 2 0 0 1 2.58 0l7 6'
            'A2 2 0 0 1 21 10v9a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2z"/>',
    "tag": '<path d="M12.59 2.59A2 2 0 0 0 11.17 2H4a2 2 0 0 0-2 2v7.17a2 2 0 0 0 .59 1.42l8.7 8.7a2.43 2.43 0 0 0 3.42 0'
           'l6.58-6.58a2.43 2.43 0 0 0 0-3.42z"/><circle cx="7.5" cy="7.5" r=".5" fill="currentColor"/>',
    "up": '<polyline points="22 7 13.5 15.5 8.5 10.5 2 17"/><polyline points="16 7 22 7 22 13"/>',
    "wand": '<path d="m21.64 3.64-1.28-1.28a1.21 1.21 0 0 0-1.72 0L2.36 18.64a1.21 1.21 0 0 0 0 1.72l1.28 1.28a1.2 1.2 0 0 0 '
            '1.72 0L21.64 5.36a1.2 1.2 0 0 0 0-1.72"/><path d="m14 7 3 3"/><path d="M5 6v4"/><path d="M19 14v4"/>'
            '<path d="M10 2v2"/><path d="M7 8H3"/><path d="M21 16h-4"/><path d="M11 3H9"/>',
    "truck": '<path d="M14 18V6a2 2 0 0 0-2-2H4a2 2 0 0 0-2 2v11a1 1 0 0 0 1 1h2"/><path d="M15 18H9"/>'
             '<path d="M19 18h2a1 1 0 0 0 1-1v-3.65a1 1 0 0 0-.22-.62l-3.48-4.35A1 1 0 0 0 17.52 8H14"/>'
             '<circle cx="17" cy="18" r="2"/><circle cx="7" cy="18" r="2"/>',
}


def icon(name: str) -> str:
    return ("<svg class=i viewBox='0 0 24 24' fill=none stroke=currentColor stroke-width=1.9 "
            f"stroke-linecap=round stroke-linejoin=round aria-hidden=true>{_I[name]}</svg>")


PAGE_SIZE = 48
PAGE_MAX = 240
PAGE_SCAN = 400  # so viele Treffer werden fürs Filtern/Sortieren gelesen
SOURCE_SHORT = {"ebay": "eB", "kleinanzeigen": "KA", "vinted": "Vi", "mydealz": "md"}

JS = """
(function(){
  var root = document.documentElement;
  function theme(t){ if(t === 'light'){root.classList.add('light')} else if(t === 'dark'){root.classList.remove('light')} }
  if(window.matchMedia && matchMedia('(prefers-color-scheme: light)').matches){theme('light')}
  var qs = new URLSearchParams(location.search); if(qs.get('theme')){theme(qs.get('theme'))}
  // ---- Schreiben: POST an den eigenen Server (/api/edit -> manage.handle).
  var busy = 0, tt;
  function toast(text, bad){
    var t = document.querySelector('.toast');
    if(!t){ t = document.createElement('div'); t.className = 'toast'; document.body.appendChild(t); }
    t.textContent = text; t.classList.toggle('err', !!bad); t.classList.add('on');
    clearTimeout(tt); tt = setTimeout(function(){ t.classList.remove('on') }, bad ? 5200 : 2600);
  }
  function act(params, el, after){
    busy++; if(el) el.classList.add('busy');
    fetch('/api/edit', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify(params)})
      .then(function(r){ return r.json() })
      .catch(function(){ return {ok:false, message:'Tracker nicht erreichbar'} })
      .then(function(d){
        busy--; if(el) el.classList.remove('busy');
        if(!d.ok){
          var msg = document.querySelector('form .msg');
          if(msg && el && el.tagName === 'FORM'){ msg.textContent = d.message || 'Fehler'; }
          toast(d.message || 'Hat nicht geklappt', true); return;
        }
        if(after){ after(d); return; }
        dirty = false; toast(d.message || 'Erledigt');
        setTimeout(function(){ location.reload() }, 450);
      });
  }
  function fields(form){
    var out = {};
    Array.prototype.forEach.call(form.elements, function(x){
      if(!x.name || x.name.charAt(0) === '_') return;
      if(x.type === 'checkbox'){
        if(x.hasAttribute('data-many')){ out[x.name] = out[x.name] || []; if(x.checked) out[x.name].push(x.value); }
        else out[x.name] = x.checked;
      } else out[x.name] = x.value;
    });
    return out;
  }
  document.addEventListener('submit', function(e){
    var form = e.target.closest('form[data-op]'); if(!form) return;
    e.preventDefault();
    var params = {op: form.getAttribute('data-op')}, f = fields(form);
    if(params.op === 'manual'){
      // Manuelle Suche: Aktion schickt uns auf die Suche-Seite, die den Lauf zeigt.
      var q = (f.line || '').trim(); if(!q) return;
      var here = document.querySelector('.runbox .dot.s-loading');
      if(here){ toast('Eine manuelle Suche läuft schon', true); return; }
      act(params, form, function(d){
        toast(d.message || 'Läuft'); dirty = false;
        setTimeout(function(){ location.href = '/?v=suche' }, 400);
      });
      return;
    }
    if(params.op === 'manual-save'){
      act(params, form, function(d){ dirty = false; toast(d.message || 'Gespeichert');
        setTimeout(function(){ location.href = '/?v=suchen' }, 500); });
      return;
    }
    if(params.op === 'rules'){ params.rules = f; }
    else { params.fields = f; params.id = form.getAttribute('data-id') || ''; params.replaces = form.getAttribute('data-replaces') || ''; }
    act(params, form, function(d){
      dirty = false; toast(d.message || 'Gespeichert');
      var back = form.getAttribute('data-back');
      setTimeout(function(){ if(back){ location.href = back } else { location.reload() } }, 450);
    });
  });
  var dirty = false;
  document.addEventListener('input', function(e){ if(e.target.closest('form')) dirty = true; });
  document.addEventListener('click', function(e){
    var a = e.target.closest('[data-act]');
    if(a){
      e.preventDefault(); e.stopPropagation();
      var op = a.getAttribute('data-act'), q = a.getAttribute('data-confirm');
      // Rückfrage ohne Dialog: der Knopf zeigt die Frage, ein zweiter Klick bestätigt.
      if(q && !a.classList.contains('sure')){
        a.classList.add('sure'); a.setAttribute('data-was', a.innerHTML); a.textContent = q;
        setTimeout(function(){ if(a.classList.contains('sure')){ a.classList.remove('sure'); a.innerHTML = a.getAttribute('data-was') } }, 4000);
        return;
      }
      a.classList.remove('sure');
      var params = {op: op, key: a.getAttribute('data-key') || '', name: a.getAttribute('data-name') || '',
                    tier: a.getAttribute('data-tier') || '', all: a.hasAttribute('data-all')};
      var card = a.closest('.card');
      if((op === 'archive' || op === 'unarchive') && card && !params.all){
        act(params, card, function(d){
          card.classList.add('gone'); toast(d.message);
          setTimeout(function(){ card.remove() }, 260);
        });
      } else { act(params, a); }
      return;
    }
    var el = e.target.closest('[data-open]');
    if(!el) return;
    e.preventDefault();
    window.open(el.getAttribute('data-open'), '_blank', 'noopener');
  });
  // Frische Daten per Reload (alles ist serverseitig gerendert). Nie mitten im Tippen oder während eine Aktion läuft.
  var idle = Date.now();
  ['pointermove','wheel','keydown','touchstart'].forEach(function(n){addEventListener(n,function(){idle=Date.now()},{passive:true})});
  setInterval(function(){
    if(dirty || busy || document.querySelector('form[data-op]')) return;
    if(Date.now()-idle > 20000 && !document.hidden){ location.reload() }
  }, %(reload)d);
})();
"""


def _page(body: str, *, page: bool = False, reload_ms: int = 90_000) -> str:
    script = JS % {"reload": reload_ms}
    return (
        "<!doctype html><html lang=de><head><meta charset=utf-8>"
        "<meta name=viewport content='width=device-width,initial-scale=1'>"
        f"<title>Tracker</title><style>{CSS}</style></head>"
        f"<body class='{'page' if page else 'tilebody'}'>{body}<script>{script}</script></body></html>"
    )


def _qs(**params: Any) -> str:
    """Eine Adresse auf dieser Oberfläche: die Wurzel plus Query."""
    clean = {k: v for k, v in params.items() if v not in (None, "")}
    return "/?" + urlencode(clean) if clean else "/"


# ---------------------------------------------------------------- Bausteine
def _verdict(m: Match) -> str | None:
    return (m.listing.assessment or {}).get("verdict")


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


def _arch_btn(m: Match) -> str:
    return (f"<span class=act role=button tabindex=0 title='Archivieren – aus der Liste nehmen' data-act=archive "
            f"data-key='{esc(m.key)}'>{icon('archive')}</span>")


def _card(m: Match, *, detail: bool = False, hero: bool = False, tier: str | None = None,
          action: str | None = None, below: str = "") -> str:
    l = m.listing
    a = l.assessment or {}
    v = a.get("verdict")
    deal = is_deal(m) and v in DEAL_VERDICTS
    key = quote(m.key, safe="")
    img = (f"<img src='/img/{key}' alt='' loading={'eager' if hero else 'lazy'} decoding=async>"
           if l.image_url else f"<div class='ph src-{esc(l.source)}'>{icon('image')}</div>")
    new = f"<span class='glass new'>Neu</span>" if _is_new(m) else ""
    verdict = f"<span class='verdict v-{esc(v)}'>{esc(a.get('label') or v)}</span>" if v else ""
    if not v and tier:
        verdict = f"<span class='tbadge t-{esc(tier)}'>{esc(TIER_LABEL[tier])}</span>"
    act = _arch_btn(m) if action is None else action
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
        f"<a class='{cls}' href='{esc(l.url)}' data-open='/go/{key}' title='{esc(l.title)}'>"
        f"<div class=media>{img}<div class=tags>{_src(l.source)}{new}</div>{verdict}{act}</div>"
        f"<div class=info><div class=priceline><span class=price>{esc(_money(l.price))}</span>{drop}</div>"
        f"{specs}<div class=ttl>{esc(l.title)}</div>{extra}{foot}{below}</div></a>"
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
            f"<a class='srch{' on' if s.name == sel else ''}' href='{esc(_qs(s=s.name))}#angebote'>"
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
        q = {"s": sel, "f": flt if flt != "alle" else "", "o": order if order != "neu" else ""}
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


def page(engine: Engine, params: dict[str, str]) -> str:
    """Die Vollbild-Seite: Status, Kennzahlen, beste Deals, Suchen, Angebote, Quellen."""
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

    view = params.get("v") if params.get("v") in VIEWS else "uebersicht"
    everything = engine.recent_matches(limit=PAGE_SCAN, include_silent=True)
    all_unique = _unique(everything)
    tier_of = manage.tiers(everything)
    counts = {t: sum(1 for m in all_unique if tier_of.get(m.key) == t) for t in TIERS}
    nav = _nav(view, counts, engine.store.archive_count(), len(engine.parsed))
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
    head = (f"<header class=hdr><div class=logo>{icon('radar')}</div><div><h1>Tracker</h1>"
            f"<div class=status><span class='dot s-{state} pulse'></span>{esc(status)}</div></div>"
            f"<span class=sp></span>{_searchbar(view)}<div class=srcs>{srcs}</div></header>")
    if view != "uebersicht":
        if view in TIERS:
            main = _tier_page(engine, view, all_unique, tier_of, params, limit)
        elif view == "suche":
            main = _manual_page(engine, params, limit)
        elif view == "suchen":
            main = _searches_page(engine, params)
        else:
            main = _archive_page(engine, params, limit)
        body = (f"<div class=wrap>{head}{nav}{main}"
                f"<footer>Tracker {esc(__version__)} · aktualisiert sich von selbst</footer></div>")
        return _page(body, page=True, reload_ms=3_000 if view == "suche" and manual.state_of(engine).get("current", {}).get("status") == "running" else 120_000)
    body = (
        "<div class=wrap>"
        f"<header class=hdr><div class=logo>{icon('radar')}</div><div><h1>Tracker</h1>"
        f"<div class=status><span class='dot s-{state} pulse'></span>{esc(status)}</div></div>"
        f"<span class=sp></span><div class=srcs>{srcs}</div></header>"
        f"{nav}"
        f"{_kpis(engine, unique_all)}"
        f"{_tier_overview(all_unique, tier_of)}"
        f"{_heroes(unique_all)}"
        f"{_search_cards(engine, sel)}"
        f"{_offers(engine, unique, sel, flt, order, limit)}"
        f"{f'<div class=duo>{duo}</div>' if duo else ''}"
        f"<footer>Tracker {esc(__version__)} · aktualisiert sich von selbst</footer>"
        "</div>"
    )
    return _page(body, page=True, reload_ms=120_000)


# ---------------------------------------------------------------- Seiten
VIEWS = ("uebersicht", *TIERS, "suche", "suchen", "archiv")
TIER_ICON = {"billig": "down", "mittel": "tag", "teuer": "up", "verdaechtig": "alert"}


def _unique(matches: list[Match]) -> list[Match]:
    seen: set[str] = set()
    return [m for m in matches if not (m.key in seen or seen.add(m.key))]


def _searchbar(view: str) -> str:
    """Die Suchleiste im Kopf: Enter oder Knopf startet eine Manuelle Suche über alle Quellen."""
    return (
        f"<div class=bar><form data-op=manual><div class=inp>"
        f"<input name=line placeholder='z. B. 4090 – überall suchen' aria-label='Manuelle Suche' "
        f"autocomplete=off spellcheck=false value='' data-line>{icon('search')}</div>"
        f"<button class='btn pri' type=submit title='Einmalig über alle Quellen suchen'>"
        f"{icon('search')}Finden</button></form></div>")


def _nav(view: str, counts: dict[str, int], archived: int, searches: int) -> str:
    def tab(v: str, label: str, ic: str, n: int | None = None, cls: str = "") -> str:
        badge = f"<span class=n>{n}</span>" if n is not None else ""
        return (f"<a class='{'on' if v == view else ''} {cls}' href='{esc(_qs(v=v if v != 'uebersicht' else ''))}'>"
                f"{icon(ic)}{esc(label)}{badge}</a>")
    tabs = [tab("uebersicht", "Übersicht", "home"), "<span class=gap></span>"]
    tabs += [tab(t, TIER_LABEL[t], TIER_ICON[t], counts.get(t, 0), f"t-{t}") for t in TIERS
             if t != "verdaechtig" or counts.get(t)]
    tabs += ["<span class=gap></span>", tab("suche", "Suche", "search"),
             tab("suchen", "Suchen", "search", searches),
             tab("archiv", "Archiv", "archive", archived)]
    return f"<nav class=nav><div class=tabs>{''.join(tabs)}</div></nav>"


def _tier_overview(matches: list[Match], tier_of: dict[str, str]) -> str:
    cards = []
    for t in TIERS:
        group = [m for m in matches if tier_of.get(m.key) == t]
        if t == "verdaechtig" and not group:
            continue
        prices = [m.listing.price for m in group if m.listing.price]
        span = (f"{format_money(min(prices))} – {format_money(max(prices))}" if len(prices) > 1
                else format_money(prices[0]) if prices else "nichts drin")
        thumbs = "".join([f"<img src='/img/{quote(m.key, safe='')}' alt='' loading=lazy>"
                          for m in group if m.listing.image_url][:5])
        cards.append(
            f"<a class='tier t-{t}' href='{esc(_qs(v=t))}'>"
            f"<div class=top><span class='ic tint'>{icon(TIER_ICON[t])}</span>{esc(TIER_LABEL[t])}"
            f"<span class=go>{icon('out')}</span></div>"
            f"<div class=v>{len(group)}</div><div class=h>{esc(span)}<br>{esc(TIER_HINT[t])}</div>"
            f"{f'<div class=thumbs>{thumbs}</div>' if thumbs else ''}</a>")
    return (f"<section class=sec><div class=sech><h2>{icon('tag')}Preisklassen</h2>"
            "<span class=cnt>jede Klasse hat ihre eigene Seite</span></div>"
            f"<div class=tiers>{''.join(cards)}</div></section>")


def _search_chips(engine: Engine, sel: str, **keep: str) -> str:
    chips = [f"<a class='chip {'on' if not sel else ''}' href='{esc(_qs(**keep))}'>Alle Suchen</a>"]
    chips += [f"<a class='chip {'on' if s.name == sel else ''}' href='{esc(_qs(s=s.name, **keep))}'>"
              f"{esc(s.name)}</a>" for s in engine.searches]
    return f"<div class=chips>{''.join(chips)}</div>"


def _sorted(matches: list[Match], order: str) -> list[Match]:
    if order == "preis":
        return sorted(matches, key=lambda m: (m.listing.price is None, m.listing.price or 0))
    if order == "wert":
        def ratio(m: Match) -> float:
            a = m.listing.assessment or {}
            return m.listing.price / a["fair_price"] if a.get("fair_price") and m.listing.price else 9.0
        return sorted(matches, key=ratio)
    return matches


def _tier_page(engine: Engine, tier: str, matches: list[Match], tier_of: dict[str, str],
               params: dict[str, str], limit: int) -> str:
    sel = params.get("s") or ""
    if sel not in {s.name for s in engine.searches}:
        sel = ""
    order = params.get("o") if params.get("o") in ORDERS else ("preis" if tier == "billig" else "neu")
    group = [m for m in matches if tier_of.get(m.key) == tier and (not sel or m.search == sel)]
    group = _sorted(group, order)
    keep = {"v": tier, "o": order}
    seg_o = "".join(f"<a class='{'on' if k == order else ''}' href='{esc(_qs(s=sel, v=tier, o=k))}'>{v}</a>"
                    for k, v in ORDERS.items())
    bulk = ""
    if group:
        bulk = (f"<button class='btn dng' data-act=archive-tier data-tier='{tier}' "
                f"data-confirm='Wirklich alle {len(group)} archivieren?'>{icon('archive')}Alle archivieren</button>")
    if group:
        grid = "<div class=grid>" + "".join(_card(m, detail=True, tier=tier) for m in group[:limit]) + "</div>"
    else:
        grid = (f"<div class=nodeal><div class='ic tint t-{tier}'>{icon(TIER_ICON[tier])}</div><div>"
                f"<b>Gerade nichts in „{esc(TIER_LABEL[tier])}“</b>Sobald etwas passt, landet es hier.</div></div>")
    more = ""
    if len(group) > limit:
        more = (f"<div class=more><a class=btn href='{esc(_qs(s=sel, n=str(min(limit + PAGE_SIZE, PAGE_MAX)), **keep))}'>"
                f"Mehr anzeigen · noch {len(group) - limit}</a></div>")
    rules = manage.rules(engine.store)
    auto = ""
    if tier == "teuer" and rules["enabled"] and rules["expensive_hours"]:
        auto = f" Teure Angebote wandern nach {rules['expensive_hours']} h automatisch ins Archiv."
    return (
        f"<section class=sec><div class=phead><div class='ic tint t-{tier}'>{icon(TIER_ICON[tier])}</div>"
        f"<div><h2>{esc(TIER_LABEL[tier])} <span class=cnt style='color:var(--t3);font-weight:550'>{len(group)}</span></h2>"
        f"<p>{esc(TIER_HINT[tier])}.{esc(auto)} Mit {icon('archive').replace('<svg class=i', '<svg class=i style=display:inline;vertical-align:-3px;width:14px;height:14px')} "
        "auf einer Karte nimmst du sie aus der Liste.</p></div>"
        f"<span class=sp></span>{bulk}</div>"
        f"<div class=toolbar>{_search_chips(engine, sel, **keep)}<div class=seg>{seg_o}</div></div>"
        f"{grid}{more}</section>"
    )


SOURCE_CHOICES = ("ebay", "kleinanzeigen", "vinted", "mydealz")


def _opt(value: str, current: str, label: str) -> str:
    return f"<option value='{esc(value)}'{' selected' if value == current else ''}>{esc(label)}</option>"


def _search_form(engine: Engine, edit: str | None) -> str:
    target = next((p for p in engine.parsed if p.search is not None and p.search.name == edit), None) if edit else None
    f = manage.fields_of(target.line) if target is not None else {}
    title = f"„{target.search.name}“ bearbeiten" if target is not None else "Neue Suche"
    attrs = ""
    note = ("Tipp: Mehrere Suchbegriffe mit | trennen, dann werden alle abgefragt und die Treffer zusammengeführt.")
    if target is not None and target.origin == "custom":
        attrs = f" data-id='{esc(target.cid)}'"
    elif target is not None:
        attrs = f" data-replaces='{esc(target.search.name)}'"
        note = ("Diese Suche steht in Nests Einstellungen. Speichern übernimmt sie als eigene Suche; "
                "die Zeile in Nest wird dann hier ausgeblendet (Verlauf bleibt).")

    def val(k: str) -> str:
        return esc(str(f.get(k) or ""))

    srcs = set(f.get("sources") or [])
    checks = "".join(
        f"<label class=src-{s}><input type=checkbox name=sources value={s} data-many{' checked' if s in srcs else ''}>"
        f"<span class=dot></span>{esc(SOURCE_LABELS.get(s, s))}</label>" for s in SOURCE_CHOICES)
    default = ", ".join(SOURCE_LABELS.get(s, s) for s in engine.settings.default_sources)
    ev = str(f.get("evaluate") or "")
    back = esc(_qs(v="suchen"))
    return (
        f"<form class=form data-op=search-save data-back='{back}'{attrs}>"
        f"<h3>{icon('plus' if target is None else 'edit')}{esc(title)}</h3><p class=note>{esc(note)}</p>"
        "<div class=fg>"
        f"<div class='f w4'><label for=f-name>Name</label><input id=f-name name=name value='{val('name')}' "
        "placeholder='z. B. RTX 4070 günstig' maxlength=60></div>"
        f"<div class='f w8'><label for=f-q>Wonach suchen?</label><input id=f-q name=query required value='{val('query')}' "
        "placeholder='rtx 4070 | rtx 4070 super'></div>"
        f"<div class='f w3'><label for=f-min>Preis ab</label><div class=unit><input id=f-min name=min inputmode=decimal "
        f"value='{val('min')}' placeholder='0'><span>€</span></div></div>"
        f"<div class='f w3'><label for=f-max>Preis bis</label><div class=unit><input id=f-max name=max inputmode=decimal "
        f"value='{val('max')}' placeholder='600'><span>€</span></div></div>"
        f"<div class='f w3'><label for=f-cond>Zustand</label><select id=f-cond name=condition>"
        f"{_opt('', str(f.get('condition') or ''), 'Egal')}{_opt('gebraucht', str(f.get('condition') or ''), 'Gebraucht')}"
        f"{_opt('neu', str(f.get('condition') or ''), 'Neu')}</select></div>"
        f"<div class='f w3'><label for=f-buy>Kaufart (eBay)</label><select id=f-buy name=buying>"
        f"{_opt('', str(f.get('buying') or ''), 'Egal')}{_opt('sofort', str(f.get('buying') or ''), 'Sofort-Kaufen')}"
        f"{_opt('auktion', str(f.get('buying') or ''), 'Auktion')}</select></div>"
        f"<div class='f w12'><span class=lab>Wo suchen?</span><div class=checks>{checks}</div>"
        f"<small>Nichts angehakt = Standard ({esc(default)})</small></div>"
        f"<div class='f w4'><label for=f-must>Muss im Titel stehen</label><input id=f-must name=must value='{val('must')}' "
        "placeholder='32gb, ryzen'><small>alle davon, mit Komma</small></div>"
        f"<div class='f w4'><label for=f-any>Mindestens eins davon</label><input id=f-any name=any value='{val('any')}' "
        "placeholder='i7, ryzen 7'></div>"
        f"<div class='f w4'><label for=f-ex>Ausschließen</label><input id=f-ex name=exclude value='{val('exclude')}' "
        "placeholder='defekt, bastler, tausch'></div>"
        f"<div class='f w3'><label for=f-ev>Preis bewerten</label><select id=f-ev name=evaluate>"
        f"{_opt('', ev, 'Nein – nur Preisspanne')}{_opt('pc', ev, 'Gaming-PC (Teilewert)')}"
        f"{_opt('gpu', ev, 'Grafikkarte (Marktwert)')}{_opt('ram', ev, 'RAM (Preis je GB)')}</select></div>"
        f"<div class='f w3'><label for=f-deal>Nur melden bis</label><div class=unit><input id=f-deal name=deal inputmode=decimal "
        f"value='{val('deal')}' placeholder='90'><span>%</span></div><small>vom Marktwert (mit Bewertung)</small></div>"
        f"<div class='f w3'><label for=f-gpu>Mind. Grafikkarte</label><input id=f-gpu name=gpu value='{val('gpu')}' "
        "placeholder='3070'></div>"
        f"<div class='f w3'><label for=f-ram>Mind. RAM</label><div class=unit><input id=f-ram name=ram inputmode=numeric "
        f"value='{val('ram')}' placeholder='16'><span>GB</span></div></div>"
        f"<div class='f w3'><label for=f-plz>PLZ (Kleinanzeigen)</label><input id=f-plz name=plz inputmode=numeric maxlength=5 "
        f"value='{val('plz')}' placeholder='{esc(engine.settings.kleinanzeigen_plz or '10115')}'></div>"
        f"<div class='f w3'><label for=f-rad>Umkreis</label><div class=unit><input id=f-rad name=radius inputmode=numeric "
        f"value='{val('radius')}' placeholder='{engine.settings.kleinanzeigen_radius or 50}'><span>km</span></div></div>"
        "</div>"
        "<details class=pro><summary>Profi: Suchzeile direkt schreiben</summary>"
        "<div class='f w12'><textarea name=line placeholder='name:&quot;Gaming-PC&quot; gaming pc | pc rtx 400-1000 bewertung:pc "
        "deal:0.9 -defekt @ka'></textarea><small>Wenn hier etwas steht, zählt nur diese Zeile.</small></div></details>"
        "<div class=formfoot>"
        f"<label class=switch><input type=checkbox name=paused{' checked' if f.get('paused') else ''}>Pausiert anlegen</label>"
        "<span class=msg></span><span class=sp></span>"
        f"{f'<a class=btn href={back}>Abbrechen</a>' if target is not None else ''}"
        f"<button class='btn pri' type=submit>{icon('check')}Speichern</button></div></form>"
    )


def _searches_page(engine: Engine, params: dict[str, str]) -> str:
    states = engine.store.all_search_states()
    rows = []
    for p in engine.parsed:
        if p.search is None:
            rows.append(f"<div class=srow><div class='st tint s-error'>{icon('alert')}</div><div class=mid>"
                        f"<div class=t><b>Nicht verstanden</b></div><code>{esc(p.line)}</code>"
                        f"<div class=meta>{esc(p.error)}</div></div></div>")
            continue
        s = p.search
        st = _search_state(engine, s.name)
        row = states.get(s.name)
        own = p.origin == "custom"
        price = ("bis " + format_money(s.max_price) if s.max_price is not None and s.min_price is None else
                 f"{format_money(s.min_price)} – {format_money(s.max_price)}" if s.max_price is not None else
                 "ab " + format_money(s.min_price) if s.min_price is not None else "")
        meta = "".join(_src(x, glass=False) for x in s.sources)
        if price:
            meta += f"<span class='pill tint s-paused'>{esc(price)}</span>"
        if s.evaluate:
            meta += f"<span class='pill tint v-gut'>Bewertung {esc(s.evaluate.upper())}</span>"
        tracked = engine.store.count_tracked(s.name)
        meta += f"<span>{tracked} gemerkt · geprüft {esc(ago(parse_iso(row['last_run'])) if row and row['last_run'] else '–')}</span>"
        name = esc(s.name)
        btns = (f"<a class='btn sm' href='{esc(_qs(v='suchen', edit=s.name))}#form'>{icon('edit')}Bearbeiten</a>"
                f"<button class='btn sm' data-act=search-toggle data-name='{name}'>"
                f"{icon('play' if not s.enabled else 'pause')}{'Fortsetzen' if not s.enabled else 'Pausieren'}</button>"
                f"<button class='btn sm dng' data-act=search-delete data-name='{name}' data-confirm='Wirklich entfernen?'>"
                f"{icon('trash')}Entfernen</button>")
        rows.append(
            f"<div class='srow{' off' if not s.enabled else ''}'><div class='st tint s-{st}'>{icon('search')}</div>"
            f"<div class=mid><div class=t><b>{name}</b><span class='origin{' own' if own else ''}'>"
            f"{'Eigene' if own else 'Nest-Einstellung'}</span></div>"
            f"<code>{esc(p.line)}</code><div class=meta>{meta}</div></div><div class=btns>{btns}</div></div>")
    hidden = engine.hidden_searches
    hid = ""
    if hidden:
        items = "".join(
            f"<div class='srow off'><div class='st tint s-paused'>{icon('eye')}</div><div class=mid><div class=t><b>{esc(n)}</b>"
            "<span class=origin>ausgeblendet</span></div><div class=meta>Steht in Nests Einstellungen, hier ausgeblendet "
            "oder als eigene Suche übernommen.</div></div><div class=btns>"
            f"<button class='btn sm' data-act=search-restore data-name='{esc(n)}'>{icon('undo')}Zurückholen</button></div></div>"
            for n in hidden)
        hid = (f"<section class=sec><div class=sech><h2>{icon('eye')}Ausgeblendet</h2><span class=cnt>{len(hidden)}</span></div>"
               f"<div class=slist>{items}</div></section>")
    edit = params.get("edit")
    return (
        f"<section class=sec id=form>{_search_form(engine, edit)}</section>"
        f"<section class=sec><div class=sech><h2>{icon('search')}Alle Suchen</h2><span class=cnt>{len(engine.parsed)}</span>"
        f"<span class=sp></span></div><div class=slist>{''.join(rows) or '<div class=hint>Noch keine Suche – oben eine anlegen.</div>'}"
        f"</div></section>{hid}"
    )


def _manual_page(engine: Engine, params: dict[str, str], limit: int) -> str:
    """Die Suche-Seite: Leiste, laufender Lauf mit Quelle-Stand, Ergebnisse als Karten."""
    st = manual.state_of(engine)
    run = st.get("current") or {}
    line = str(params.get("q") or st.get("line") or "")
    name = run.get("name") or st.get("name") or ""
    head = (
        f"<div class=phead><div class='ic tint v-gut'>{icon('search')}</div>"
        "<div><h2>Manuelle Suche <span class=cnt style='color:var(--t3);font-weight:550'>einmalig, über alle Quellen</span></h2>"
        "<p>Sucht jetzt einmal überall – ohne dauerhafte Beobachtung und ohne Meldung. Gefällt dir, "
        "was sie findet? Unten „Als Suche speichern“ – dann beobachtet der Tracker das ab jetzt laufend.</p></div></div>"
    )
    runbox = ""
    if run.get("status") == "running":
        runbox = (f"<div class=runbox><span class='dot s-loading pulse'></span><b>Sucht gerade „{esc(run.get('name') or '')}“</b>"
                  f"<code>{esc(run.get('line') or '')}</code><span class=sp></span>"
                  "<span class=cnt>Diese Seite aktualisiert sich von selbst …</span></div>")
    elif run.get("status") == "error":
        runbox = (f"<div class=runbox><span class='dot s-error'></span><b>Suche fehlgeschlagen</b>"
                  f"<span>{esc(run.get('error') or '')}</span></div>")
    if run and run.get("status") != "running":
        bits = [f"<span class='pill tint s-ok'>{esc(SOURCE_LABELS.get(k.replace(' (Fehler)',''), k.replace(' (Fehler)','')))}"
                f"{'!' if '(Fehler)' in k else ''} · {esc(v)}</span>" for k, v in (run.get("per_source") or {}).items()]
        runbox += (f"<div class=runbox><span class='dot s-ok'></span><b>„{esc(name)}“ beendet</b>{''.join(bits)}"
                   f"<span class=sp></span>"
                   f"<button class='btn' data-act=manual data-op2=clear>{icon('trash')}Verwerfen</button>"
                   f"<form data-op=manual-save style='display:contents'>"
                   f"<input type=hidden name=line value='{esc(run.get('line') or '')}'>"
                   f"<button class='btn pri' type=submit>{icon('plus')}Als Suche speichern</button></form></div>")
    listing_view = manual.results_of(engine, st)
    seen: set[str] = set()
    uniq = [m for m in (Match(search=name, key=l.key, first_seen=now(), notified=0, silent=0, listing=l)
                        for l in listing_view) if not (m.key in seen or seen.add(m.key))]
    if uniq:
        shown = uniq[:limit]
        grid = "<div class=grid>" + "".join(_card(m, detail=True) for m in shown) + "</div>"
        more = (f"<div class=more><a class=btn href='{esc(_qs(v='suche', n=str(min(limit + PAGE_SIZE, PAGE_MAX))))}'>"
                f"Mehr anzeigen · noch {len(uniq) - limit}</a></div>") if len(uniq) > limit else ""
    else:
        grid = (f"<div class=nodeal><div class='ic tint v-gut'>{icon('search')}</div><div>"
                + ("<b>Suche läuft …</b>In ein paar Sekunden steht sie hier. Einfach warten, die Seite lädt von selbst."
                   if run.get("status") == "running" else
                   "<b>Kein Ergebnis</b>Oben in der Leiste nach etwas anderem suchen – oder einen der Suchvorschläge unten nehmen.</div>")
                + "</div></div>")
        more = ""
    history = st.get("history") or []
    chips = "".join(
        f"<a class=chip href='{esc(_qs(v='suche', q=h.get('line') or ''))}'>{esc(h.get('name') or h.get('query') or '')}</a>"
        for h in history[:8])
    hist = f"<div class=toolbar><div class=chips>{chips}</div></div>" if chips else ""
    return (f"<section class=sec>{head}{runbox}</section><section class=sec>{grid}{more}</section>{hist}")


def _archive_page(engine: Engine, params: dict[str, str], limit: int) -> str:
    r = manage.rules(engine.store)
    rules = (
        "<form class=form data-op=rules>"
        f"<h3>{icon('wand')}Automatisch aufräumen</h3>"
        "<p class=note>Damit die Liste nicht nervt: Angebote wandern von selbst ins Archiv. Gelöscht wird nichts, "
        "und ein Klick holt sie zurück.</p>"
        "<div class=rules>"
        f"<label class=switch><input type=checkbox name=enabled{' checked' if r['enabled'] else ''}>Automatisch archivieren</label>"
        f"<div class=f><label>Älter als</label><div class=unit><input name=max_age_days inputmode=numeric value='{r['max_age_days']}'>"
        "<span>Tage</span></div></div>"
        f"<div class=f><label>„Teuer“ nach</label><div class=unit><input name=expensive_hours inputmode=numeric "
        f"value='{r['expensive_hours']}'><span>h</span></div><small>0 = nie</small></div>"
        f"<div class=f><label>Nicht mehr online seit</label><div class=unit><input name=gone_hours inputmode=numeric "
        f"value='{r['gone_hours']}'><span>h</span></div><small>vermutlich verkauft</small></div>"
        f"<label class=switch><input type=checkbox name=mute_archived{' checked' if r['mute_archived'] else ''}>"
        "Von Hand Archiviertes nie wieder melden</label>"
        "</div><div class=formfoot><span class=msg></span><span class=sp></span>"
        f"<button class='btn pri' type=submit>{icon('check')}Regeln speichern</button></div></form>"
    )
    items = engine.recent_matches(limit=PAGE_SCAN, include_silent=True, archived=True)
    items = _unique(items)
    cards = []
    for m in items[:limit]:
        restore = (f"<span class=act role=button tabindex=0 title='Zurückholen' data-act=unarchive "
                   f"data-key='{esc(m.key)}'>{icon('undo')}</span>")
        why = (f"<div class=why>{icon('archive')}<span>{esc(m.archived_reason or 'archiviert')} · "
               f"{esc(ago(m.archived_at))}</span></div>")
        cards.append(_card(m, action=restore, below=why))
    if cards:
        grid = "<div class=grid>" + "".join(cards) + "</div>"
        bulk = (f"<button class=btn data-act=unarchive data-all data-confirm='Alle {len(items)} zurückholen?'>"
                f"{icon('undo')}Alle zurückholen</button>")
    else:
        grid = (f"<div class=nodeal><div class='ic tint s-paused'>{icon('archive')}</div><div><b>Archiv ist leer</b>"
                "Mit dem Archiv-Knopf auf einer Karte nimmst du ein Angebot aus der Liste.</div></div>")
        bulk = ""
    return (f"<section class=sec>{rules}</section>"
            f"<section class=sec><div class=sech><h2>{icon('archive')}Archiv</h2><span class=cnt>{len(items)}</span>"
            f"<span class=sp></span>{bulk}</div>{grid}</section>")


GO_PREFIXES = ("https://www.ebay.", "https://www.kleinanzeigen.de/", "https://www.vinted.", "https://www.mydealz.de/")


def go(target_url: str | None) -> str:
    """Weiterleitung zum Marktplatz, nur zu bekannten Zielen (kein offener Redirect)."""
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
