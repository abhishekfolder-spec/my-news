#!/usr/bin/env python3
"""
Portfolio news tracker — the "Portfolio" tab of the Daily Wire.

Each run:
  1. Exchange filings for every stock in portfolio.yaml
       NSE corporate announcements (falls back to NSE's announcements RSS
       if NSE blocks GitHub's servers) + BSE announcements.
  2. Classifies each filing with portfolio_rules.yaml:
       critical/high → 🔴   schedule_only/medium → 🟡   drop → hidden   else → ⚪
  3. Merges the same filing seen on both exchanges.
  4. Google News RSS per stock; keeps only headlines that hit a trigger.
  5. Returns a dict that digest.py renders as the Portfolio tab, and writes
     docs/data/portfolio.json (input for the later Claude review layer).

Standalone use:
    python portfolio.py                       # today's run, prints a summary
    python portfolio.py --days 30 --out docs/portfolio-backtest.html
                                              # backtest page for tuning rules
"""

import re
import sys
import json
import time
import html
import argparse
import pathlib
import datetime as dt
import concurrent.futures as cf
from urllib.parse import quote_plus

import yaml
import requests
import feedparser

ROOT = pathlib.Path(__file__).resolve().parent
DATA = ROOT / "docs" / "data"
CODES_CACHE = DATA / "portfolio_codes.json"
STATE_FILE = DATA / "portfolio_state.json"
IST = dt.timezone(dt.timedelta(hours=5, minutes=30))

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36")
RELAY = "https://api.allorigins.win/raw?url="     # same free relay digest.py uses

NSE_HOME = "https://www.nseindia.com"
NSE_FILINGS_PAGE = NSE_HOME + "/companies-listing/corporate-filings-announcements"
NSE_API = NSE_HOME + "/api/corporate-announcements"
NSE_RSS = "https://nsearchives.nseindia.com/content/RSS/Online_announcements.xml"
NSE_MASTERS = [
    "https://nsearchives.nseindia.com/content/equities/EQUITY_L.csv",
    "https://nsearchives.nseindia.com/emerge/corporates/content/SME_EQUITY_L.csv",
]
BSE_API = "https://api.bseindia.com/BseIndiaAPI/api"
BSE_HEADERS = {"User-Agent": UA, "Accept": "application/json, text/plain, */*",
               "Referer": "https://www.bseindia.com/", "Origin": "https://www.bseindia.com"}
BSE_PDF = "https://www.bseindia.com/xml-data/corpfiling/AttachLive/"
BSE_PDF_HIST = "https://www.bseindia.com/xml-data/corpfiling/AttachHis/"
GNEWS = "https://news.google.com/rss/search?q={q}&hl=en-IN&gl=IN&ceid=IN:en"

STOP = set("the of and for in on to a an under regulation reg sebi lodr ltd limited "
           "company announcement disclosure intimation pursuant with by is at as".split())


# --------------------------------------------------------------------------- #
#  Config + helpers
# --------------------------------------------------------------------------- #
def load_config():
    stocks = yaml.safe_load(open(ROOT / "portfolio.yaml", encoding="utf-8"))["holdings"]
    rules = yaml.safe_load(open(ROOT / "portfolio_rules.yaml", encoding="utf-8"))
    comp = {}
    for sec in ("critical", "drop", "schedule_only", "high", "medium"):
        comp[sec] = [re.compile(r, re.I) for r in rules["filings"].get(sec, []) or []]
    news = rules["news"]
    comp["news_trig"] = [re.compile(r, re.I) for r in news.get("triggers", [])]
    comp["news_drop"] = [re.compile(r, re.I) for r in news.get("drop", [])]
    comp["trusted"] = [re.compile(r, re.I) for r in news.get("trusted_sources", [])]
    return stocks, rules, comp


def now_ist():
    return dt.datetime.now(IST)


def tokens(text):
    return {w for w in re.findall(r"[a-z0-9]+", (text or "").lower())
            if len(w) > 2 and w not in STOP}


def jaccard(a, b):
    a, b = tokens(a), tokens(b)
    return len(a & b) / len(a | b) if a and b else 0.0


def first_match(patterns, text):
    for p in patterns:
        if p.search(text):
            return p.pattern
    return None


def classify(text, comp):
    """→ (tier, rule).  tier ∈ high | medium | drop | other"""
    for sec, tier in (("critical", "high"), ("drop", "drop"), ("schedule_only", "medium"),
                      ("high", "high"), ("medium", "medium")):
        hit = first_match(comp[sec], text)
        if hit:
            return tier, hit
    return "other", None


def clean(s, limit=None):
    s = re.sub(r"\s+", " ", html.unescape(s or "")).strip()
    if limit and len(s) > limit:
        s = s[:limit].rsplit(" ", 1)[0] + "…"
    return s


def norm_name(s):
    s = (s or "").lower().replace("&", " and ")
    s = re.sub(r"\b(ltd|limited|the|india|co|corporation|corp)\b\.?", " ", s)
    return re.sub(r"[^a-z0-9]+", " ", s).strip()


# --------------------------------------------------------------------------- #
#  Code mapping: NSE symbol → ISIN → BSE scrip code (cached, checked by you)
# --------------------------------------------------------------------------- #
def _get(url, headers=None, timeout=30, relay=False):
    h = {"User-Agent": UA}
    h.update(headers or {})
    try:
        r = requests.get(url, headers=h, timeout=timeout)
        r.raise_for_status()
        return r
    except Exception:
        if not relay:
            raise
        r = requests.get(RELAY + quote_plus(url), headers={"User-Agent": UA}, timeout=timeout)
        r.raise_for_status()
        return r


def nse_master():
    """{SYMBOL: (name, isin)} from NSE's equity + SME lists."""
    out = {}
    for url in NSE_MASTERS:
        try:
            text = _get(url, relay=True).text
        except Exception as exc:
            print(f"  codes: NSE list unavailable ({url.rsplit('/', 1)[-1]}: {type(exc).__name__})")
            continue
        lines = [l for l in text.splitlines() if l.strip()]
        head = [h.strip().upper().replace(" ", "_") for h in lines[0].split(",")]
        try:
            i_sym = head.index("SYMBOL")
            i_name = next(i for i, h in enumerate(head) if "NAME" in h)
            i_isin = next(i for i, h in enumerate(head) if "ISIN" in h)
        except (ValueError, StopIteration):
            continue
        for l in lines[1:]:
            c = [x.strip().strip('"') for x in l.split(",")]
            if len(c) > max(i_sym, i_name, i_isin):
                out[c[i_sym].upper()] = (c[i_name], c[i_isin])
    return out


def bse_master():
    """List of BSE active equity scrips (dicts with lower-cased keys)."""
    url = (BSE_API + "/ListofScripData/w?Group=&Scripcode=&industry=&segment=Equity&status=Active")
    try:
        rows = _get(url, headers=BSE_HEADERS, timeout=60).json()
    except Exception as exc:
        print(f"  codes: BSE list unavailable ({type(exc).__name__})")
        return []
    if isinstance(rows, dict):
        rows = rows.get("Table") or next((v for v in rows.values() if isinstance(v, list)), [])
    return [{k.lower(): v for k, v in r.items()} for r in rows]


def resolve_codes(stocks):
    """Returns {NSE: {...}} and the list of rows for the mapping table."""
    cache = {}
    if CODES_CACHE.exists():
        cache = json.loads(CODES_CACHE.read_text(encoding="utf-8"))
    todo = [s for s in stocks
            if s["nse"] not in cache or cache[s["nse"]].get("status") != "ok"]
    if todo:
        print(f"  codes: resolving {len(todo)} stock(s)…")
        nse = nse_master()
        bse = bse_master()
        by_isin = {str(r.get("isin_number", "")).strip(): r for r in bse if r.get("isin_number")}
        by_id = {str(r.get("scrip_id", "")).strip().upper(): r for r in bse if r.get("scrip_id")}
        by_name = {norm_name(r.get("scrip_name") or r.get("issuer_name")): r for r in bse}
        for s in todo:
            sym = s["nse"].upper()
            rec = {"nse_name": None, "isin": None, "bse": s.get("bse"), "bse_name": None,
                   "matched_by": "portfolio.yaml" if s.get("bse") else None}
            if sym in nse:
                rec["nse_name"], rec["isin"] = nse[sym]
            if not rec["bse"]:
                hit, how = None, None
                if rec["isin"] and rec["isin"] in by_isin:
                    hit, how = by_isin[rec["isin"]], "ISIN"
                elif sym in by_id:
                    hit, how = by_id[sym], "symbol"
                elif norm_name(s["name"]) in by_name:
                    hit, how = by_name[norm_name(s["name"])], "name"
                if hit:
                    rec["bse"] = str(hit.get("scrip_cd") or hit.get("scripcode") or "").strip() or None
                    rec["bse_name"] = hit.get("scrip_name") or hit.get("issuer_name")
                    rec["matched_by"] = how
            if not nse and not bse:
                rec["status"] = "retry"            # both lists down: try next run
            elif not rec["nse_name"] and nse:
                rec["status"] = "check NSE symbol"
            elif not rec["bse"]:
                rec["status"] = "NSE only" if s.get("sme") else "no BSE match"
            else:
                rec["status"] = "ok"
            cache[sym] = rec
        DATA.mkdir(parents=True, exist_ok=True)
        CODES_CACHE.write_text(json.dumps(cache, indent=1, ensure_ascii=False), encoding="utf-8")
    table = []
    for s in stocks:
        r = cache.get(s["nse"].upper(), {})
        table.append({"nse": s["nse"], "name": s["name"], "nse_name": r.get("nse_name"),
                      "bse": r.get("bse"), "bse_name": r.get("bse_name"),
                      "matched_by": r.get("matched_by"), "status": r.get("status", "?")})
    return cache, table


# --------------------------------------------------------------------------- #
#  Filings: NSE
# --------------------------------------------------------------------------- #
def nse_session():
    s = requests.Session()
    s.headers.update({"User-Agent": UA, "Accept-Language": "en-US,en;q=0.9",
                      "Accept": "text/html,application/json,*/*"})
    s.get(NSE_HOME, timeout=15)
    s.get(NSE_FILINGS_PAGE, timeout=15)
    return s


def parse_nse_time(s):
    for fmt in ("%d-%b-%Y %H:%M:%S", "%Y-%m-%d %H:%M:%S", "%d-%b-%Y %H:%M"):
        try:
            return dt.datetime.strptime(s.strip(), fmt).replace(tzinfo=IST)
        except Exception:
            pass
    return None


def fetch_nse(stocks, since, until):
    """Per-symbol NSE API. Raises if NSE refuses us (caller falls back to RSS)."""
    sess = nse_session()
    out, fails = [], 0
    for s in stocks:
        params = {"index": "sme" if s.get("sme") else "equities", "symbol": s["nse"],
                  "from_date": since.strftime("%d-%m-%Y"), "to_date": until.strftime("%d-%m-%Y")}
        try:
            r = sess.get(NSE_API, params=params, timeout=30,
                         headers={"Referer": NSE_FILINGS_PAGE, "Accept": "application/json"})
            r.raise_for_status()
            rows = r.json()
            rows = rows.get("data", rows) if isinstance(rows, dict) else rows
        except Exception:
            fails += 1
            if fails >= 3 and not out:
                raise RuntimeError("NSE API refused")
            continue
        for row in rows or []:
            when = parse_nse_time(row.get("an_dt") or row.get("sort_date") or "")
            if not when or when < since:
                continue
            subject = clean(row.get("desc"))
            detail = clean(row.get("attchmntText"))
            out.append({"nse": s["nse"], "exchange": "NSE", "when": when,
                        "subject": subject, "detail": detail,
                        "link": row.get("attchmntFile") or "",
                        "text": f"{subject} {detail}"})
        time.sleep(0.4)
    return out


def fetch_nse_rss(stocks, since):
    """Fallback: NSE's rolling announcements RSS (recent items only)."""
    try:
        feed = feedparser.parse(_get(NSE_RSS, relay=True).content)
    except Exception:
        return None
    names = {norm_name(s["name"]): s["nse"] for s in stocks}
    syms = {s["nse"].upper() for s in stocks}
    out = []
    for e in feed.entries:
        when = None
        if e.get("published_parsed"):
            when = dt.datetime(*e.published_parsed[:6], tzinfo=dt.timezone.utc).astimezone(IST)
        if not when:
            when = parse_nse_time(e.get("published", "") or "")
        if when and when < since:
            continue
        link = e.get("link", "")
        m = re.search(r"/([A-Z0-9&-]+)_\d{8}", link)
        sym = m.group(1) if m and m.group(1) in syms else names.get(norm_name(e.get("title", "")))
        if not sym:
            continue
        detail = clean(e.get("description") or e.get("summary"))
        out.append({"nse": sym, "exchange": "NSE", "when": when or now_ist(),
                    "subject": detail[:140], "detail": detail, "link": link, "text": detail})
    return out


# --------------------------------------------------------------------------- #
#  Filings: BSE
# --------------------------------------------------------------------------- #
def parse_bse_time(s):
    s = (s or "").split(".")[0]
    try:
        return dt.datetime.fromisoformat(s).replace(tzinfo=IST)
    except Exception:
        return None


def fetch_bse_one(stock, code, since, until):
    out, page = [], 1
    while page <= 10:
        url = (f"{BSE_API}/AnnSubCategoryGetData/w?pageno={page}&strCat=-1"
               f"&strPrevDate={since:%Y%m%d}&strScrip={code}&strSearch=P"
               f"&strToDate={until:%Y%m%d}&strType=C&subcategory=-1")
        r = _get(url, headers=BSE_HEADERS, timeout=30)
        data = r.json()
        rows = data.get("Table") or []
        if not rows:
            break
        for row in rows:
            when = parse_bse_time(row.get("News_submission_dt") or row.get("NEWS_DT") or row.get("DT_TM"))
            if not when or when < since:
                continue
            subject = clean(row.get("NEWSSUB"))
            subject = re.sub(r"^.*?-\s*\d{6}\s*-\s*", "", subject)  # "Name - 511742 - Subject"
            detail = clean(row.get("HEADLINE"))
            att = row.get("ATTACHMENTNAME") or ""
            link = (BSE_PDF + att) if att else ""
            if att and when < now_ist() - dt.timedelta(days=4):
                link = BSE_PDF_HIST + att
            cat = " ".join(filter(None, [row.get("CATEGORYNAME"), row.get("SUBCATNAME")]))
            out.append({"nse": stock["nse"], "exchange": "BSE", "when": when,
                        "subject": subject, "detail": detail, "link": link,
                        "text": f"{cat} {subject} {detail}"})
        total = (data.get("Table1") or [{}])[0].get("ROWCNT") or 0
        if page * len(rows) >= int(total or 0):
            break
        page += 1
    return out


def fetch_bse(stocks, codes, since, until):
    out, fails, tried = [], 0, 0
    jobs = [(s, codes.get(s["nse"].upper(), {}).get("bse")) for s in stocks]
    jobs = [(s, c) for s, c in jobs if c]
    with cf.ThreadPoolExecutor(max_workers=4) as ex:
        futs = [ex.submit(fetch_bse_one, s, c, since, until) for s, c in jobs]
        for f in futs:
            tried += 1
            try:
                out.extend(f.result())
            except Exception:
                fails += 1
    return out, tried, fails


# --------------------------------------------------------------------------- #
#  Merge + classify filings
# --------------------------------------------------------------------------- #
def merge_filings(rows, comp):
    for r in rows:
        r["tier"], r["rule"] = classify(r["text"], comp)
    rows.sort(key=lambda r: (r["nse"], r["when"]))
    merged = []
    for r in rows:
        twin = None
        for m in merged[::-1]:
            if m["nse"] != r["nse"]:
                break
            if (r["exchange"] not in m["exchange_set"]
                    and abs((m["when"] - r["when"]).total_seconds()) < 6 * 3600
                    and (m["rule"] == r["rule"] and m["rule"] is not None
                         or jaccard(m["text"], r["text"]) >= 0.45)):
                twin = m
                break
        if twin is not None and r["exchange"] not in twin["exchange_set"]:
            twin["exchange_set"].add(r["exchange"])
            twin["links"].append((r["exchange"], r["link"]))
            if len(r["detail"]) > len(twin["detail"]):
                twin["detail"] = r["detail"]
            if r["tier"] == "high":
                twin["tier"], twin["rule"] = "high", r["rule"]
            continue
        r = dict(r)
        r["exchange_set"] = {r["exchange"]}
        r["links"] = [(r["exchange"], r["link"])]
        merged.append(r)
    return merged


# --------------------------------------------------------------------------- #
#  News: Google News RSS
# --------------------------------------------------------------------------- #
def fetch_news_one(stock, since, comp, days):
    window = f" when:{max(2, days)}d"
    items, seen = [], set()
    for phrase in stock.get("news") or [stock["name"]]:
        url = GNEWS.format(q=quote_plus(f'"{phrase}"{window}'))
        try:
            feed = feedparser.parse(_get(url, timeout=25).content)
        except Exception:
            continue
        must = re.compile(stock["title_must"], re.I) if stock.get("title_must") else None
        excl = re.compile(stock["title_exclude"], re.I) if stock.get("title_exclude") else None
        for e in feed.entries:
            title = clean(e.get("title"))
            src = clean((e.get("source") or {}).get("title")) if e.get("source") else ""
            if src and title.endswith(" - " + src):
                title = title[: -len(src) - 3]
            when = (dt.datetime(*e.published_parsed[:6], tzinfo=dt.timezone.utc).astimezone(IST)
                    if e.get("published_parsed") else None)
            if when and when < since:
                continue
            check = excl.sub(" ", title) if excl else title
            on_target = (must.search(check) if must else
                         any(p.lower() in check.lower() for p in stock.get("news") or [stock["name"]]))
            if not on_target:
                continue
            if first_match(comp["news_drop"], title):
                continue
            trig = first_match(comp["news_trig"], title)
            if not trig:
                continue
            key = " ".join(sorted(tokens(title)))
            if key in seen or any(jaccard(title, i["title"]) >= 0.6 for i in items):
                continue
            seen.add(key)
            items.append({"nse": stock["nse"], "title": title, "source": src or "News",
                          "link": e.get("link", ""), "when": when, "rule": trig,
                          "trusted": bool(first_match(comp["trusted"], src))})
    items.sort(key=lambda i: (not i["trusted"], -(i["when"] or since).timestamp()))
    return items


def fetch_news(stocks, since, comp, cap, days):
    out, fails = [], 0
    with cf.ThreadPoolExecutor(max_workers=6) as ex:
        futs = {ex.submit(fetch_news_one, s, since, comp, days): s for s in stocks}
        for f in cf.as_completed(futs):
            try:
                out.extend(f.result()[:cap])
            except Exception:
                fails += 1
    return out, fails


# --------------------------------------------------------------------------- #
#  Orchestration
# --------------------------------------------------------------------------- #
def window_start(rules, days=None):
    now = now_ist()
    if days:
        return now - dt.timedelta(days=days)
    base = rules.get("settings", {}).get("filings_lookback_hours", 30)
    since = now - dt.timedelta(hours=base)
    # Catch up after weekends / missed runs: start 1h before the last good run (max 4 days).
    if STATE_FILE.exists():
        try:
            last = dt.datetime.fromisoformat(json.loads(STATE_FILE.read_text())["last_run"])
            since = min(since, max(last - dt.timedelta(hours=1), now - dt.timedelta(days=4)))
        except Exception:
            pass
    return since


def run(days=None, write_state=True):
    stocks, rules, comp = load_config()
    by_sym = {s["nse"]: s for s in stocks}
    now = now_ist()
    since = window_start(rules, days)
    health = {}

    codes, table = resolve_codes(stocks)

    # Filings — NSE (API → RSS fallback) and BSE
    nse_rows = []
    try:
        nse_rows = fetch_nse(stocks, since, now)
        health["NSE"] = f"ok · {len(nse_rows)}"
    except Exception:
        rss = fetch_nse_rss(stocks, since)
        if rss is None:
            health["NSE"] = "unavailable"
        else:
            nse_rows = rss
            health["NSE"] = f"RSS fallback · {len(rss)}"
    bse_rows, tried, fails = fetch_bse(stocks, codes, since, now)
    health["BSE"] = (f"ok · {len(bse_rows)}" if tried and fails < tried
                     else "unavailable" if tried else "no codes yet")
    if fails and tried and fails < tried:
        health["BSE"] += f" ({fails} stock(s) failed)"

    filings = merge_filings(nse_rows + bse_rows, comp)

    # News
    ncfg = rules["news"]
    news_since = min(since, now - dt.timedelta(hours=ncfg.get("lookback_hours", 36)))
    news, nfails = fetch_news(stocks, news_since, comp, ncfg.get("max_per_stock", 3),
                              days or 2)
    # skip headlines that only restate a filing for the same stock
    news = [n for n in news
            if not any(f["nse"] == n["nse"] and jaccard(f["text"], n["title"]) >= 0.4
                       for f in filings if f["tier"] == "high")]
    health["News"] = f"ok · {len(news)}" if nfails < len(stocks) else "unavailable"

    def stock_view(sym):
        s = by_sym.get(sym, {"nse": sym, "name": sym, "group": "holding"})
        return {"nse": sym, "name": s["name"], "group": s.get("group", "holding")}

    key = lambda r: -r["when"].timestamp() if r.get("when") else 0
    tiers = {t: sorted([f for f in filings if f["tier"] == t], key=key)
             for t in ("high", "medium", "other", "drop")}
    result = {
        "generated": now.isoformat(timespec="minutes"),
        "since": since.isoformat(timespec="minutes"),
        "stock_count": len(stocks),
        "health": health,
        "high": [dict(f, stock=stock_view(f["nse"])) for f in tiers["high"]],
        "medium": [dict(f, stock=stock_view(f["nse"])) for f in tiers["medium"]],
        "other": [dict(f, stock=stock_view(f["nse"])) for f in tiers["other"]],
        "dropped": len(tiers["drop"]),
        "news": [dict(n, stock=stock_view(n["nse"])) for n in sorted(news, key=key)],
        "codes": table,
        "backtest_days": days,
    }

    DATA.mkdir(parents=True, exist_ok=True)
    if not days:
        (DATA / "portfolio.json").write_text(json.dumps(result, default=_json, indent=1,
                                                        ensure_ascii=False), encoding="utf-8")
        ok = any(v.startswith(("ok", "RSS")) for k, v in health.items() if k != "News")
        if write_state and ok:
            STATE_FILE.write_text(json.dumps({"last_run": now.isoformat()}), encoding="utf-8")
    return result


def _json(o):
    if isinstance(o, dt.datetime):
        return o.isoformat(timespec="minutes")
    if isinstance(o, set):
        return sorted(o)
    return str(o)


# --------------------------------------------------------------------------- #
#  Rendering (uses digest.py's CSS classes; extra styles in PF_CSS)
# --------------------------------------------------------------------------- #
PF_CSS = """
.pf-health{font-size:12.5px;color:var(--muted);margin:-6px 0 22px}
.pf-health b{font-weight:600;color:var(--ink)}
.pf-sec{font-size:15.5px;font-weight:700;margin:26px 0 10px;display:flex;align-items:center;gap:8px}
.pf-item{background:var(--card);border:1px solid var(--line);border-radius:12px;
  padding:13px 16px;margin-bottom:10px}
.pf-item.hi{border-left:4px solid #C0392B}
.pf-top{display:flex;flex-wrap:wrap;align-items:center;gap:6px;font-size:12px;color:var(--muted)}
.pf-sym{font-weight:700;font-size:12px;border-radius:5px;padding:0 7px;line-height:20px;
  background:var(--green-soft);color:var(--green-deep)}
.pf-wl{font-size:11px;font-weight:600;border-radius:5px;padding:0 6px;line-height:18px;
  background:#F6EBC4;color:#7A5F0E}
.pf-tr{font-size:11px;font-weight:600;border-radius:5px;padding:0 6px;line-height:18px;
  background:var(--blue-soft);color:var(--blue-deep)}
.pf-item h3{font-size:15.5px;font-weight:650;line-height:1.35;margin:5px 0 3px}
.pf-item h3 a:hover{text-decoration:underline;text-underline-offset:3px}
.pf-item p{margin:0;font-size:14px;color:var(--muted)}
.pf-links{display:flex;gap:6px;margin-top:8px}
.pf-links a{font-size:12px;font-weight:600;color:var(--blue-deep);background:var(--blue-soft);
  border-radius:5px;padding:1px 8px}
.pf-list{list-style:none;margin:0;padding:0}
.pf-list li{font-size:14px;padding:7px 0;border-top:1px solid var(--line);line-height:1.4}
.pf-list li:first-child{border-top:0}
.pf-list a:hover{text-decoration:underline;text-underline-offset:3px}
.pf-list .m{color:var(--muted);font-size:12px}
.pf-quiet{background:var(--green-soft);border-radius:12px;padding:16px 18px;color:var(--green-deep);font-weight:600}
.pf-scroll{overflow-x:auto;-webkit-overflow-scrolling:touch}
.pf-codes{min-width:620px;width:100%;border-collapse:collapse;font-size:13px}
.pf-codes th,.pf-codes td{text-align:left;padding:5px 6px;border-top:1px solid var(--line);vertical-align:top}
.pf-codes .bad{color:#C0392B;font-weight:600}
.pf-alert{display:flex;gap:12px;align-items:center;background:#FBE9E7;border-radius:12px;
  padding:12px 16px;margin-bottom:22px;font-size:14.5px}
.pf-alert a{font-weight:700;color:#8E2B1F}
"""


def _e(s):
    return html.escape(str(s or ""), quote=True)


def _when(w):
    if not w:
        return "—"
    if isinstance(w, str):
        w = dt.datetime.fromisoformat(w)
    return w.astimezone(IST).strftime("%d %b %H:%M")


def _chips(stock):
    out = f'<span class="pf-sym">{_e(stock["nse"])}</span>'
    if stock.get("group") == "watchlist":
        out += '<span class="pf-wl">watchlist</span>'
    return out


def _filing_card(f):
    links = "".join(f'<a href="{_e(u)}">{_e(x)} filing</a>' for x, u in f["links"] if u)
    title_link = next((u for _, u in f["links"] if u), "")
    # Lead with what the filing says; the exchange's category becomes a label.
    label = _short_subject(f["subject"])
    body = f["detail"] if len(f["detail"] or "") > 15 else f["subject"]
    title = _e(clean(body, 220))
    head = f'<a href="{_e(title_link)}">{title}</a>' if title_link else title
    return (f'<div class="pf-item hi"><div class="pf-top">{_chips(f["stock"])}'
            f'<span>{_e(f["stock"]["name"])}</span><span>· {_when(f["when"])}</span>'
            + (f'<span class="pf-tr">{_e(label)}</span>' if label else "") + '</div>'
            f'<h3>{head}</h3>' +
            f'<div class="pf-links">{links}</div></div>')


def _short_subject(subj):
    """'Announcement under Regulation 30 (LODR)-Credit Rating' -> 'Credit Rating'."""
    s = re.sub(r"(?i)^.*?\(lodr\)\s*-\s*", "", subj or "")
    s = re.sub(r"(?i)^(announcement|disclosure) under regulation \d+\s*", "", s).strip(" -")
    return clean(s, 48)


def _news_row(n):
    tr = '<span class="pf-tr">trusted</span>' if n["trusted"] else ""
    return (f'<div class="pf-item"><div class="pf-top">{_chips(n["stock"])}'
            f'<span>{_e(n["source"])}</span>{tr}<span>· {_when(n["when"])}</span></div>'
            f'<h3><a href="{_e(n["link"])}">{_e(n["title"])}</a></h3></div>')


def _line(f):
    url = next((u for _, u in f["links"] if u), "")
    t = _e(clean(f["detail"] if len(f["detail"] or "") > 15 else f["subject"], 150))
    t = f'<a href="{_e(url)}">{t}</a>' if url else t
    return (f'<li>{_chips(f["stock"])} {t} <span class="m">· {_when(f["when"])}'
            f' · {"/".join(sorted(f["exchange_set"]))}</span></li>')


def render_alert(pf):
    """Small banner for the Overview tab."""
    n_hi, n_news = len(pf["high"]), len(pf["news"])
    if not (n_hi or n_news):
        return ""
    bits = []
    if n_hi:
        bits.append(f"{n_hi} flagged filing{'s' * (n_hi != 1)}")
    if n_news:
        bits.append(f"{n_news} news item{'s' * (n_news != 1)}")
    names = sorted({x["stock"]["nse"] for x in pf["high"] + pf["news"]})
    return (f'<div class="pf-alert"><span>💼</span><span><b>Portfolio:</b> '
            f'{" · ".join(bits)} ({_e(", ".join(names[:6]))}{"…" if len(names) > 6 else ""}) — '
            f'<a href="#portfolio">open the Portfolio tab</a></span></div>')


def render_pane(pf, show_other=True, show_codes=False):
    h = pf["health"]
    hl = " · ".join(f"{k} <b>{_e(v)}</b>" for k, v in h.items())
    span = (f"last {pf['backtest_days']} days" if pf.get("backtest_days")
            else f"since {_when(pf['since'])}")
    out = [f'<h2 class="block">💼 Portfolio</h2>',
           f'<div class="pf-health">{pf["stock_count"]} stocks · {span} · {hl}'
           f' · {pf["dropped"]} routine filings hidden</div>']

    if not (pf["high"] or pf["news"]):
        out.append(f'<div class="pf-quiet">Nothing material across your {pf["stock_count"]} '
                   f'stocks {span}.</div>')
    if pf["high"]:
        out.append(f'<div class="pf-sec">🔴 Flagged filings <span class="cnt">{len(pf["high"])}</span></div>')
        out.extend(_filing_card(f) for f in pf["high"])
    if pf["news"]:
        out.append(f'<div class="pf-sec">📰 In the news <span class="cnt">{len(pf["news"])}</span></div>')
        out.extend(_news_row(n) for n in pf["news"])
    if pf["medium"]:
        out.append(f'<div class="pf-sec">🟡 Worth knowing <span class="cnt">{len(pf["medium"])}</span></div>')
        out.append('<ul class="pf-list">' + "".join(_line(f) for f in pf["medium"]) + '</ul>')
    if show_other and pf["other"]:
        out.append(f'<details class="more" style="margin-top:22px"><summary>⚪ Other filings '
                   f'({len(pf["other"])}) — unmatched by any rule</summary>'
                   '<ul class="pf-list">' + "".join(_line(f) for f in pf["other"]) + '</ul></details>')
    if show_codes:
        rows = pf["codes"] if show_codes == "all" else [
            r for r in pf["codes"] if r["status"] not in ("ok", "NSE only")]
        out.append('<div class="pf-sec">🔢 Code mapping — please check</div>'
                   '<div class="pf-scroll"><table class="pf-codes"><tr><th>NSE</th><th>Your name</th><th>NSE name</th>'
                   '<th>BSE code</th><th>BSE name</th><th>Matched by</th><th>Status</th></tr>')
        for r in rows:
            bad = ' class="bad"' if r["status"] not in ("ok", "NSE only") else ""
            out.append(f'<tr><td>{_e(r["nse"])}</td><td>{_e(r["name"])}</td><td>{_e(r["nse_name"])}</td>'
                       f'<td>{_e(r["bse"])}</td><td>{_e(r["bse_name"])}</td>'
                       f'<td>{_e(r["matched_by"])}</td><td{bad}>{_e(r["status"])}</td></tr>')
        out.append('</table></div>')
    return "\n".join(out)


def section(settings=None):
    """Called by digest.py. Never raises: a failure shows as a note in the tab."""
    try:
        pf = run()
        rules = yaml.safe_load(open(ROOT / "portfolio_rules.yaml", encoding="utf-8"))
        show_other = rules.get("settings", {}).get("show_other_filings", True)
        unresolved = [r for r in pf["codes"] if r["status"] not in ("ok", "NSE only")]
        pane = render_pane(pf, show_other=show_other, show_codes="problems" if unresolved else False)
        print(f"  portfolio: {len(pf['high'])} flagged, {len(pf['news'])} news, "
              f"{len(pf['medium'])} medium, {len(pf['other'])} other · {pf['health']}")
        return {"pane": pane, "alert": render_alert(pf), "css": PF_CSS,
                "count": len(pf["high"]) + len(pf["news"])}
    except Exception as exc:                                    # pragma: no cover
        print(f"  portfolio: FAILED ({type(exc).__name__}: {exc})")
        return {"pane": ('<h2 class="block">💼 Portfolio</h2><div class="pf-quiet">'
                         f'Tracker failed this run ({_e(type(exc).__name__)}). '
                         'Check the GitHub Actions log.</div>'),
                "alert": "", "css": PF_CSS, "count": 0}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, help="backtest: look back N days")
    ap.add_argument("--out", help="write a standalone HTML page here")
    args = ap.parse_args()
    pf = run(days=args.days, write_state=not args.days)
    print(json.dumps({"health": pf["health"], "high": len(pf["high"]), "news": len(pf["news"]),
                      "medium": len(pf["medium"]), "other": len(pf["other"]),
                      "dropped": pf["dropped"]}, indent=1))
    print("\nCode mapping:")
    for r in pf["codes"]:
        print(f"  {r['nse']:<12} BSE {str(r['bse']):<8} {r['matched_by'] or '':<14} {r['status']}")
    if args.out:
        import digest                                  # reuse the Daily Wire look
        page = (digest.HEAD.format(title="Portfolio backtest", date=now_ist().strftime("%d %b %Y"),
                                   css=digest.CSS + PF_CSS)
                + '<header class="hero"><h1 class="title">Portfolio backtest</h1>'
                  f'<div class="date">Generated {now_ist():%d %b %Y %H:%M} IST</div></header>'
                + '<section class="pane on" id="portfolio" style="display:block">'
                + render_pane(pf, show_other=True, show_codes="all")
                + '</section><footer><span></span><a href="index.html">◂ Today</a></footer>'
                  '</div></body></html>')
        out = pathlib.Path(args.out)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(page, encoding="utf-8")
        print(f"\n  wrote {out}")


if __name__ == "__main__":
    sys.exit(main())
