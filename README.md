# The Saint (The_St)

A local place to explore your own words and digital footprint. Start with personal
notes and journals, then compare with browser history or a TikTok export.

## Android app

Build an offline development APK for your phone:

```sh
python android/build.py
```

The APK is written to `android/build/the-saint-debug.apk`. It supports personal
notes, TikTok exports, search, and word counts. See [Android setup](android/README.md)
for SDK requirements, USB installation, and the current mobile feature scope.

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

- **Personal notes & journal:** select `.md`, `.markdown`, or `.txt` files. Paragraphs
  become passages with their filename and section heading. YAML front matter and
  fenced code are excluded. Long paragraphs split into 120-word chunks. Notes are
  limited to 1 MB each, 500 selected files, 8 MB per request, and 5,000 passages.
- **TikTok:** select JSON export files. Expressed text, watch timestamps, and assigned
  interest labels remain separate. Watch links are never fetched. Schema matching
  is heuristic; missing streams may need an adapter adjustment for your export.
- **Firefox / Chrome:** clicking Import reads the first detected local profile.
  SQLite's backup API reads committed WAL data into a temporary copy, which is
  removed after ingestion. Close the browser and retry if it is busy.
- **Sample journal:** synthetic prose for exploring the interface. Its word counts
  are real counts; it does not pretend to be a precomputed semantic analysis.

Each import replaces the current session. Search and **Show more passages** let
you explore the source text. Word counts are lexical counts, not inferred themes.

To inspect a whole notes folder from the command line (including Obsidian Markdown):

```sh
python notes.py /path/to/notes
python tiktok.py /path/to/export --inspect
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
```

Tests cover note parsing, TikTok parsing, SQLite WAL reads, local HTTP import/clear
flows, cross-origin rejection, and stale analysis results. They use synthetic data
and temporary files, never your actual browser history or notes.

The core hypothesis remains: your own exhaust, embedded and clustered, resolves
into behavioral islands you recognize. DNS controls, encrypted persistent storage,
and drift tracking are future modules.
