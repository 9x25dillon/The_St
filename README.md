# The Saint (The_St)

A local place to explore your own words and digital footprint. Start with personal
notes and journals, then compare with browser history or exports from TikTok, YouTube,
Instagram, X/Twitter, Spotify, Reddit, Amazon, or your device's own screen time.

## Android app

Build an offline development APK for your phone:

```sh
python android/build.py
```

The APK is written to `android/build/the-saint-debug.apk`. It supports every source
below except Firefox/Chrome history (which read a live local browser profile the
sandboxed picker can't reach), plus search and word counts. See
[Android setup](android/README.md) for SDK requirements, USB installation, and the
current mobile feature scope. Semantic clustering and the Profile Health panel are
desktop-only; Android doesn't run on-device embeddings.

## Start here

Requires Python 3.10 or newer. No packages, account, or network connection are
needed for the interface, import previews, word counts, and search.

```sh
python run.py
```

Open **http://127.0.0.1:8765**. Click **Try a sample journal**, or choose your own
Markdown/plain-text files and click **Import selected source**. Use
`python run.py --port 8766` if the default port is occupied. Stop with Ctrl+C.
On systems where Python is named `python3`, use that instead of `python`.

## Sources

Choose **Auto-detect from files** (the default) and drop in whatever export you have —
the app recognizes each tool's standard filenames and routes to the right adapter, and
tells you what it detected. If a file's name isn't recognized (renamed, or a format
this app doesn't know), it asks you to pick the source manually from the dropdown
rather than guessing wrong.

- **Personal notes & journal:** select `.md`, `.markdown`, or `.txt` files. Paragraphs
  become passages with their filename and section heading. YAML front matter and
  fenced code are excluded. Long paragraphs split into 120-word chunks. Notes are
  limited to 1 MB each, 500 selected files, 8 MB per request, and 5,000 passages.
- **TikTok:** select JSON export files. Expressed text, watch timestamps, and assigned
  interest labels remain separate. Watch links are never fetched. Schema matching
  is heuristic; missing streams may need an adapter adjustment for your export.
- **YouTube:** a Google Takeout export (`watch-history.json` / `search-history.json`).
  Search queries and watched video titles are kept separate; Takeout gives real
  titles, so watched entries are embeddable text, unlike TikTok's bare watch links.
  No assigned ad-interest categories in a standard Takeout export.
- **Instagram:** a Meta "Download your information" JSON export. Assigned topics
  (`your_topics.json`) and search history are kept separate, matched by field name
  inside Meta's `string_map_data` wrapper rather than by filename. Ad/post view
  history isn't parsed for v1 — a volume signal, not text you wrote.
- **X / Twitter:** an X archive export (`.js` files under `data/`). The
  `window.YTD.<stream>.part0 = ...` wrapper is stripped automatically. Search
  queries, your own post text, and assigned interest topics are kept separate.
  Classic tweet timestamps aren't ISO-8601 and are left blank rather than guessed.
- **Spotify:** an extended streaming history export
  (`Streaming_History_Audio_*.json`). A real limitation, stated plainly: streaming
  history can't tell a self-chosen play apart from one you clicked out of a
  recommendation. No assigned-category stream in the export.
- **Reddit:** a GDPR data export's `posts.csv` / `comments.csv`. Classified by each
  file's own header shape, not filename. Saved posts and vote history aren't parsed
  for v1.
- **Amazon:** a "Request My Data" order-history CSV
  (`Retail.OrderHistory.*.csv`). Repeat purchases of the same product collapse into
  one entry. Search history isn't always included in the export and isn't parsed.
- **Device screen time:** a `usage.json` file of `{"app", "minutes", "date"}` rows —
  there's no universal export for this, so build one yourself from whatever usage
  view your device offers. Represented as short text ("Instagram: 47 minutes") so it
  flows through the same pipeline as everything else, not as a dedicated chart.
- **Firefox / Chrome:** clicking Import reads the first detected local profile.
  SQLite's backup API reads committed WAL data into a temporary copy, which is
  removed after ingestion. Close the browser and retry if it is busy. Desktop only.
- **Sample journal:** synthetic prose for exploring the interface. Its word counts
  are real counts; it does not pretend to be a precomputed semantic analysis.

Each import **adds to** the current session rather than replacing it — import notes,
then a TikTok export, then a YouTube export, and they sit in one combined view (a
"sources in this session" list shows what's been added, and how much). **Clear
session** is still there for a full reset. Search and **Show more passages** let you
explore the source text across every imported source. Word counts are lexical counts,
not inferred themes.

To inspect a whole notes folder from the command line (including Obsidian Markdown):

```sh
python notes.py /path/to/notes
python tiktok.py /path/to/export --inspect
python youtube.py /path/to/export --inspect
python instagram.py /path/to/export --inspect
python x.py /path/to/export --inspect
python spotify.py /path/to/export --inspect
python reddit.py /path/to/export --inspect
python amazon.py /path/to/export --inspect
python usage.py /path/to/usage.json --inspect
```

The folder adapter skips hidden paths and symbolic links. The web picker imports
only the files you select; it does not scan your home folder.

## Optional semantic maps

Install the analysis dependencies, then download the embedding model explicitly:

```sh
python setup_local.py
python setup_local.py --download-model
python run.py
```

Setup creates a project `.venv`. Model setup downloads `all-MiniLM-L6-v2` to the
model library's local cache. Setup uses CPU-only PyTorch on Linux and Windows. These two setup steps need
internet access and can require substantial disk space. `run.py` automatically uses the project environment.
The basic app also runs independently with `python app.py` if setup fails.

Import at least 30 passages and click **Build semantic map**. The first run can
take several minutes. Web analysis uses only a cached model and does not download
one automatically. It clusters a 15-dimensional UMAP projection with HDBSCAN, then
makes a separate 2D projection for display. Passage rows expose GLOSH outlier scores.

TikTok labels appear as gold stars; hovering shows their closest expressed passage
by cosine similarity in the original embedding space. The 2D plot is exploratory:
distance alone does not prove a platform inference is wrong. Exports can omit context.

Building a map also computes a **Profile Health** panel from the same clustering —
signal-to-noise (how many passages score as clearly-yours signal vs. noise),
homogenization (how much of your data collapses into one dominant island vs. a fair
spread of several), and a combined profile health percentage. Each passage is tagged
`signal` or `flagged as noise`. All of this is a local read of your own already-computed
embedding space; nothing is sent anywhere, and nothing is fed back into any platform.
It's a lens on your own data, not a filter applied to it. Every event uses a uniform
weight for now — a way to mark your own events as more/less representative, and a
tracked history of how your profile drifts over time, are future modules (see below).

Existing command-line HTML workflows remain available in the environment:

```sh
.venv/bin/python mirror.py --demo --out /tmp/saint-demo.html
.venv/bin/python mirror.py --browser firefox --out /tmp/browser-map.html
.venv/bin/python notes.py /path/to/notes --out /tmp/notes-map.html
.venv/bin/python tiktok.py /path/to/export --out /tmp/tiktok-map.html
```

On Windows use `.venv\Scripts\python.exe`. Unlike web analysis, command-line
embedding may download the model if it is not already cached.

## Privacy and boundaries

The web server listens only on `127.0.0.1`, validates Host and Origin, and requires
a per-session token for changes. Assets are served locally with no CDN or analytics.
Imports and analysis results are held in process memory, with no application data
store or automatic export. All tabs connected to the same server share one session.

**Clear session** removes the current view. An already-running analysis releases its
snapshot when it finishes and cannot restore cleared data. Restarting the server
also clears the session. This is not a promise of secure RAM erasure or protection
against other software running as your user. Explicit CLI HTML exports contain
readable source text and are not encrypted; keep them somewhere private.

## Verify

```sh
python -m unittest discover -s tests -v
node --check web/app.js  # optional JavaScript syntax check, requires Node
node tests/browser_smoke.mjs  # optional Chromium check; start the app first
.venv/bin/python tests/semantic_smoke.py  # after optional setup and model download
.venv/bin/python -m unittest tests.signal_score_smoke -v  # profile-health scoring, needs numpy
```

Tests cover every adapter's parsing (notes, TikTok, YouTube, Instagram, X/Twitter,
Spotify, Reddit, Amazon, device usage), auto-detect routing, combined-session imports,
SQLite WAL reads, local HTTP import/clear flows, cross-origin rejection, stale analysis
results, and the profile-health scoring math. They use synthetic data and temporary
files, never your actual browser history, notes, or real export files.

The core hypothesis remains: your own exhaust, embedded and clustered, resolves
into behavioral islands you recognize. DNS controls, encrypted persistent storage,
and drift tracking are future modules. So are: scheduled/watched re-ingestion (needs
an explicit persistence design first — deliberately not built silently); screen time
as a live OS query rather than an imported file (needs a real permission/consent flow,
a first for this zero-permission app); and a labeling loop where you mark individual
events as more/less representative of you, with weights that adjust over time.
