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
from datetime import timedelta
from typing import Any
from urllib.parse import quote, urlencode

from ebay_tracker.models import SOURCE_LABELS, format_money

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


CSS = """
:root{
  --bg:#0f1115;--card:#171a21;--card2:#1d2129;--line:#262b35;--fg:#eef1f6;--mut:#8c95a5;
  --acc:#4f8cff;--ok:#34c77b;--warn:#f0a93b;--err:#ef5b5b;--r:14px;
  color-scheme:dark;
}
html.light{
  --bg:#f5f6f8;--card:#ffffff;--card2:#f1f3f6;--line:#e3e6eb;--fg:#15181e;--mut:#667085;
  --acc:#2f6fed;color-scheme:light;
}
*{box-sizing:border-box}
html,body{margin:0;height:100%;background:transparent}
body{font:14px/1.45 -apple-system,BlinkMacSystemFont,"SF Pro Text","Segoe UI",system-ui,sans-serif;
  color:var(--fg);-webkit-font-smoothing:antialiased;overflow:hidden}
a{color:inherit;text-decoration:none}
.wrap{height:100%;display:flex;flex-direction:column;gap:14px;padding:18px 20px}
.top{display:flex;align-items:center;gap:14px;min-width:0}
.title{font-weight:650;font-size:15px;white-space:nowrap}
.sub{color:var(--mut);font-size:12.5px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.spacer{flex:1}
.chips{display:flex;gap:8px;overflow-x:auto;scrollbar-width:none;min-width:0}
.chips::-webkit-scrollbar{display:none}
.chip{flex:none;padding:6px 12px;border-radius:999px;background:var(--card2);color:var(--mut);
  font-size:12.5px;font-weight:550;white-space:nowrap;border:1px solid transparent}
.chip.on{background:var(--acc);color:#fff}
.chip .dot{display:inline-block;width:7px;height:7px;border-radius:50%;margin-right:6px;vertical-align:1px}
.btn{flex:none;padding:6px 12px;border-radius:999px;border:1px solid var(--line);color:var(--mut);font-size:12.5px;
  background:transparent;cursor:pointer;white-space:nowrap}
.btn:hover{color:var(--fg)}
.strip{flex:1;min-height:0;display:flex;gap:14px;overflow-x:auto;overflow-y:hidden;padding-bottom:4px;
  scroll-snap-type:x proximity}
.strip .card{width:200px;flex:none;scroll-snap-align:start}
.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(220px,1fr));gap:18px}
.card{display:flex;flex-direction:column;background:var(--card);border:1px solid var(--line);
  border-radius:var(--r);overflow:hidden;cursor:pointer;transition:transform .15s,border-color .15s;min-height:0}
.card:hover{transform:translateY(-2px);border-color:var(--acc)}
.img{position:relative;background:var(--card2);aspect-ratio:4/3;flex:none;overflow:hidden}
.strip .img{aspect-ratio:auto;flex:1;min-height:60px}
.img img{width:100%;height:100%;object-fit:cover;display:block}
.img .ph{position:absolute;inset:0;display:grid;place-items:center;color:var(--mut);font-size:26px}
.badge{position:absolute;left:8px;top:8px;padding:3px 8px;border-radius:999px;font-size:11px;font-weight:650;
  background:rgba(15,17,21,.78);color:#fff;backdrop-filter:blur(6px)}
.badge.new{left:auto;right:8px;background:var(--acc)}
.body{padding:10px 12px 12px;display:flex;flex-direction:column;gap:3px;min-width:0}
.price{font-weight:700;font-size:15px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.name{font-size:12.5px;line-height:1.35;display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical;
  overflow:hidden;overflow-wrap:anywhere}
.strip .name{-webkit-line-clamp:1}
.strip .specs + .name{display:none}
.meta{color:var(--mut);font-size:11.5px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.specs{font-size:11.5px;font-weight:600;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.verdict{position:absolute;left:8px;bottom:8px;padding:3px 8px;border-radius:999px;font-size:11px;font-weight:700;color:#fff}
.verdict.top{background:#16a34a}.verdict.gut{background:#22a06b}.verdict.fair{background:#5b6472}
.verdict.teuer{background:#8a5a2b}.verdict.verdaechtig{background:#d97706}
.pc{list-style:none;margin:6px 0 0;padding:0;font-size:11.5px;line-height:1.4}
.pc li{white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.pc .p{color:var(--ok)}.pc .c{color:var(--warn)}
.empty{flex:1;display:grid;place-items:center;text-align:center;color:var(--mut);padding:10px 24px}
.empty b{display:block;color:var(--fg);font-size:15px;margin-bottom:4px}
/* ---------- Vollbild-Seite ---------- */
body.page{overflow:auto;background:var(--bg)}
.page .wrap{height:auto;max-width:1280px;margin:0 auto;padding:40px 32px 64px;gap:28px}
.page h1{font-size:26px;margin:0;letter-spacing:-.01em}
.stats{display:grid;grid-template-columns:repeat(auto-fit,minmax(180px,1fr));gap:18px}
.stat{background:var(--card);border:1px solid var(--line);border-radius:var(--r);padding:18px 20px}
.stat .v{font-size:28px;font-weight:700;letter-spacing:-.02em}
.stat .l{color:var(--mut);font-size:12.5px;margin-top:2px}
.section{display:flex;flex-direction:column;gap:14px}
.section h2{font-size:16px;margin:0}
.searches{display:grid;grid-template-columns:repeat(auto-fill,minmax(300px,1fr));gap:14px}
.srch{background:var(--card);border:1px solid var(--line);border-radius:var(--r);padding:16px 18px;
  display:flex;flex-direction:column;gap:6px;min-width:0}
.srch .h{display:flex;align-items:center;gap:8px;font-weight:600;min-width:0}
.srch .h span{overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.srch code{font:12px/1.4 ui-monospace,Menlo,monospace;color:var(--mut);overflow-wrap:anywhere}
.srch .m{color:var(--mut);font-size:12.5px}
.srch .e{color:var(--err);font-size:12.5px;overflow-wrap:anywhere}
.dot{width:8px;height:8px;border-radius:50%;flex:none;background:var(--mut)}
.dot.ok{background:var(--ok)}.dot.warn{background:var(--warn)}.dot.error{background:var(--err)}
.dot.loading{background:var(--acc)}.dot.paused{background:var(--mut);opacity:.5}
"""

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
        f"<body class='{'page' if page else ''}'>{body}<script>{script}</script></body></html>"
    )


# Der Basispfad, den die Seitenfunktionen gerade verwenden. Wird pro Anfrage
# von widget()/page() gesetzt (thread-lokal, weil ThreadingHTTPServer jede
# Anfrage in ihrem Thread bedient) und von den relativen URL-Helfern gelesen.
import threading
_base = threading.local()


def _b() -> str:
    """Der Basispfad der aktuellen Anfrage: "" auf der Subdomain, sonst
    `/plugins/frame/<label>/`. Ein Modul-globaler Thread-Status ist hier
    vertretbar, weil genau eine Anfrage einen Thread bedient und der Wert
    vor dem Rendern gesetzt wird."""
    return getattr(_base, "path", "") or ""


def _qs(**params: Any) -> str:
    """Eine relative URL auf dieser Oberfläche. Ohne Basis (Subdomain-Form)
    ist es `/` bzw. `/?…`; mit Basis wird daraus der Pfad unter dem Mount
    (`/arthur-tracker/`, `/arthur-tracker/?s=…`) -- ohne doppelten Slash,
    weil BASE schon mit / endet."""
    clean = {k: v for k, v in params.items() if v not in (None, "")}
    if clean:
        return _b() + "?" + urlencode(clean) if _b() else "/?" + urlencode(clean)
    return _b() if _b() else "/"


def _card(m: Match, *, fresh_hours: int = 24, detail: bool = False) -> str:
    l = m.listing
    a = l.assessment or {}
    is_new = not m.silent and m.first_seen and (now() - m.first_seen) < timedelta(hours=fresh_hours)
    img = (f"<img src='{_b()}img/{quote(m.key, safe='')}' alt='' loading=lazy>" if l.image_url
           else "<div class=ph>▢</div>")
    meta = " · ".join(x for x in (l.location, ago(m.first_seen)) if x)
    new_badge = "<span class='badge new'>Neu</span>" if is_new else ""
    verdict = (f"<span class='verdict {esc(a.get('verdict'))}'>{esc(a.get('label'))}</span>"
               if a.get("verdict") else "")
    specs = f"<div class=specs>{esc(a.get('summary'))}</div>" if a.get("summary") else ""
    points = ""
    if detail and a:
        items = [f"<li class=p>✓ {esc(x)}</li>" for x in a.get("pros", [])[1:4]]
        items += [f"<li class=c>✗ {esc(x)}</li>" for x in a.get("cons", [])[:3]]
        points = f"<ul class=pc>{''.join(items)}</ul>" if items else ""
    return (
        f"<a class=card href='{esc(l.url)}' data-open='{_b()}go/{quote(m.key, safe='')}' title='{esc(l.title)}'>"
        f"<div class=img>{img}<span class=badge>{esc(l.source_label)}</span>{new_badge}{verdict}</div>"
        f"<div class=body><div class=price>{esc(l.price_text)}</div>{specs}"
        f"<div class=name>{esc(l.title)}</div><div class=meta>{esc(meta)}</div>{points}</div></a>"
    )


def _search_state(engine: Engine, name: str) -> str:
    """CSS-Klasse des Punkts. Bewusst nicht "empty": das ist schon der Leer-Zustand der Seite."""
    search = next((s for s in engine.searches if s.name == name), None)
    if search is not None and not search.enabled:
        return "paused"
    row = engine.store.all_search_states().get(name)
    if row is None or row["last_run"] is None:
        return "loading"
    if row["last_error"]:
        return "error"
    return "warn" if row["source_errors"] else "ok"


def widget(engine: Engine, params: dict[str, str], base: str = "") -> str:
    """Die Kachel: Kopfzeile mit Filtern, darunter eine waagerechte Kartenleiste."""
    _base.path = base
    sel = params.get("s") or ""
    names = [s.name for s in engine.searches]
    if sel not in names:
        sel = ""
    chips = [f"<a class='chip {'on' if not sel else ''}' href='{_qs()}'>Alle</a>"]
    for name in names:
        dot = f"<span class='dot {_search_state(engine, name)}'></span>"
        chips.append(f"<a class='chip {'on' if name == sel else ''}' href='{esc(_qs(s=name))}'>{dot}{esc(name)}</a>")
    last = engine.store.get_meta("last_run") or {}
    when = "läuft …" if engine.running else f"geprüft {ago(parse_iso(last.get('finished')))}"
    if not engine.running and engine.next_run_at:
        when += f" · nächste {until(engine.next_run_at)}"

    matches = engine.recent_matches(limit=24, search=sel or None, include_silent=True)
    if not engine.searches:
        content = ("<div class=empty><div><b>Noch keine Suche</b>In Nest → Plugins → Tracker → Einstellungen "
                   "eine Zeile wie <code>thinkpad t14 max:600 -defekt</code> anlegen.</div></div>")
    elif not matches:
        content = ("<div class=empty><div><b>Noch keine passenden Angebote</b>"
                   f"{'Die erste Suche läuft gerade.' if not last else 'Sobald etwas auftaucht, steht es hier.'}</div></div>")
    else:
        content = "<div class=strip>" + "".join(_card(m) for m in matches) + "</div>"

    body = (
        "<div class=wrap><div class=top>"
        f"<div class=title>Tracker</div><div class=sub>{esc(when)}</div><div class=spacer></div>"
        f"<div class=chips>{''.join(chips)}</div>"
        f"<button class=btn data-open='{_b()}?layout=page'>Alles ansehen ↗</button>"
        f"</div>{content}</div>"
    )
    return _page(body, base=base)


def page(engine: Engine, params: dict[str, str], base: str = "") -> str:
    """Die Vollbild-Seite: Kennzahlen, Suchen mit Zustand, alle Treffer als Raster."""
    _base.path = base
    sel = params.get("s") or ""
    names = [s.name for s in engine.searches]
    if sel not in names:
        sel = ""
    day = now() - timedelta(hours=24)
    last = engine.store.get_meta("last_run") or {}
    stats = [
        (str(engine.count_new_since(day)), "neue Treffer in 24 h"),
        (str(engine.count_new_since(now() - timedelta(days=7))), "neue Treffer in 7 Tagen"),
        (str(len(engine.active_searches)), "aktive Suchen"),
        (ago(parse_iso(last.get("finished"))) if last else "–", "zuletzt geprüft"),
    ]
    stat_html = "".join(f"<div class=stat><div class=v>{esc(v)}</div><div class=l>{esc(l)}</div></div>" for v, l in stats)

    states = engine.store.all_search_states()
    cards = []
    for p in engine.parsed:
        if p.search is None:
            cards.append(f"<div class=srch><div class=h><span class='dot error'></span><span>Nicht verstanden</span></div>"
                         f"<code>{esc(p.line)}</code><div class=e>{esc(p.error)}</div></div>")
            continue
        s = p.search
        row = states.get(s.name)
        st = _search_state(engine, s.name)
        bits = []
        if s.min_price is not None or s.max_price is not None:
            if s.min_price is None:
                bits.append(f"bis {format_money(s.max_price)}")
            elif s.max_price is None:
                bits.append(f"ab {format_money(s.min_price)}")
            else:
                bits.append(f"{format_money(s.min_price)} – {format_money(s.max_price)}")
        bits.append(" + ".join(SOURCE_LABELS.get(x, x) for x in s.sources))
        if not s.enabled:
            bits.append("pausiert")
        elif row and row["last_run"]:
            bits.append(f"{row['last_matched']} passend · {ago(parse_iso(row['last_run']))}")
        err = ""
        if row and row["last_error"]:
            err = f"<div class=e>{esc(row['last_error'])}</div>"
        elif row and row["source_errors"]:
            err = "<div class=e>" + esc("; ".join(f"{SOURCE_LABELS.get(k, k)}: {v}"
                                                  for k, v in json.loads(row["source_errors"]).items())) + "</div>"
        warn = "".join(f"<div class=m>⚠︎ {esc(w)}</div>" for w in p.warnings)
        cards.append(
            f"<a class=srch href='{esc(_qs(layout='page', s=s.name))}'><div class=h><span class='dot {st}'></span>"
            f"<span>{esc(s.name)}</span></div><code>{esc(p.line)}</code>"
            f"<div class=m>{esc(' · '.join(bits))}</div>{err}{warn}</a>"
        )
    if not cards:
        cards.append("<div class=srch><div class=m>Noch keine Suche. In Nest → Plugins → Tracker → Einstellungen "
                     "anlegen.</div></div>")

    chips = [f"<a class='chip {'on' if not sel else ''}' href='{_qs(layout='page')}'>Alle</a>"]
    chips += [f"<a class='chip {'on' if n == sel else ''}' href='{esc(_qs(layout='page', s=n))}'>{esc(n)}</a>"
              for n in names]
    matches = engine.recent_matches(limit=120, search=sel or None, include_silent=True)
    grid = ("<div class=grid>" + "".join(_card(m, detail=True) for m in matches) + "</div>") if matches else \
        "<div class=srch><div class=m>Noch keine passenden Angebote.</div></div>"

    body = (
        "<div class=wrap>"
        "<div class=top><h1>Tracker</h1><div class=spacer></div>"
        f"<div class=sub>{'Prüfung läuft …' if engine.running else 'Nächste Prüfung ' + until(engine.next_run_at) if engine.next_run_at else ''}</div></div>"
        f"<div class=stats>{stat_html}</div>"
        f"<div class=section><h2>Suchen</h2><div class=searches>{''.join(cards)}</div></div>"
        f"{_sources_section()}"
        f"<div class=section><div class=top><h2>Angebote</h2><div class=spacer></div>"
        f"<div class=chips>{''.join(chips)}</div></div>{grid}</div>"
        "</div>"
    )
    return _page(body, page=True, reload_ms=120_000, base=base)


def _sources_section() -> str:
    """Abrufstatistik je Quelle seit dem Start (Stufe 1 / Browser-Rettung / Sperre)."""
    rows = source_checks()
    if not rows:
        return ""
    cards = "".join(f"<div class=srch><div class=h><span class='dot {r['state']}'></span><span>{esc(r['label'])}</span>"
                    f"</div><div class=m>{esc(r['detail'])}</div></div>" for r in rows)
    return f"<div class=section><h2>Quellen seit dem Start</h2><div class=searches>{cards}</div></div>"


def go(target_url: str | None) -> str:
    """Weiterleitung in neuem Tab: Nest verwirft 3xx auf Plugin-Hosts, also per Meta/JS."""
    if not target_url or not target_url.startswith(("https://www.ebay.", "https://www.kleinanzeigen.de/")):
        return _page("<div class=wrap><div class=empty><div><b>Angebot nicht gefunden</b></div></div></div>")
    u = esc(target_url)
    return (
        "<!doctype html><html lang=de><head><meta charset=utf-8>"
        f"<meta http-equiv=refresh content='0;url={u}'><title>Weiter …</title></head>"
        f"<body><a href='{u}'>Weiter zum Angebot</a>"
        f"<script>location.replace({json.dumps(target_url)})</script></body></html>"
    )
