#!/usr/bin/env python3
"""
Daily news digest builder.

Pulls a set of RSS/Atom feeds (Substacks, X Lists, Economic Times, Moneycontrol,
anything with a feed), optionally enriches headline-only items with the full
article body, then renders a static HTML dashboard + a dated archive copy that
GitHub Pages can serve.

Usage:
    python digest.py              # normal run (reads config.yaml)
    python digest.py --demo       # render from bundled sample data (no network)
"""

import os
import re
import sys
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
UA = "Mozilla/5.0 (compatible; daily-news-digest/1.0; +https://github.com/)"
PLACEHOLDER = re.compile(r"^\s*(PASTE_|https?://PASTE_)", re.I)


# --------------------------------------------------------------------------- #
#  Fetching + parsing
# --------------------------------------------------------------------------- #
def fetch_feed(url, timeout=25):
    """Download a feed with a real UA and hand the bytes to feedparser."""
    resp = requests.get(url, headers={"User-Agent": UA}, timeout=timeout)
    resp.raise_for_status()
    return feedparser.parse(resp.content)


def entry_datetime(entry):
    for key in ("published_parsed", "updated_parsed"):
        parsed = entry.get(key)
        if parsed:
            return dt.datetime(*parsed[:6], tzinfo=dt.timezone.utc)
    return None


def entry_body_html(entry):
    """Best available body: content > summary > description."""
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
    """Follow an article link and extract the readable body (best effort)."""
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
    """Fetch one source and return (report_line, [items])."""
    name, url = src["name"], src.get("url", "")
    if not url or PLACEHOLDER.match(url):
        return (f"  skip  {name}  (no URL set)", [])

    try:
        parsed = fetch_feed(url)
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
        body_html = entry_body_html(entry)
        summary = to_text(body_html, limit=360)
        full = None
        if (src.get("enrich") and settings["enrich_full_text"]
                and enriched < settings["enrich_limit_per_feed"]):
            full = enrich_full_text(entry.get("link", ""))
            if full:
                enriched += 1
        items.append({
            "title": to_text(entry.get("title", "(untitled)")),
            "link": entry.get("link", ""),
            "summary": summary,
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
#  Summaries
# --------------------------------------------------------------------------- #
def heuristic_brief(grouped, per_cat=6):
    """Free brief: the most recent items in each category, no AI needed."""
    brief = []
    for cat, items in grouped:
        top = [{"title": it["title"], "source": it["source"], "link": it["link"]}
               for it in items[:per_cat]]
        if top:
            brief.append((cat, top))
    return brief


def ai_brief(items, settings, api_key):
    """Optional AI brief. Returns markdown-ish text or None on failure."""
    try:
        from anthropic import Anthropic
    except Exception:
        return None
    lines = [f"- [{it['category']}] {it['source']}: {it['title']}"
             for it in items[:150]]
    prompt = (
        "You are a sharp markets & macro analyst writing a private morning "
        "brief for one reader. From the headlines below, write 8–12 tight "
        "bullets grouping the day's most important themes. Be specific with "
        "names and numbers, skip filler, and finish with 2–3 'Watch' items.\n\n"
        + "\n".join(lines)
    )
    try:
        client = Anthropic(api_key=api_key)
        msg = client.messages.create(
            model=settings.get("ai_model", "claude-sonnet-5"),
            max_tokens=1300,
            messages=[{"role": "user", "content": prompt}],
        )
        return "".join(b.text for b in msg.content if b.type == "text").strip()
    except Exception as exc:
        print(f"  (AI summary failed: {exc}; using free brief)")
        return None


# --------------------------------------------------------------------------- #
#  Rendering
# --------------------------------------------------------------------------- #
CSS = """
:root{
  --paper:#F1EEE6; --card:#FBFAF6; --ink:#16223C; --ink-soft:#4B5670;
  --amber:#C0821A; --amber-soft:#E8CE8F; --line:#D9D3C6; --rule:#C7C0B1;
}
*{box-sizing:border-box}
html{-webkit-text-size-adjust:100%}
body{margin:0;background:var(--paper);color:var(--ink);
  font-family:'Newsreader',Georgia,serif;line-height:1.5;
  font-optical-sizing:auto}
a{color:inherit;text-decoration:none}
.wrap{max-width:940px;margin:0 auto;padding:0 20px 80px}

/* ---- masthead: telex-style transmission header ---- */
.mast{border-bottom:2px solid var(--ink);padding:26px 0 10px;margin-bottom:6px}
.mast h1{font-family:'Space Grotesk',sans-serif;font-weight:700;
  letter-spacing:-.02em;font-size:clamp(30px,6vw,54px);margin:0;line-height:.98}
.mast .sub{font-family:'Newsreader',serif;font-style:italic;color:var(--ink-soft);
  font-size:16px;margin-top:4px}
.tape{font-family:'IBM Plex Mono',monospace;font-size:11.5px;letter-spacing:.06em;
  text-transform:uppercase;color:var(--ink-soft);margin-top:14px;
  display:flex;gap:14px;flex-wrap:wrap;align-items:center;
  border-top:1px solid var(--line);padding-top:9px}
.dot{width:8px;height:8px;border-radius:50%;background:var(--amber);
  display:inline-block;box-shadow:0 0 0 3px rgba(192,130,26,.18)}

/* ---- the brief ---- */
.brief{background:var(--card);border:1px solid var(--line);
  border-left:3px solid var(--amber);padding:22px 24px;margin:22px 0 8px}
.brief h2,.sec h2{font-family:'IBM Plex Mono',monospace;font-size:12px;
  letter-spacing:.18em;text-transform:uppercase;color:var(--amber);
  margin:0 0 14px;font-weight:600}
.brief .cat{font-family:'Space Grotesk',sans-serif;font-weight:600;font-size:13px;
  letter-spacing:.02em;margin:14px 0 6px;color:var(--ink)}
.brief ol{margin:0;padding:0;list-style:none;counter-reset:b}
.brief li{counter-increment:b;position:relative;padding:4px 0 4px 34px;
  border-bottom:1px dotted var(--line);font-size:16.5px}
.brief li:last-child{border-bottom:0}
.brief li::before{content:counter(b,decimal-leading-zero);position:absolute;left:0;
  top:6px;font-family:'IBM Plex Mono',monospace;font-size:11px;color:var(--amber)}
.brief li .src{font-family:'IBM Plex Mono',monospace;font-size:10.5px;
  color:var(--ink-soft);text-transform:uppercase;letter-spacing:.04em}
.ai{font-size:16.5px;white-space:pre-wrap}

/* ---- sections + items ---- */
.sec{margin-top:40px}
.sec>h2{border-bottom:1px solid var(--rule);padding-bottom:8px;
  display:flex;justify-content:space-between;align-items:baseline}
.sec>h2 .n{color:var(--ink-soft)}
.item{padding:16px 0;border-bottom:1px solid var(--line)}
.item:last-child{border-bottom:0}
.item .meta{font-family:'IBM Plex Mono',monospace;font-size:10.5px;
  letter-spacing:.05em;text-transform:uppercase;color:var(--ink-soft);
  display:flex;gap:10px;margin-bottom:5px}
.item .meta .call{color:var(--amber)}
.item h3{font-family:'Space Grotesk',sans-serif;font-weight:600;
  font-size:19px;line-height:1.24;margin:0 0 5px;letter-spacing:-.01em}
.item h3 a{background-image:linear-gradient(var(--amber),var(--amber));
  background-size:0% 1.5px;background-repeat:no-repeat;background-position:0 100%;
  transition:background-size .25s ease;padding-bottom:1px}
.item h3 a:hover{background-size:100% 1.5px}
.item p{margin:0;color:#33405c;font-size:16px}
.item p.long{color:var(--ink)}

footer{margin-top:56px;border-top:2px solid var(--ink);padding-top:12px;
  font-family:'IBM Plex Mono',monospace;font-size:11px;letter-spacing:.05em;
  text-transform:uppercase;color:var(--ink-soft);
  display:flex;justify-content:space-between;flex-wrap:wrap;gap:10px}

@media (max-width:560px){
  .item h3{font-size:17px}.brief li{font-size:15.5px}
}
@media (prefers-reduced-motion:reduce){*{transition:none!important}}
body{animation:fade .5s ease both}
@keyframes fade{from{opacity:0;transform:translateY(4px)}to{opacity:1;transform:none}}
"""

HEAD = """<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{title} · {date}</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Space+Grotesk:wght@500;600;700&family=Newsreader:ital,opsz,wght@0,6..72,400;0,6..72,500;1,6..72,400&family=IBM+Plex+Mono:wght@400;500;600&display=swap" rel="stylesheet">
<style>{css}</style></head><body><div class="wrap">"""


def esc(s):
    return html.escape(s or "", quote=True)


def call_sign(source):
    """A short monospace 'call sign' from a source name."""
    return esc(source.split("·")[-1].strip()[:22] if "·" in source
               else source[:22])


def render(date_disp, tape_meta, brief, ai_text, grouped, settings):
    out = [HEAD.format(title=esc(settings["title"]), date=esc(date_disp), css=CSS)]

    out.append('<header class="mast">')
    out.append(f'<h1>{esc(settings["title"])}</h1>')
    out.append(f'<div class="sub">{esc(settings["subtitle"])}</div>')
    out.append('<div class="tape"><span class="dot"></span>'
               + " ".join(f"<span>{esc(x)}</span>" for x in tape_meta)
               + '</div></header>')

    # The Brief
    out.append('<section class="brief"><h2>The Brief</h2>')
    if ai_text:
        out.append(f'<div class="ai">{esc(ai_text)}</div>')
    else:
        for cat, tops in brief:
            out.append(f'<div class="cat">{esc(cat)}</div><ol>')
            for t in tops:
                link = esc(t["link"])
                a = f'<a href="{link}">{esc(t["title"])}</a>' if link else esc(t["title"])
                out.append(f'<li><span class="src">{call_sign(t["source"])}</span> — {a}</li>')
            out.append('</ol>')
    out.append('</section>')

    # Full sections
    for cat, items in grouped:
        out.append(f'<section class="sec"><h2>{esc(cat)}'
                   f'<span class="n">{len(items):02d}</span></h2>')
        tz = ZoneInfo(settings["timezone"]) if ZoneInfo else dt.timezone.utc
        for it in items:
            when = it["when"].astimezone(tz).strftime("%H:%M") if it["when"] else "—"
            out.append('<div class="item"><div class="meta">'
                       f'<span class="call">{call_sign(it["source"])}</span>'
                       f'<span>{when}</span></div>')
            link = esc(it["link"])
            title = esc(it["title"])
            out.append(f'<h3><a href="{link}">{title}</a></h3>' if link
                       else f'<h3>{title}</h3>')
            body = it["full"] or it["summary"]
            cls = "long" if it["full"] else ""
            if body:
                out.append(f'<p class="{cls}">{esc(to_text(body, 500))}</p>')
            out.append('</div>')
        out.append('</section>')

    out.append('<footer><span>Generated ' + esc(date_disp)
               + '</span><span><a href="archive.html">◂ Archive</a></span></footer>')
    out.append('</div></body></html>')
    return "\n".join(out)


def rebuild_archive_index(settings):
    DOCS.mkdir(parents=True, exist_ok=True)
    ARCHIVE.mkdir(parents=True, exist_ok=True)
    files = sorted(ARCHIVE.glob("*.html"), reverse=True)
    rows = "\n".join(
        f'<div class="item"><h3><a href="archive/{f.name}">{f.stem}</a></h3></div>'
        for f in files) or '<p>No archived editions yet.</p>'
    page = (HEAD.format(title=esc(settings["title"]), date="Archive", css=CSS)
            + '<header class="mast"><h1>Archive</h1>'
              '<div class="sub">Past editions</div></header>'
            + f'<section class="sec"><h2>Editions'
              f'<span class="n">{len(files):02d}</span></h2>{rows}</section>'
            + '<footer><span><a href="index.html">◂ Latest</a></span></footer>'
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
        print("  generating AI brief…")
        ai_text = ai_brief(items, settings, key)
    brief = heuristic_brief(grouped)

    tz = ZoneInfo(settings["timezone"]) if ZoneInfo else dt.timezone.utc
    now = dt.datetime.now(tz)
    date_disp = now.strftime("%A, %d %B %Y · %H:%M %Z")
    tape = [now.strftime("Transmission %Y-%m-%d"),
            f"{len(items)} items",
            f"{len({i['source'] for i in items})} sources",
            "AI brief" if ai_text else "auto brief"]

    page = render(date_disp, tape, brief, ai_text, grouped, settings)
    DOCS.mkdir(parents=True, exist_ok=True)
    ARCHIVE.mkdir(parents=True, exist_ok=True)
    (DOCS / "index.html").write_text(page, encoding="utf-8")
    (ARCHIVE / f"{now.strftime('%Y-%m-%d')}.html").write_text(page, encoding="utf-8")
    rebuild_archive_index(settings)
    print(f"\n  wrote docs/index.html  ({len(items)} items, "
          f"{len(grouped)} sections)")


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
    """Render from bundled sample data so the design can be previewed offline."""
    import sample_data
    cfg = yaml.safe_load(open(ROOT / "config.yaml", encoding="utf-8"))
    build(sample_data.ITEMS, cfg["settings"])


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--demo", action="store_true", help="render from sample data")
    ap.add_argument("--config", default=str(ROOT / "config.yaml"))
    args = ap.parse_args()
    run_demo() if args.demo else run(args.config)
