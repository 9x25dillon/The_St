# The Saint — Session hand-off

Updated: 2026-09-14. This replaces the 2026-09-11 hand-off.

## What today's session was

A feature-expansion session on top of the released notes+TikTok+Android app: six new
ingestion adapters (YouTube, Instagram, X/Twitter, Spotify, Reddit, Amazon, plus a
device screen-time contract — seven total, all working on both desktop and Android),
session-combining imports (each import now adds to the current session instead of
replacing it), filename-based auto-detect (a default "auto" source that routes to the
right adapter and falls back to asking the user when ambiguous), auto-run semantic
analysis once enough passages exist, and a local "Profile Health" scoring layer
(signal-to-noise, homogenization, a combined health score, and a per-passage
signal/noise tag) built on top of the existing UMAP/HDBSCAN cluster output.

The request was broad and open-ended ("add more data ingestion pipelines and slightly
automate the user interface... whatever the digital diet of the user's device that can
affect the algorithms"), including a request for "a mathematical typescript engine"
that turned out to mean a personal scoring framework the user had already drafted
(an "Injection Weight Score" combining anchor strength, divergence penalty, recency
decay, and noise exposure) — implemented here as `signal_score.py`, with plain-language
UI labels ("signal" / "flagged as noise" / "profile health") since this app never
sends anything to any platform; the framework's own "injection" language would
misleadingly imply otherwise if it leaked into user-facing text.

Given the size of the request, a plan was written and approved before implementation
(`/home/kill/.claude/plans/iterative-watching-volcano.md`). Everything in that plan
was implemented and verified, including on the physical Pixel 10a. Two things were
explicitly scoped OUT and NOT built, per the project's own stated policy that new
persistence or permission surfaces need explicit design, not silent addition:

- Scheduled/watched re-ingestion (a folder watcher or Android background job) — needs
  its own encryption/retention/deletion design first.
- Screen-time as a live OS query — `PACKAGE_USAGE_STATS` needs a real consent/
  Settings-deep-link flow, a first for this zero-permission app. Screen time instead
  ships as an imported `usage.json` file (an invented, documented contract — there's
  no universal screen-time export standard to match against).
- The scoring framework's declared-weight learning/feedback loop (labeling events
  +1/-1, with weights that adjust over time) — needs a labeling UI and persisted
  weights. Every event uses a uniform weight for now.
- Cross-session Drift tracking (Δ) — needs cluster centroids persisted across app
  restarts, which this app deliberately doesn't do.

## Product intent and boundaries

The Saint helps users explore their own words and digital footprint. Its central
hypothesis is that their data can resolve into recognizable behavioral themes.

- Keep processing local; read only sources the user selects or explicitly imports.
  Every new adapter added this session is still a local file parser — no adapter
  calls any network API or platform.
- Preserve provenance: show which file/heading or source a passage came from.
- Separate expressed text, served-video timestamps, and platform-assigned labels,
  where a source has that distinction (TikTok, YouTube, Instagram, X all do; Spotify,
  Reddit, Amazon, and device usage are single-stream sources with no assigned-label
  equivalent — documented per-adapter rather than invented).
- Platform labels are an inference proxy, not access to a platform's internal weights.
- Distance on a 2D plot does NOT prove an inference is wrong.
- Desktop clustering uses a roughly 15D UMAP projection and HDBSCAN; the 2D display
  is a separate projection. GLOSH supplies outlier scores. The new Profile Health
  scoring (`signal_score.py`) runs on the SAME 15D vectors, never the 2D picture,
  for the same reason.
- Android currently has word counts and search, not semantic clustering or Profile
  Health scoring. Do not describe lexical counts as inferred themes or claim
  desktop/mobile feature parity on the scoring/clustering front — ingestion breadth
  IS now at parity (every adapter except Firefox/Chrome works on both platforms).
- The app has no persistent personal-data store. Sessions still live in process
  memory only; imports now MERGE into that in-memory session instead of replacing
  it, but nothing is written to disk.
- "Injection" terminology from the user's own scoring framework stays internal to
  code/tests/comments. User-facing UI text says "signal" / "noise" / "profile
  health" — this app never feeds anything back to a platform, and the stronger
  language would misleadingly imply it does.
- DNS controls, egress inspection, and node-graph views remain future ideas, not
  built or newly authorized this session.

## Repository and publication

- Workspace: `/home/kill/The_St`
- Repository: https://github.com/9x25dillon/The_St
- Branch: `main`
- Last published source commit (prior session): `8432188a78d9fa42c8afeca80b55269290d286d4`
- Last GitHub prerelease (prior session): https://github.com/9x25dillon/The_St/releases/tag/v0.1.0
- **This session's work is committed locally but not yet pushed or released** — check
  `git status`/`git log` at the start of the next session; do not assume it matches
  the state described here without verifying. If a new Android release is warranted,
  bump `versionCode`/`versionName` in `android/AndroidManifest.xml` first (still at
  `0.1.0`/`1` as of this writing) and publish against a full commit SHA, per the
  prior session's lesson (an abbreviated SHA was rejected once).

## Implementation map

New/changed this session (see the prior hand-off, or `git log`, for what existed
before):

| Location | Purpose |
| --- | --- |
| `mirror.py` | `Record` gained a `when: float \| None` field; added shared `parse_timestamp()` (ISO-ish date -> epoch seconds), used by every new adapter and by `tiktok.py` (re-exported as `_parse_date` for backward compatibility) |
| `youtube.py`, `instagram.py`, `x.py`, `spotify.py`, `reddit.py`, `amazon.py`, `usage.py` | New adapters, each with a `load(path)` for CLI/folder use and a `load_blobs`/`load_rows`/`load_files` for the web/Android upload path. YouTube and Spotify use direct field parsing (stable schemas); Instagram and X use a heuristic recursive-key scanner like `tiktok.py`'s (drift-prone schemas); Reddit and Amazon parse CSV via header-shape classification, not filename |
| `signal_score.py` | New local scoring module: `anchor_strength`, `divergence_penalty`, `recency_decay`, `noise_exposure`, `homogenization_index`, `injection_weight` (IWS), `signal_to_noise`, `profile_health`. Numpy imported lazily per-function, matching `mirror.py`'s convention, so importing the module doesn't require the optional semantic dependencies |
| `app.py` | New `detect_source()` (filename-pattern auto-detect), `merge_snapshot()` (combine-not-replace on `/api/import`), `word_terms()` extracted as a reusable helper, dispatch branches for all 6 new sources, `semantic_map()` now computes and attaches Profile Health fields |
| `web/index.html`, `web/app.js` | New "auto" default source option + 6 new adapter options; `SOURCES`/`SOURCE_NAMES` config tables in `app.js` drive accept-pattern/label per source; a self-gating `maybeAutoAnalyze()` (deliberately NOT routed through the shared `action()`/`busy` mutex — see pitfall below); a "sources in this session" chip list; a Profile Health metrics panel; per-passage `signal`/`flagged as noise` tag |
| `android/src/local/thesaint/app/SaintData.java` | Hand-ported Java equivalents of every new adapter (including a from-scratch minimal RFC4180-ish CSV parser, since Java has none built in and no third-party libs are allowed), `detectSource()`, `mergeSnapshot()`, all matching the Python adapters' behavior — verified via `tests/android_smoke.mjs` on the physical device, not just by inspection |
| `tests/test_youtube.py`, `test_instagram.py`, `test_x.py`, `test_spotify.py`, `test_reddit.py`, `test_amazon.py`, `test_usage.py` | New adapter unit tests, same shape as the existing `test_tiktok.py`/`test_notes.py` |
| `tests/signal_score_smoke.py` | New scoring-module tests. Deliberately NOT named `test_*.py` — it needs numpy, and the base `python -m unittest discover -s tests` suite must stay dependency-free. Run via `.venv/bin/python -m unittest tests.signal_score_smoke -v` |
| `tests/test_app.py` | New tests for combined-session imports, auto-detect routing (including an ambiguity-collision fix — see pitfalls), and all 6 new sources wired through `import_data()` |
| `tests/browser_smoke.mjs`, `tests/android_smoke.mjs` | Updated for combined-session passage counts, auto-detect status text, the sources-row, and (Android) bridge-level smoke cases for every new adapter plus the new `#source option` count (10) |

## Pitfalls hit and fixed this session (read before touching auto-analyze or auto-detect)

1. **Auto-analyze must NOT go through the shared `action()`/`busy` mutex.** First
   attempt wrapped it in `action(...)` like the manual button. That disables every
   button for the call's duration — fine for an explicit click, wrong for an invisible
   background trigger — and worse, a 409 ("analysis already running", which happens
   naturally when a new import lands while a PREVIOUS import's auto-triggered analysis
   is still finishing) left `state.job.status` stale at `'idle'` client-side, which
   caused an immediate synchronous re-trigger loop that starved out the Clear button
   entirely (confirmed via `browser_smoke.mjs` timing out on a `#clear` click that
   silently no-op'd because the button was disabled). Fixed with a dedicated
   `maybeAutoAnalyze()` using its own `autoAnalyzing` gate flag, not `busy`, and a
   quiet 2-second backoff on failure instead of an immediate retry.
2. **Auto-detect filename collision:** YouTube's `search-history.json` and X's
   `search-history.js` both match a naive `search-history` substring pattern. The
   YouTube pattern needed a `\.json$` anchor (both in `app.py` and the hand-ported
   Java table in `SaintData.java`) so X's `.js` files aren't misrouted. Caught by a
   new data-driven `test_auto_detect_resolves_each_new_source_filename` test in
   `test_app.py` — if you add another filename-based source, extend that test.
3. **`anchor_strength`'s cluster "spread" must be RMS distance from centroid, not
   `std()` of those distances.** The user's original formula said "variance of the
   cluster" for σ_k, which is ambiguous; using `std(distances)` (the spread of the
   spread) made even ordinary in-cluster points score near zero. Verified empirically
   (see `signal_score.py`'s `_cluster_stats` docstring) before locking in
   `sqrt(mean(distance**2))` instead — check that reasoning before changing this.
4. **`homogenization_index` must be based on cluster-size dominance
   (`largest_cluster_size / total_non_noise`), not a nearest-neighbor-distance ratio.**
   A first attempt using `nn_distance / avg_pairwise_distance` got the sign backwards:
   it scored well-separated, healthy multi-cluster data as MORE homogenized than a
   single undifferentiated blob (verified with a quick numpy experiment before
   shipping). The label-only version is simpler, correctly signed, and needs no
   geometry at all.

## Environment and commands

Known available at session end (same as prior session, reconfirmed working):

- Python 3.14 (base, no packages) and the project `.venv` (numpy, umap-learn,
  hdbscan, sentence-transformers, plotly, CPU torch).
- Android SDK at `/home/kill/Android/Sdk`; JDK 17; `adb`, Node, Chromium, `gh`.
- `all-MiniLM-L6-v2` cached and used offline successfully again this session.
- Pixel 10a connected via USB debugging throughout this session (serial
  `5C091JEA325346`); used for real on-device verification of every new adapter, not
  just the build.

Desktop startup, base test suite, and optional-dependency tests: see README.md's
"Verify" section (updated this session with the new `signal_score_smoke.py` line).

Android build/install/smoke: same commands as the prior hand-off documents,
unchanged. `android/build/development.keystore` is unchanged; still reused, still
gitignored, still never to be regenerated casually (would break update capability
for the installed app).

## Evidence from this session

- All 49 desktop Python tests passed (`python -m unittest discover -s tests -v`),
  up from 14 at the start of the session (7 new adapter test files, plus new
  `test_app.py` coverage for combining/auto-detect).
- `.venv/bin/python -m unittest tests.signal_score_smoke -v`: 13/13 passed.
- `.venv/bin/python tests/semantic_smoke.py` passed with new assertions on the
  `health`/`iws`/`signal` fields, offline, using the already-cached model.
- `node tests/browser_smoke.mjs` passed against a real headless Chromium run,
  covering the new auto-detect flow (through a real `File` object, not just a
  synthetic dict), the combined-session passage count, and the sources-row.
- `python android/build.py` compiled and signed cleanly; APK grew from 29,330 bytes
  (prior release) to 37,522 bytes.
- `node tests/android_smoke.mjs` passed TWICE against the real, physical Pixel
  10a over the WebView debug bridge — once after the YouTube vertical slice alone,
  once again after all 6 adapters landed — covering every new adapter via direct
  bridge calls (bypassing the file picker, same pattern as the existing TikTok
  bridge check) plus the real-`File`-object auto-detect path through the actual UI.
- Real personal exports (an actual TikTok/Instagram/Spotify/etc. download) were NOT
  used to validate any new adapter against real-world schema quirks — every adapter
  was built and tested against synthetic fixtures modeled on documented/recalled
  export shapes. Each adapter's docstring says so and invites "widen the matchers"
  tuning against a real export, same as the existing `tiktok.py` already did.

Repeat only the checks relevant to new changes:

```sh
python -m unittest discover -s tests -v
node --check web/app.js
node tests/browser_smoke.mjs
.venv/bin/python tests/semantic_smoke.py
.venv/bin/python -m unittest tests.signal_score_smoke -v
python android/build.py
```

Do not assume old dev servers on ports 8765/8766 are still running or current.
`browser_smoke.mjs`/`android_smoke.mjs` were both hit by an unrelated,
pre-existing environment flake (a headless-Chromium cold-start "Failed to fetch" on
the very first navigation, and a separate `ENOTEMPTY` profile-cleanup race) —
neither is caused by this session's changes; both self-resolved on retry every time
they were hit. Don't chase them if seen again; just retry once or twice.

## Next-session instructions

1. Read this file, both READMEs, and check actual `git status`/`git log` before
   assuming any of the above is pushed or released.
2. The four explicitly-deferred items above (scheduled re-ingestion, live
   usage-permission, the declared-weight feedback loop, cross-session drift) are
   NOT authorized work — each needs its own design conversation (persistence design
   for the first two in particular, per the project's standing policy) before
   building, not a silent follow-on.
3. If real export samples become available (the user's actual TikTok/Instagram/
   Spotify/Reddit/Amazon data), running each adapter's `--inspect` CLI against them
   would be the highest-value next verification step — every adapter's docstring
   already flags that this hasn't been done yet.
4. Before any new Android release: bump `versionCode`/`versionName`, verify the
   exact APK/signature, and publish against a full commit SHA (abbreviated SHAs
   have been rejected by the release command before).
5. Keep this hand-off current: state what's verified vs. only proposed, and whether
   new work has been committed/pushed/published.

## Vocabulary

- Scope: the boundary of the work. This session's scope was ingestion breadth,
  session automation, and local scoring — explicitly NOT persistence or new
  permission surfaces, per the project's own policy on those two topics.
- Provenance: where something came from. Every new adapter preserves it (filename/
  detail field), same as every existing one.
