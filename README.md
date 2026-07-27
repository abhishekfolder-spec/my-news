# The Daily Wire — automated news digest

Pulls your Substacks, X (Twitter) Lists, Economic Times and Moneycontrol once a
day, builds a single HTML dashboard (a ranked **Brief** up top, the **full list**
grouped by theme below), and publishes it to a private-ish GitHub Pages URL you
can bookmark. Runs on a schedule on GitHub's servers — your computer can be off.

Every source is just an RSS/Atom feed, so adding or removing anything later is a
one-line edit in `config.yaml`.

---

## How it works

```
config.yaml ──► digest.py ──►  docs/index.html      (today, overwritten daily)
  (sources)      (fetch,        docs/archive/DATE.html (permanent copy)
                  enrich,        docs/archive.html      (index of past days)
                  summarise,
                  render)
        ▲
        └── GitHub Actions runs it daily and commits the result. GitHub Pages
            serves docs/ as a website.
```

- **Substacks** → native `/feed` (full article text on free posts).
- **X / Twitter** → 3 themed **Lists**, each turned into one RSS feed (see below).
- **ET / Moneycontrol** → native section feeds, with the full article body pulled
  in via a readability extractor (`enrich: true`).
- **Summary** → a free heuristic **Brief** by default; optional AI brief if you
  add an API key.

---

## One-time setup (≈15 min)

1. **Create a GitHub repo** (private is fine) and add all these files.
2. **Settings → Actions → General → Workflow permissions** → select
   **Read and write permissions** → Save. *(Lets the daily job commit the HTML.)*
3. **Settings → Pages** → Source: **Deploy from a branch** → Branch: `main`,
   folder: **/docs** → Save. Your dashboard will live at
   `https://<you>.github.io/<repo>/`.
4. **Actions tab → Daily News Digest → Run workflow** to trigger the first build
   manually (don't wait for the schedule). Refresh your Pages URL after ~1 min.

The schedule (in `.github/workflows/daily.yml`) is `30 1 * * *` = 07:00 IST.
Cron is always UTC — change the numbers to move the time.

---

## Configuring sources — `config.yaml`

Each source is:

```yaml
  - name: "ET · Markets"          # label on the dashboard
    url:  "https://…/feed"        # RSS/Atom URL
    category: "India Markets"     # which section it groups under
    enrich: true                  # optional: pull full text (for headline feeds)
```

Anything whose `url` still starts with `PASTE_` is **skipped**, so half-finished
config never breaks the run. The build log prints `ok / skip / FAIL` per source.

### Substacks
Every Substack feed is `<publication-address>/feed`. Three are pre-filled with my
best guess (marked `VERIFY`); the rest are `PASTE_SUBSTACK_URL/feed`. To fill one
in: open the publication in a browser, take its address, add `/feed`. If a
`VERIFY` one shows `FAIL` in the log, the guessed subdomain was wrong — replace it.

### X / Twitter — the 3 Lists
Native per-account RSS no longer exists and free feed-generators cap you at a
handful of feeds, so we collapse ~90 handles into **3 X Lists → 3 feeds**:

1. On X, create three Lists and add the handles (my proposed split below).
2. Open a List, copy its URL (`https://x.com/i/lists/………`).
3. Paste it into a generator — **rss.app** or **Inoreader** (free tiers) — and
   copy the RSS URL it gives you.
4. Put that RSS URL in `config.yaml` (replacing the matching `PASTE_X_LIST_RSS_…`).

**Proposed handle split** (adjust freely — membership is just who you add to each
List; a few handles look mistyped and will simply be skipped if they 404):

**List A — India Markets & Investing**
`@logical_trader @ajaya_buddy @kapichopra72 @InvestwithJoshi @harsh_vardhhan
@rahulrao_1992 @9onecapital @navdeepdahiya55 @vishalbhargava5 @sproutresearch1
@cardniti @puneetk009 @zennivesh @Rmantri @iamjubinmj @superdhaasu @kashyap286
@travelbluez @debu_neogi @persistencecap @investor_vineet @alchemist1320
@digantharia @aseemdhru @capitalmind_in @dhruvrrawani @euityinsightss
@jitenkparmar @chhotesaab @theharshfolio @moneyworks4u_fa @idsrinivas @hkuppy
@chins1729 @incredcapital @finstor85 @jeevanpatwa @suru27 @tusharbohra @tijori1
@udaykotak @sahilkapoort @amitmantri @rohitchauahan @jatin_khemani @contrarianEPS
@hchawlah @indiaER @unseenvalue @jaganmsna @sartanparayash @sab_maya_hai
@random_gyan @smartsyncserv @indianviking1`

**List B — Global Macro & Economics**
`@Peter_Atwater @garysavage11 @crossbordercap @themichaelevery @icecapglobal
@lukegromen @trinhomics @kaul_vivek @glennluk @epsilontheory @iambremmer
@tanvi_ratna @avtram @INartecarlodoss @agnostoxxx`

**List C — Tech · Energy · Policy & Other**
`@_krishashok @claudeai @utilitydive @alexwg @abcampbell @ankitiima @balajis
@svembu @aravind @naval @shmikaravi @ember_energy @andrewng @mgsolidarity
@jigarshahdc @industrlplicy @itstarh`

---

## Optional: AI-written brief
The free build lists the day's top headlines per section. For a written analyst
brief instead:

1. Get an API key at `console.anthropic.com`.
2. Repo **Settings → Secrets and variables → Actions → New repository secret**,
   name it `ANTHROPIC_API_KEY`.
3. In `config.yaml` set `use_ai_summary: true` (and `ai_model` to a model your key
   can access).

It costs a few cents a day and never blocks the run — if the call fails, the free
brief is used automatically.

---

## Run it locally
```bash
pip install -r requirements.txt
python digest.py            # real run
python digest.py --demo     # render sample data (no network) to preview design
open docs/index.html
```

## Tuning
- `lookback_hours` — window of "today" (default 24).
- `enrich_full_text` / `enrich_limit_per_feed` — full-text pulls; lower if runs
  get slow.
- `category_order` — section order on the page.
- `max_items_per_feed` — per-feed cap.

## Troubleshooting
- **A feed shows `FAIL`** — the URL is wrong or the site blocked the fetch. Open
  the URL in a browser to check; ET/Moneycontrol occasionally rate-limit.
- **Pages shows nothing** — confirm Settings → Pages points at `/docs`, and that
  the first workflow run finished green.
- **Nothing today** — if all feeds legitimately had no items in the last
  `lookback_hours`, the sections will be sparse. Widen the window to test.
