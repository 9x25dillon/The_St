## Implementation update

The repo now includes `notes.py` for personal Markdown/plain-text journals,
`app.py` plus `web/` for the loopback-only in-memory interface, and `run.py`
for one-command startup. `setup_local.py` installs optional CPU-based semantic
analysis. See README.md for current run instructions and privacy boundaries.
Browser imports now use SQLite backup, and parser/HTTP/browser smoke tests live
in `tests/`. The historical session notes below describe the original prototype;
their immediate actions and earlier distance-based interpretation are superseded
by the README and current implementation.

# The Saint (The_St) — Session Hand-off

## What this is

**The Saint** is a local-first tool for reading your own digital exhaust. It ingests your
data exports (browser history, TikTok, whatever you point it at), embeds and clusters them
into the behavioral islands you'd recognize as yourself, then drops the platforms' own
inferences into the same space. Where their labels drift away from anything you actually
searched, that distance is the algorithm's image of you reaching past the truth. Everything
runs on your machine and nothing leaves it.

Working doc title was "The Algorithmic Mirror." Renamed to **The Saint** this session.

## Where we landed today

A totalizing vision — a mirror of the algorithm's own parameters, then a self-building OS,
then a quantum stack replacing all software across every device — was scoped down, turn by
turn, into something buildable. The scope-down was the whole win. What shipped is a
local-first repo with a verified parser, a browser ingest path, a TikTok adapter, and a
divergence view. It is real and runnable, not a diagram.

## Current state of the repo

- `mirror.py` — module one. Reads browser history (Firefox or Chrome), embeds titles and
  search queries, clusters, renders an interactive 2D scatter (`mirror.html`). Has a
  `--demo` mode that proves the machinery on synthetic data with no browser needed.
  Verified: demo run produced five clean islands.
- `tiktok.py` — TikTok export adapter with the expressed-vs-assigned divergence view.
  Schema-tolerant field-finder (no hard-coded paths). Verified: parser correctly pulled
  searches, hashtags, comments, watch-history timestamps, and ad-interest categories from a
  synthetic export. The visual render reuses module one's embedding and plotting path
  (proven), just not yet run on real text.
- `README.md` — states the hypothesis as the success metric.
- `requirements.txt` — sentence-transformers, umap-learn, hdbscan, plotly, numpy.
- `mirror.html` — the demo output (five separated blobs, hover to read).

## Locked design decisions (do not relitigate)

- **Self-mirror, not corporate-mirror.** No platform exposes its internal weights. The tool
  reflects you from your own data, and uses the platform's own inferred labels (ad-interest
  categories) as an honest inference proxy of their output — never a claim to their internals.
- **Four interception points**, all classical hardware you already own and can see into: a
  DNS sink, an egress inspector, the browser, and this local store. The Saint is module one.
  The OS is the accretion of modules, never the starting point.
- **Cluster in mid-dimensional UMAP (~15D), display in a separate 2D UMAP.** Never cluster on
  the 2D projection — that manufactures groups that are only projection artifacts.
- **Outliers from HDBSCAN's GLOSH score.** One principled detector, not three overlapping ones.
- **Local-only.** Output HTML embeds its own plotting library — no CDN call, nothing egresses.
  The privacy tool must not become a privacy hole. Encrypt the store at rest; it is the
  single highest-value target once it aggregates your life.
- **Divergence view.** Embed expressed signals and the platform's assigned categories in one
  space. Categories that land far from your clusters are the algorithm's reach past your
  stated intent.
- **Adaptation on observation, policy by hand.** Let it re-cluster freely; keep what it blocks
  or changes human-set. Self-reconfiguring behavior is an undebuggable drift machine.

## TikTok specifics

- **Export route:** Settings and privacy, then Account, then Download your data. Choose JSON.
  Select all. Request.
- **Latency (act first):** takes hours to about four days to prepare, and the download link
  expires about four days after it is ready. Fire the request before touching code.
- **Three data types, not equivalent:**
  - Expressed text (search terms, hashtags, comments) — intent you typed. Embeddable.
  - Served links (watch history) — bare video URLs plus timestamps, no captions. A volume and
    timeline signal only. Resolving URLs to get captions is slow, rate-limited, ToS-gray —
    skip for v1.
  - Assigned labels (ad-interest categories, usually 20 to 60) — TikTok's inferences about
    you. Their output, not their weights.

## Immediate next actions

1. Request the TikTok export now (latency is the only live blocker).
2. When it lands: `python tiktok.py ./your_export`
3. If searches or hashtags come back empty, widen the regex matchers at the top of
   `tiktok.py` — key names drift across export versions, and your real file is ground truth.
4. Look at which gold stars float away from your dot clusters. Those are TikTok's inferences
   your actual searches never earned.

## Open TODOs

- README and repo header still say "The Algorithmic Mirror" — rename to The Saint.
- Set the repo "About" or subtitle. Candidate one-liner: "A local-first lens on your own data
  that maps who you are against who the algorithm decided you are."
- Tune `min_cluster_size` against real data: many tiny islands means raise it; one giant blob
  means lower it. That tuning is part of the understanding, not a nuisance.
- Deferred: watch-history semantic enrichment (needs URL resolution).
- Future modules: DNS sink, egress inspector, drift tracking, a node-graph view (v2), ambient
  view (v3). Consider post-quantum encryption for the at-rest store when it grows.

## How to run

    pip install -r requirements.txt
    python mirror.py --demo                 # proves the machinery, no data needed
    python mirror.py --browser firefox      # or: --browser chrome
    python tiktok.py ./your_export          # once the export lands

## How to prompt next session (saves cycles)

- **Scaffold vs spec.** Open a build request with the verb. "Scaffold X" means build the
  runnable files. "Spec X" means design it on paper, no code. Removes the build-or-discuss guess.
- **Ask for the falsifiable version.** For any big idea, "give me the falsifiable version"
  gets you a testable claim with a clear failure condition instead of grand framing.
- **Keep things orthogonal.** One word to request separation of concerns — for example, the
  clean introspection path must stay orthogonal to any decoy or obfuscation path, never mixed.
- **Front-load constraints.** State the operative facts up front (solo build, local-first,
  what you have already minimized) so they steer the work from the first turn.

## The core hypothesis under test

    My own exhaust, embedded and clustered, resolves into behavioral islands
    I actually recognize.

If it holds, the larger system earns its keep. If it does not, that was learned cheaply,
before building a node graph or a DNS layer. Understanding first, cathedral later.
