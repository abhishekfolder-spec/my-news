"""
Self-contained summariser. No API key, no network, no ML model downloads —
just Python. Two jobs:

  * summarize_text(...)  -> condense one article into its key sentence(s)
  * build_brief(...)     -> rank the day's items, cluster near-duplicates into
                            "themes", and return the top stories for the Brief

Method: classic extractive summarisation (term-frequency sentence scoring) plus
lightweight TF-IDF cosine clustering. Deterministic and fast on a few hundred
items, which is all a daily digest ever has.
"""

import re
import math
from collections import Counter

# A compact English stop-word list (kept inline so there's nothing to download).
STOPWORDS = set("""
a about above after again against all am an and any are aren't as at be because
been before being below between both but by can can't cannot could couldn't did
didn't do does doesn't doing don't down during each few for from further had
hadn't has hasn't have haven't having he he'd he'll he's her here here's hers
herself him himself his how how's i i'd i'll i'm i've if in into is isn't it it's
its itself let's me more most mustn't my myself no nor not of off on once only or
other ought our ours ourselves out over own same shan't she she'd she'll she's
should shouldn't so some such than that that's the their theirs them themselves
then there there's these they they'd they'll they're they've this those through
to too under until up very was wasn't we we'd we'll we're we've were weren't what
what's when when's where where's which while who who's whom why why's with won't
would wouldn't you you'd you'll you're you've your yours yourself yourselves also
said say says will new one two get amid via like just now still may might per
across into onto upon among toward towards
""".split())

_WORD = re.compile(r"[a-z0-9][a-z0-9'&+-]*")
# Split on sentence enders followed by whitespace + a capital / digit / quote.
_SENT = re.compile(r"(?<=[.!?])\s+(?=[\"'“(A-Z0-9])")


def tokenize(text):
    toks = _WORD.findall((text or "").lower())
    return [t for t in toks if t not in STOPWORDS and (len(t) > 2 or t.isdigit())]


def split_sentences(text):
    text = re.sub(r"\s+", " ", (text or "").strip())
    if not text:
        return []
    parts = _SENT.split(text)
    # Merge stray tiny fragments into the previous sentence.
    out = []
    for p in parts:
        p = p.strip()
        if not p:
            continue
        if out and len(p.split()) < 4:
            out[-1] = out[-1] + " " + p
        else:
            out.append(p)
    return out


def _freqs(text):
    counts = Counter(tokenize(text))
    if not counts:
        return {}
    top = counts.most_common(1)[0][1]
    return {w: c / top for w, c in counts.items()}


def summarize_text(text, max_sentences=2, max_chars=340):
    """Return the most informative sentence(s) from `text`, in original order."""
    sents = split_sentences(text)
    if len(sents) <= max_sentences:
        s = " ".join(sents)
        return (s[:max_chars].rsplit(" ", 1)[0] + "…") if len(s) > max_chars else s

    freqs = _freqs(text)
    scored = []
    for i, s in enumerate(sents):
        words = tokenize(s)
        if not words:
            continue
        base = sum(freqs.get(w, 0) for w in words) / (len(words) ** 0.5)
        base *= 1.0 + (0.12 if i == 0 else 0.0)          # slight lead bonus
        if len(words) < 5 or len(words) > 45:            # dodge fragments/run-ons
            base *= 0.6
        scored.append((base, i, s))

    scored.sort(reverse=True)
    chosen = sorted(i for _, i, _ in scored[:max_sentences])
    summary = " ".join(sents[i] for i in chosen)
    if len(summary) > max_chars:
        summary = summary[:max_chars].rsplit(" ", 1)[0] + "…"
    return summary


# --------------------------------------------------------------------------- #
#  Brief: rank + cluster the day's items into themes
# --------------------------------------------------------------------------- #
def _vector(item):
    # Title terms count double — headlines carry the topic.
    text = (item["title"] + " ") * 2 + (item.get("summary") or "")
    return Counter(tokenize(text))


def _cosine(a, b, idf):
    common = set(a) & set(b)
    if not common:
        return 0.0
    dot = sum(a[t] * idf.get(t, 0) * b[t] * idf.get(t, 0) for t in common)
    na = math.sqrt(sum((a[t] * idf.get(t, 0)) ** 2 for t in a))
    nb = math.sqrt(sum((b[t] * idf.get(t, 0)) ** 2 for t in b))
    return dot / (na * nb) if na and nb else 0.0


def build_brief(items, max_themes=10, sim_threshold=0.20, summary_sentences=1):
    """Rank items by salience, cluster near-duplicates, return top themes.

    Each theme: {headline, link, source, category, gist, related}.
    """
    items = [it for it in items if it.get("title")]
    if not items:
        return []

    vecs = {id(it): _vector(it) for it in items}
    df = Counter()
    for v in vecs.values():
        df.update(v.keys())
    n = len(items)
    idf = {t: math.log((n + 1) / (c + 1)) + 1 for t, c in df.items()}

    # recency: newest = 1.0, decaying to ~0.6 over the window
    times = [it["when"] for it in items if it.get("when")]
    newest = max(times) if times else None
    oldest = min(times) if times else None
    span = (newest - oldest).total_seconds() if newest and oldest and newest > oldest else 1

    def salience(it):
        v = vecs[id(it)]
        content = sum(cnt * idf.get(t, 0) for t, cnt in v.items())
        rec = 1.0
        if it.get("when") and newest:
            rec = 0.6 + 0.4 * (1 - (newest - it["when"]).total_seconds() / span)
        return content * rec

    ranked = sorted(items, key=salience, reverse=True)

    clusters = []  # each: {"head": item, "members": [items]}
    for it in ranked:
        placed = False
        for cl in clusters:
            if _cosine(vecs[id(it)], vecs[id(cl["head"])], idf) >= sim_threshold:
                cl["members"].append(it)
                placed = True
                break
        if not placed:
            clusters.append({"head": it, "members": [it]})

    themes = []
    for cl in clusters[:max_themes]:
        h = cl["head"]
        body = h.get("full") or h.get("summary") or ""
        themes.append({
            "headline": h["title"],
            "link": h.get("link", ""),
            "source": h.get("source", ""),
            "category": h.get("category", ""),
            "gist": summarize_text(body, max_sentences=summary_sentences, max_chars=200),
            "related": len(cl["members"]) - 1,
        })
    return themes
