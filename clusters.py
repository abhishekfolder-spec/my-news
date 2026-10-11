"""Group headlines that cover the same story, then assign each group a theme.

Pure Python, no extra dependencies: TF-IDF over titles plus a short summary,
greedy grouping by cosine similarity, and keyword rules for themes.
Cheap enough for a few hundred items on every daily run.
"""
import math
import re
import datetime as dt
from collections import Counter

EPOCH = dt.datetime(1970, 1, 1, tzinfo=dt.timezone.utc)

STOP = set("""
the and for with from that this these those are was were been being has have had
will would could should may might can not but its it's their there they them
after before over under into about amid says say said new more than also just
how why what who when where which while as at by on in of to up out off per vs
""".split())

WORD = re.compile(r"[a-z][a-z0-9'&\-]{2,}")

# Phrases outlets use interchangeably; mapped to one token before matching.
ALIASES = [
    (r"reserve bank of india", "rbi"),
    (r"federal reserve|us fed", "fed"),
    (r"strait of hormuz", "hormuz"),
    (r"european central bank", "ecb"),
    (r"bank of england", "boe"),
    (r"bank of japan", "boj"),
    (r"united states|the us\b|u\.s\.", "us"),
    (r"united kingdom|britain's|\buk\b", "uk"),
]
_ALIAS_RX = [(re.compile(p), r) for p, r in ALIASES]


def tokens(text):
    out = []
    text = (text or "").lower()
    for rx, repl in _ALIAS_RX:
        text = rx.sub(repl, text)
    for t in WORD.findall(text):
        t = t.replace("'s", "").strip("'-&")
        if len(t) > 2 and t not in STOP:
            out.append(t)
    return out


def _unit(v):
    norm = math.sqrt(sum(x * x for x in v.values()))
    return {t: x / norm for t, x in v.items()} if norm else {}


def _vectors(docs):
    df = Counter()
    for d in docs:
        df.update(set(d))
    n = len(docs)
    idf = {t: math.log((1 + n) / (1 + c)) + 1.0 for t, c in df.items()}
    return [_unit({t: (1 + math.log(c)) * idf[t] for t, c in Counter(d).items()})
            for d in docs]


def _cos(a, b):
    if len(a) > len(b):
        a, b = b, a
    return sum(x * b.get(t, 0.0) for t, x in a.items())


def _doc_text(it):
    # title counted twice so headline words outweigh summary words
    return f"{it['title']} {it['title']} {(it.get('summary') or '')[:240]}"


def group(items, threshold):
    """Return a list of groups; each group is a list of item indexes."""
    vecs = _vectors([tokens(_doc_text(it)) for it in items])
    clusters = []  # {"sum": accumulated vector, "centroid": unit vector, "members": [...]}
    for i, v in enumerate(vecs):
        best, score = None, 0.0
        for c in clusters:
            s = _cos(v, c["centroid"])
            if s > score:
                best, score = c, s
        if best is not None and score >= threshold:
            best["members"].append(i)
            acc = best["sum"]
            for t, x in v.items():
                acc[t] = acc.get(t, 0.0) + x
            best["centroid"] = _unit(acc)
        else:
            clusters.append({"sum": dict(v), "centroid": dict(v), "members": [i]})
    return [c["members"] for c in clusters]


def _when(it):
    return it["when"] or EPOCH


def _pub(source):
    return (source or "").split("·")[0].strip()


def _theme(cluster_items, rules, fallback):
    best_name, best_score = fallback, 0
    for name, rx in rules:
        score = 0
        for it in cluster_items:
            score += 2 * len(rx.findall(it["title"].lower()))
            score += len(rx.findall((it.get("summary") or "")[:400].lower()))
        if score > best_score:
            best_name, best_score = name, score
    return best_name


def build_clusters(items, settings):
    """Return clusters: dicts with lead item, members, outlets, theme and time."""
    threshold = settings.get("cluster_threshold", 0.35)
    fallback = settings.get("theme_fallback", "Macro")
    rules = [(r["theme"], re.compile(r"\b(?:%s)" % r["keywords"], re.I))
             for r in settings.get("theme_rules", [])]

    out = []
    for idxs in group(items, threshold):
        members = sorted((items[i] for i in idxs), key=_when, reverse=True)
        outlets, seen = [], set()
        for it in members:  # newest first, one chip per outlet
            p = _pub(it["source"])
            if p and p not in seen:
                seen.add(p)
                outlets.append((p, it["link"]))
        lead = members[0]
        out.append({
            "lead": lead,
            "items": members,
            "outlets": outlets,
            "theme": _theme(members, rules, fallback),
            "when": lead["when"],
        })
    return out
