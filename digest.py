#!/usr/bin/env python3
"""
Daily news digest builder.

Pulls a set of RSS/Atom feeds (Substacks, X Lists, Economic Times, Moneycontrol,
anything with a feed), optionally enriches headline-only items with the full
article body, condenses everything with a built-in (key-free) summariser, then
renders a static HTML dashboard + a dated archive copy for GitHub Pages.

Usage:
    python digest.py              # normal run (reads config.yaml)
    python digest.py --demo       # render from bundled sample data (no network)
"""

import os
import re
import html
import hashlib
import argparse
import pathlib
import datetime as dt
import concurrent.futures as cf

import yaml
import feedparser
import requests
from bs4 import BeautifulSoup

import summarize                      # the built-in, no-key summariser

try:
    from zoneinfo import ZoneInfo
except Exception:                                       # pragma: no cover
    ZoneInfo = None

try:
    import trafilatura
    HAVE_TRAFILATURA = True
except Exception:
    HAVE_TRAFILATURA = False

ROOT = pathlib.Path(__file__).resolve().parent
DOCS = ROOT / "docs"
ARCHIVE = DOCS / "archive"
BROWSER_HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"),
    "Accept": "application/rss+xml, application/atom+xml, application/xml;q=0.9, */*;q=0.8",
}
# Free relay used only as a fallback when a site (e.g. Substack) blocks GitHub's IP.
PROXY = "https://api.allorigins.win/raw?url="
PLACEHOLDER = re.compile(r"^\s*(PASTE_|https?://PASTE_)", re.I)


# --------------------------------------------------------------------------- #
#  Fetching + parsing
# --------------------------------------------------------------------------- #
def _http_get(u, timeout):
    resp = requests.get(u, headers=BROWSER_HEADERS, timeout=timeout)
    resp.raise_for_status()
    return resp


def fetch_feed(url, timeout=25, allow_proxy=False):
    """Fetch a feed. If the site blocks us (e.g. Substack blocks GitHub IPs) and
    allow_proxy is on, retry once through a free relay so the request no longer
    comes straight from the blocked datacenter IP."""
    try:
        return feedparser.parse(_http_get(url, timeout).content)
    except Exception:
        if not allow_proxy:
            raise
        proxied = PROXY + requests.utils.quote(url, safe="")
        return feedparser.parse(_http_get(proxied, timeout).content)


def entry_datetime(entry):
    for key in ("published_parsed", "updated_parsed"):
        parsed = entry.get(key)
        if parsed:
            return dt.datetime(*parsed[:6], tzinfo=dt.timezone.utc)
    return None


def entry_body_html(entry):
    if entry.get("content"):
        return entry["content"][0].get("value", "") or ""
    return entry.get("summary", "") or entry.get("description", "") or ""


def to_text(raw_html, limit=None):
    text = BeautifulSoup(raw_html or "", "lxml").get_text(" ", strip=True)
    text = re.sub(r"\s+", " ", text).strip()
    if limit and len(text) > limit:
        text = text[:limit].rsplit(" ", 1)[0] + "…"
    return text


def enrich_full_text(url, timeout=20):
    if not HAVE_TRAFILATURA:
        return None
    try:
        downloaded = trafilatura.fetch_url(url)
        if not downloaded:
            return None
        return trafilatura.extract(downloaded, include_comments=False,
                                   include_tables=False, favor_precision=True)
    except Exception:
        return None


def collect_source(src, settings):
    name, url = src["name"], src.get("url", "")
    if not url or PLACEHOLDER.match(url):
        return (f"  skip  {name}  (no URL set)", [])

    allow_proxy = bool(src.get("proxy")) or "substack.com" in url
    try:
        parsed = fetch_feed(url, allow_proxy=allow_proxy)
    except Exception as exc:
        return (f"  FAIL  {name}  ({type(exc).__name__})", [])

    if parsed.bozo and not parsed.entries:
        return (f"  FAIL  {name}  (not a valid feed)", [])

    cutoff = dt.datetime.now(dt.timezone.utc) - dt.timedelta(
        hours=settings["lookback_hours"])
    items, enriched = [], 0
    for entry in parsed.entries[: settings["max_items_per_feed"]]:
        when = entry_datetime(entry)
        if when and when < cutoff:
            continue
        raw = to_text(entry_body_html(entry))
        full = None
        if (src.get("enrich") and settings["enrich_full_text"]
                and enriched < settings["enrich_limit_per_feed"]):
            full = enrich_full_text(entry.get("link", ""))
            if full:
                enriched += 1
        items.append({
            "title": to_text(entry.get("title", "(untitled)")),
            "link": entry.get("link", ""),
            "summary": raw,
            "full": full,
            "when": when,
            "source": name,
            "category": src.get("category", "Other"),
        })
    return (f"  ok    {name}  ({len(items)} items)", items)


# --------------------------------------------------------------------------- #
#  Dedupe + grouping
# --------------------------------------------------------------------------- #
def dedupe(items):
    seen, out = set(), []
    for it in items:
        key = (it["link"] or "").split("?")[0] or it["title"].lower()
        h = hashlib.md5(key.encode("utf-8")).hexdigest()
        if h in seen:
            continue
        seen.add(h)
        out.append(it)
    return out


def group_by_category(items, order):
    groups = {}
    for it in items:
        groups.setdefault(it["category"], []).append(it)
    for cat in groups:
        groups[cat].sort(key=lambda x: x["when"] or dt.datetime.min.replace(
            tzinfo=dt.timezone.utc), reverse=True)
    ordered = [(c, groups[c]) for c in order if c in groups]
    ordered += [(c, v) for c, v in groups.items() if c not in order]
    return ordered


# --------------------------------------------------------------------------- #
#  Optional AI brief (OFF by default; the built-in summariser needs no key)
# --------------------------------------------------------------------------- #
def ai_brief(items, settings, api_key):
    try:
        from anthropic import Anthropic
    except Exception:
        print("  (anthropic package not installed; using built-in brief)")
        return None
    lines = [f"- [{it['category']}] {it['source']}: {it['title']}"
             for it in items[:150]]
    prompt = ("You are a markets & macro analyst writing a private morning brief. "
              "From the headlines below, write 8-12 tight bullets grouping the "
              "day's key themes, then 2-3 'Watch' items.\n\n" + "\n".join(lines))
    try:
        client = Anthropic(api_key=api_key)
        msg = client.messages.create(
            model=settings.get("ai_model", "claude-sonnet-5"),
            max_tokens=1300, messages=[{"role": "user", "content": prompt}])
        return "".join(b.text for b in msg.content if b.type == "text").strip()
    except Exception as exc:
        print(f"  (AI brief failed: {exc}; using built-in brief)")
        return None


# --------------------------------------------------------------------------- #
#  Rendering
# --------------------------------------------------------------------------- #
CSS = """
:root{--green:#2F6F55;--green-deep:#1E4A38;--green-soft:#DCEBE0;
  --orange:#E0722B;--orange-deep:#9A4A17;--orange-soft:#FBE3CF;
  --cream:#FAF4E3;--card:#FFFCF3;--ink:#2A2620;--muted:#716B5D;--line:#E4D9BE}
*{box-sizing:border-box}
html{-webkit-text-size-adjust:100%;scroll-behavior:smooth}
body{margin:0;background:var(--cream);color:var(--ink);
  font-family:Inter,"Segoe UI",system-ui,-apple-system,Roboto,sans-serif;
  font-size:16px;line-height:1.6;-webkit-font-smoothing:antialiased}
a{color:inherit;text-decoration:none}
.wrap{max-width:860px;margin:0 auto;padding:0 16px 72px}
.hero{padding:44px 0 22px}
.cover-icon{font-size:56px;line-height:1}
.title{font-size:clamp(30px,6vw,44px);font-weight:750;letter-spacing:-.025em;margin:12px 0 2px;line-height:1.1}
.sub{color:var(--muted);font-size:16px}
.meta-row{display:flex;flex-wrap:wrap;gap:8px;margin-top:16px}
.pill{font-size:13px;font-weight:550;border-radius:999px;padding:3px 12px;
  background:var(--card);border:1px solid var(--line)}
.pill.g{background:var(--green-soft);border-color:transparent;color:var(--green-deep)}
.pill.o{background:var(--orange-soft);border-color:transparent;color:var(--orange-deep)}
nav.tabs{position:sticky;top:0;z-index:10;background:var(--cream);
  border-top:1px solid var(--line);border-bottom:1px solid var(--line);
  margin:0 -16px;padding:0 12px;display:flex;overflow-x:auto;white-space:nowrap;
  scrollbar-width:none;-webkit-overflow-scrolling:touch}
nav.tabs::-webkit-scrollbar{display:none}
nav.tabs a{display:inline-flex;align-items:center;gap:6px;padding:13px 11px 11px;
  font-size:14.5px;font-weight:550;color:var(--muted);
  border-bottom:2.5px solid transparent;margin-bottom:-1px;transition:color .15s,border-color .15s}
nav.tabs a:hover{color:var(--ink)}
nav.tabs a.on{color:var(--green-deep);border-bottom-color:var(--orange)}
nav.tabs .n{font-size:11.5px;font-weight:600;background:var(--orange-soft);
  color:var(--orange-deep);border-radius:999px;padding:0 7px;line-height:18px}
.pane{padding-top:26px}
.js .pane{display:none}
.js .pane.on{display:block}
.callout{display:flex;gap:14px;background:var(--green-soft);border-radius:14px;
  padding:18px 20px;margin-bottom:30px}
.callout .ico{font-size:22px;line-height:1.3}
.callout .body{flex:1;min-width:0}
.callout h2{font-size:17px;font-weight:700;color:var(--green-deep);margin:2px 0 8px;letter-spacing:-.01em}
.ai{white-space:pre-wrap;font-size:15.5px}
ol.brief{margin:0;padding:0;list-style:none;counter-reset:b}
ol.brief li{counter-increment:b;position:relative;padding:9px 0 9px 30px;
  border-top:1px solid rgba(30,74,56,.14)}
ol.brief li:first-child{border-top:0}
ol.brief li::before{content:counter(b);position:absolute;left:0;top:10px;width:20px;height:20px;
  border-radius:6px;background:var(--green);color:#fff;font-size:11.5px;font-weight:700;
  display:flex;align-items:center;justify-content:center}
ol.brief .hd{font-weight:600;font-size:15.5px;line-height:1.35}
ol.brief .hd a:hover{text-decoration:underline;text-underline-offset:3px}
ol.brief .src{display:inline-block;font-size:11.5px;font-weight:600;color:var(--orange-deep);
  background:var(--orange-soft);border-radius:5px;padding:0 6px;margin-right:6px;vertical-align:1px}
ol.brief .gist{font-size:14.5px;color:var(--muted);margin-top:2px}
ol.brief .rel{font-size:12px;color:var(--green-deep);font-weight:600;margin-left:6px}
h2.block{font-size:20px;font-weight:700;letter-spacing:-.015em;margin:0 0 14px;
  display:flex;align-items:center;gap:10px}
h2.block .n{font-size:12px;font-weight:600;color:var(--orange-deep);background:var(--orange-soft);
  border-radius:999px;padding:1px 9px}
.grid{display:grid;grid-template-columns:1fr;gap:14px}
@media (min-width:640px){.grid{grid-template-columns:1fr 1fr}}
.scard{display:block;background:var(--card);border:1px solid var(--line);border-radius:14px;
  padding:16px 18px;transition:border-color .15s,transform .15s}
.scard:hover{border-color:var(--green);transform:translateY(-1px)}
.sh{display:flex;justify-content:space-between;align-items:center;font-weight:650;
  font-size:15.5px;margin-bottom:8px}
.sh .n{font-size:12px;font-weight:600;color:var(--orange-deep);background:var(--orange-soft);
  border-radius:999px;padding:0 8px;line-height:20px}
.scard ul{list-style:none;margin:0;padding:0}
.scard li{font-size:14.5px;line-height:1.4;padding:6px 0;border-top:1px solid var(--line)}
.scard li .m{display:block;font-size:12px;color:var(--muted);margin-top:1px}
.row{padding:18px 0}
.row+.row{border-top:1px solid var(--line)}
.row .meta{display:flex;gap:6px;margin-bottom:6px;flex-wrap:wrap}
.chip{font-size:12px;font-weight:600;border-radius:5px;padding:1px 7px;line-height:20px}
.chip.o{background:var(--orange-soft);color:var(--orange-deep)}
.chip.g{background:var(--green-soft);color:var(--green-deep)}
.row h3{font-size:18px;font-weight:650;line-height:1.35;margin:0 0 4px;letter-spacing:-.01em}
.row h3 a{background-image:linear-gradient(var(--orange),var(--orange));background-size:0 2px;
  background-repeat:no-repeat;background-position:0 100%;transition:background-size .25s ease}
.row h3 a:hover{background-size:100% 2px}
.row p{margin:0;color:var(--muted);font-size:15px}
footer{margin-top:48px;padding-top:14px;border-top:1px solid var(--line);display:flex;
  justify-content:space-between;flex-wrap:wrap;gap:10px;font-size:13px;color:var(--muted)}
footer a{color:var(--green-deep);font-weight:600}
@media (max-width:560px){.row h3{font-size:16.5px}.callout{padding:16px}.grid{gap:12px}}
@media (prefers-reduced-motion:reduce){*{transition:none!important;scroll-behavior:auto!important}}
"""

HEAD = """<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{title} · {date}</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;650;700;750&display=swap" rel="stylesheet">
<style>{css}</style>
<script>document.documentElement.classList.add('js')</script>
</head><body><div class="wrap">"""

TABS_JS = """<script>
(function(){
  var links=[].slice.call(document.querySelectorAll('nav.tabs a'));
  var panes=[].slice.call(document.querySelectorAll('.pane'));
  function show(id){
    var target=document.getElementById(id)?id:'overview';
    panes.forEach(function(p){p.classList.toggle('on',p.id===target)});
    links.forEach(function(a){a.classList.toggle('on',a.getAttribute('href')==='#'+target)});
  }
  window.addEventListener('hashchange',function(){show(location.hash.slice(1))});
  show(location.hash.slice(1)||'overview');
})();
</script>
"""

ICONS = [("macro", "🌍"), ("capital", "📈"), ("market", "📈"), ("india", "📈"),
         ("energy", "⚡"), ("tech", "💻"), ("substack", "✍️")]


def esc(s):
    return html.escape(s or "", quote=True)


def call_sign(source):
    return esc(source.split("·")[-1].strip()[:22] if "·" in source else source[:22])


def section_icon(cat):
    c = cat.lower()
    for key, icon in ICONS:
        if key in c:
            return icon
    return "📰"


def slug(cat):
    s = re.sub(r"[^a-z0-9]+", "-", cat.lower()).strip("-") or "section"
    return "s-" + s if s == "overview" else s


def render(date_disp, tape_meta, themes, ai_text, grouped, settings):
    tz = ZoneInfo(settings["timezone"]) if ZoneInfo else dt.timezone.utc
    sents = settings.get("summary_sentences", 2)
    sections = [(slug(cat), cat, items) for cat, items in grouped]

    def when_of(it):
        return it["when"].astimezone(tz).strftime("%H:%M") if it["when"] else "—"

    out = [HEAD.format(title=esc(settings["title"]), date=esc(date_disp), css=CSS)]
    out.append('<header class="hero"><div class="cover-icon">📰</div>')
    out.append(f'<h1 class="title">{esc(settings["title"])}</h1>')
    out.append(f'<div class="sub">{esc(settings["subtitle"])}</div>')
    pills = "".join(
        f'<span class="pill{" g" if i == 0 else (" o" if i == 1 else "")}">{esc(x)}</span>'
        for i, x in enumerate(tape_meta))
    out.append(f'<div class="meta-row">{pills}</div></header>')

    out.append('<nav class="tabs" aria-label="Sections"><a href="#overview">🏠 Overview</a>')
    for sid, cat, items in sections:
        out.append(f'<a href="#{sid}">{section_icon(cat)} {esc(cat)} '
                   f'<span class="n">{len(items)}</span></a>')
    out.append('</nav>')

    # Overview pane: brief + section cards
    out.append('<section class="pane" id="overview">')
    out.append('<div class="callout"><div class="ico">🧭</div><div class="body">'
               '<h2>The Brief</h2>')
    if ai_text:
        out.append(f'<div class="ai">{esc(ai_text)}</div>')
    else:
        out.append('<ol class="brief">')
        for t in themes:
            link = esc(t["link"])
            hd = esc(t["headline"])
            head = f'<a href="{link}">{hd}</a>' if link else hd
            rel = f'<span class="rel">+{t["related"]} related</span>' if t["related"] else ""
            gist = (f'<div class="gist">{esc(t["gist"])}{rel}</div>' if t["gist"]
                    else (f'<div class="gist">{rel}</div>' if rel else ""))
            out.append(f'<li><div class="hd"><span class="src">{call_sign(t["source"])}</span>'
                       f' {head}</div>{gist}</li>')
        out.append('</ol>')
    out.append('</div></div>')

    out.append('<h2 class="block">Sections</h2><div class="grid">')
    for sid, cat, items in sections:
        out.append(f'<a class="scard" href="#{sid}"><div class="sh">'
                   f'<span>{section_icon(cat)} {esc(cat)}</span>'
                   f'<span class="n">{len(items)}</span></div><ul>')
        for it in items[:3]:
            out.append(f'<li>{esc(it["title"])}<span class="m">'
                       f'{call_sign(it["source"])} · {when_of(it)}</span></li>')
        out.append('</ul></a>')
    out.append('</div></section>')

    # One pane per section
    for sid, cat, items in sections:
        out.append(f'<section class="pane" id="{sid}">'
                   f'<h2 class="block">{section_icon(cat)} {esc(cat)} '
                   f'<span class="n">{len(items)}</span></h2>')
        for it in items:
            link = esc(it["link"])
            title = esc(it["title"])
            gist = summarize.summarize_text(it.get("full") or it.get("summary") or "",
                                            max_sentences=sents)
            out.append('<article class="row"><div class="meta">'
                       f'<span class="chip o">{call_sign(it["source"])}</span>'
                       f'<span class="chip g">{when_of(it)}</span></div>')
            out.append(f'<h3><a href="{link}">{title}</a></h3>' if link else f'<h3>{title}</h3>')
            if gist:
                out.append(f'<p>{esc(gist)}</p>')
            out.append('</article>')
        out.append('</section>')

    out.append('<footer><span>Generated ' + esc(date_disp) + '</span>'
               '<a href="archive.html">◂ Archive</a></footer>')
    out.append(TABS_JS)
    out.append('</div></body></html>')
    return "\n".join(out)


def rebuild_archive_index(settings):
    DOCS.mkdir(parents=True, exist_ok=True)
    ARCHIVE.mkdir(parents=True, exist_ok=True)
    files = sorted(ARCHIVE.glob("*.html"), reverse=True)
    rows = "\n".join(
        f'<article class="row"><h3><a href="archive/{f.name}">{f.stem}</a></h3></article>'
        for f in files) or '<p>No archived editions yet.</p>'
    page = (HEAD.format(title=esc(settings["title"]), date="Archive", css=CSS)
            + '<header class="hero"><div class="cover-icon">🗂️</div>'
              '<h1 class="title">Archive</h1><div class="sub">Past editions</div></header>'
            + f'<h2 class="block">Editions <span class="n">{len(files):02d}</span></h2>{rows}'
            + '<footer><span></span><a href="index.html">◂ Latest</a></footer>'
              '</div></body></html>')
    (DOCS / "archive.html").write_text(page, encoding="utf-8")



# --------------------------------------------------------------------------- #
#  Orchestration
# --------------------------------------------------------------------------- #
def build(items, settings):
    items = dedupe(items)
    grouped = group_by_category(items, settings["category_order"])

    ai_text = None
    key = os.environ.get("ANTHROPIC_API_KEY")
    if settings.get("use_ai_summary") and key:
        print("  optional AI brief enabled…")
        ai_text = ai_brief(items, settings, key)

    themes = summarize.build_brief(
        items,
        max_themes=settings.get("brief_size", 10),
        summary_sentences=settings.get("summary_sentences", 2),
    )

    tz = ZoneInfo(settings["timezone"]) if ZoneInfo else dt.timezone.utc
    now = dt.datetime.now(tz)
    date_disp = now.strftime("%A, %d %B %Y · %H:%M %Z")
    tape = [now.strftime("Transmission %Y-%m-%d"),
            f"{len(items)} items",
            f"{len({i['source'] for i in items})} sources",
            "AI brief" if ai_text else "self-summarised"]

    page = render(date_disp, tape, themes, ai_text, grouped, settings)
    DOCS.mkdir(parents=True, exist_ok=True)
    ARCHIVE.mkdir(parents=True, exist_ok=True)
    (DOCS / ".nojekyll").touch()   # tell GitHub Pages: serve HTML as-is, skip Jekyll
    (DOCS / "index.html").write_text(page, encoding="utf-8")
    (ARCHIVE / f"{now.strftime('%Y-%m-%d')}.html").write_text(page, encoding="utf-8")
    rebuild_archive_index(settings)
    print(f"\n  wrote docs/index.html  ({len(items)} items, "
          f"{len(themes)} brief themes, {len(grouped)} sections)")


def run(config_path):
    cfg = yaml.safe_load(open(config_path, encoding="utf-8"))
    settings, sources = cfg["settings"], cfg["sources"]
    print(f"Fetching {len(sources)} sources…")
    all_items = []
    with cf.ThreadPoolExecutor(max_workers=settings["fetch_workers"]) as ex:
        for line, items in ex.map(lambda s: collect_source(s, settings), sources):
            print(line)
            all_items.extend(items)
    build(all_items, settings)


def run_demo():
    import sample_data
    cfg = yaml.safe_load(open(ROOT / "config.yaml", encoding="utf-8"))
    build(sample_data.ITEMS, cfg["settings"])


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--demo", action="store_true", help="render from sample data")
    ap.add_argument("--config", default=str(ROOT / "config.yaml"))
    args = ap.parse_args()
    run_demo() if args.demo else run(args.config)
