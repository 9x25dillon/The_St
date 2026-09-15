# The Saint — Session hand-off

Updated: 2026-09-15. This replaces the 2026-09-14 hand-off.

## What today's session was

Three pieces of work, chosen by the user from a short list of options after an
orientation pass:

1. **Duplicate-import fix.** Since imports started merging into the session, importing the
   same export twice doubled every passage, label, and watch count (desktop deduped
   nothing; Android deduped labels only, so the platforms also disagreed). Now records and
   watch timestamps merge as multisets, and an import that brings nothing new leaves the
   session untouched, including an already-built semantic map.
2. **Explore filters.** Clickable chips above the passage list filter by source (both
   platforms) and, once a map exists, by island and by signal/noise (desktop). Counts are
   faceted (each chip counts what you'd see if you picked it), and the canvas fades points
   that don't match the search and filters.
3. **Real-export hardening.** No real exports were on disk, so the user chose "research +
   request both": the adapters were checked against real published export samples, X's
   official archive README, RLEAPP's TikTok parser (tested against real exports), and the
   UChicago DSAR export-schema dataset. Several adapters turned out to be wrong on real
   data (details below). Research also surfaced a blocker the user then made a policy call
   on: real Spotify extended-history files are ~12.8 MB each, over the old 8 MB import cap.

The user picked **"per-file + keep newest"** for the size question: the page sends
selections in parts so the cap is per file (32 MB desktop, 16 MB Android), and past 5,000
passages a source keeps its most recent entries instead of the import failing.

## What the research found (and what changed because of it)

Each was confirmed against a real sample or official schema, not recalled:

| Adapter | Problem on real data | Fix |
| --- | --- | --- |
| TikTok | Watch links are `www.tiktokv.com` (missed); favorite effects/hashtags/sounds are `m.tiktok.com` (matched), so "watch timestamps" counted favorites. On two real exports the old code said 6 (truth 3) and 0 (truth 18). `AdInterestCategories` is a string, not a list, so labels were never captured. | Path-aware walk: only Watch History / Video Browsing History entries count; `tiktokv?` domain; labels from `AdInterestCategories` split on `\|`, `,` or newline; Settings "Interests" (self-chosen) excluded; captions on your own posts captured |
| X | `personalization.js` interests are `{name, isDisabled}` objects (0 labels captured); `like.js` `fullText` was filed as your own posts; tweet dates left blank; `search-history.js` doesn't exist | Stream-aware walk (`window.YTD.<stream>`): `like` source, `repost` for "RT @", `saved-search.js` queries, interest objects + inferred `shows`, classic date format parsed. On a real 515-tweet archive: 515/515 dated (was 0); a real personalization file gives 250 labels (was 0) |
| YouTube | "From Google Ads" entries counted as watches (4 of 7 in a real sample) | `ad` source; bare-URL titles skipped; HTML Takeout refused with instructions |
| Spotify | Account-data podcasts (`podcastName`/`episodeName`) dropped; `SearchQueries.json` and `Inferences.json` (assigned labels — the README wrongly said none exist) unread; 54% of real plays are <30 s skips | All read; plays <30 s dropped; audiobooks; `load_blobs` now returns `(records, categories)` |
| Instagram | `string_map_data` field names are localized ("Nome"), so non-English topics vanished; `recommended_topics.json` not auto-detected; profile searches pulled in other people's usernames | Container-key classification (`topics_*`, `searches_*`), English field names only as fallback; `searches_user` skipped; Meta Latin-1 mojibake repaired per value |
| Amazon | Newer `Your Amazon Orders/Order History.csv` not auto-detected | Pattern added (columns unchanged) |
| Reddit | Headers matched the schema already | `[deleted]`/`[removed]` dropped; YouTube Takeout's `comments.csv` (same name) refused clearly instead of silently yielding nothing |

**Still unverified:** TikTok's ad-interest separator (every real sample had the field
empty); Meta mojibake (all samples seen were ASCII — the repair is the well-known
Messenger-export fix); Spotify's `searchTime` "[UTC]" suffix (stripped defensively).

## Product intent and boundaries (unchanged unless noted)

- Keep processing local; read only sources the user selects or explicitly imports. No
  adapter calls any network API. The research this session used public GitHub data from
  the development machine only; nothing in the app changed network behavior.
- Preserve provenance; separate expressed text, served content, and platform-assigned
  labels. New this session: served content now has more kinds (`ad`, `like`, `repost`),
  kept apart rather than mixed with what the user wrote.
- The app has no persistent personal-data store. Sessions live in process memory.
- **New:** a session holds 5,000 passages; past that, a source's passages (already
  present and incoming together) are trimmed to the newest, so multi-file exports land on
  the same result in any file order. Other sources are never evicted. Trade-off the user
  should know: once a source overflows, its searches compete with its plays on recency.
- "Injection" terminology stays internal; UI says signal / noise / profile health.
- Still deferred and NOT authorized without a design conversation: scheduled/watched
  re-ingestion, live screen-time permission, the declared-weight labeling loop,
  cross-session drift.

## Repository and publication

- Workspace: `/home/kill/The_St`, branch `main`.
- At the start of this session `main` was 1 commit ahead of `origin/main` (the
  2026-09-14 hand-off commit `e7814a0` had never been pushed); it went up with this
  session's push.
- **This session's work is committed and pushed**: `684e35e` ("Harden adapters against
  real exports, dedupe imports, add explore filters") on `origin/main`, followed by a
  hand-off update commit. Verify with `git log --oneline -3` rather than trusting this
  file.
- The installed APK on the Pixel is this session's build (41,618 bytes, still
  `versionName 0.1.0` / `versionCode 1`). The GitHub `v0.1.0` release is further behind.

## Implementation map (this session)

| Location | Change |
| --- | --- |
| `app.py` | `merge_snapshot` returns `(snapshot, added, skipped, dropped)`; multiset dedupe via `_not_already_present`; `_record_key` (case-insensitive text; date only for `usage`, see `DATED_IDENTITY_SOURCES`); `_newest` trimming; `watch_times` kept server-side (`PRIVATE_SNAPSHOT_KEYS`); records tagged with `origin`; `/api/import` accepts `names` for whole-selection auto-detect and reports `changed/added/skipped/dropped`; `MAX_REQUEST` 64 MB, `MAX_IMPORT_RECORDS` 100k; new filename patterns |
| `tiktok.py`, `x.py`, `youtube.py`, `spotify.py`, `instagram.py`, `reddit.py`, `amazon.py` | Hardening in the table above; docstrings say what was checked against what |
| `web/app.js` | Filter chips (`renderFilters`, faceted counts, map dimming via `visible`); per-part import loop (`parts`, `MAX_FILE_BYTES`); `importSummary`; `notice` keeps the last import's outcome in front of later analysis progress messages |
| `web/index.html`, `web/style.css` | `#filters`, `#showing`, `#file-help`; chip styles with island color swatches |
| `android/src/local/thesaint/app/SaintData.java` | Full port of all of the above (path-aware walkers, `Merge` result, `newest`, `fixMetaText`, `OutOfMemoryError` caught, 24M-char payload cap) |
| `tests/parity_cases.json` + `tests/test_parity.py` | **New.** 11 shared parsing cases shaped like verified real exports. Python runs them in the base suite; `tests/android_smoke.mjs` runs the same file through the native bridge. Add a case here whenever an adapter changes, so the Java port can't silently drift |
| `tests/test_*.py`, `tests/browser_smoke.mjs`, `tests/android_smoke.mjs` | Fixtures moved from invented shapes to real ones; new coverage for dedupe, trimming order-independence, whole-selection detection, filters |

## Pitfalls hit this session

1. **Keep-newest must trim across the source, not just the incoming part.** The first
   version trimmed each import on its own; with a selection sent in parts, the first
   (older) file filled the session and every newer file was dropped — the opposite of
   "keep newest". Caught by a real-size in-browser test, not by unit tests.
2. **Per-file parts change what "duplicate" means.** With `when` in the dedupe key, the
   same song in two Spotify files (different play times) became two passages. The key now
   ignores `when` except for `usage`, matching what every adapter already does within one
   import.
3. **Status messages get overwritten by the analysis poll** every 1.5 s, which hid the
   "kept the most recent" notice. Fixed with `notice`.
4. **Python keeps fractional seconds, Java whole seconds.** The parity file caught it;
   parity compares whole seconds. Nothing downstream needs sub-second precision.
5. **Environment:** the Bash tool's shell is zsh (despite fish being the login shell): no
   fish `for ... end` syntax, and never assign a variable named `path` (it clobbers `PATH`).
   The headless-Chromium "Failed to fetch" cold-start flake and `ENOTEMPTY` profile-cleanup
   race from the prior hand-off both recurred; retry once.

## Evidence from this session

- `python -m unittest discover -s tests`: 66 passed (was 49).
- `.venv/bin/python tests/semantic_smoke.py` passed offline; `tests.signal_score_smoke` 13/13.
- `node tests/browser_smoke.mjs` passed (adds source filter + duplicate import checks).
- Headless-browser semantic map with filters: 6 islands, island/source chips and faceted
  counts correct, map dimming correct, no horizontal scroll at 390 px (screenshots
  reviewed, not committed).
- Real-size desktop import through the real file input: two generated 12.8 MB Spotify
  files (real field names, 16,300 plays each) + `SearchQueries.json` imported in ~1 s;
  kept 5,000 passages spanning the newest dates; re-import reported nothing added.
- **On the physical Pixel 10a** (serial `5C091JEA325346`): `node tests/android_smoke.mjs`
  passed, including all 11 shared cases through the Java adapter, dedupe, trimming, and the
  filter chip; a 12.8 MB Spotify file imported through the real file input in 1.2 s with
  no memory error and the app still running.
- Every adapter's `--inspect` CLI ran against the shared cases.
- Still NOT done: running any adapter against the user's own real exports.

## Next-session instructions

1. Check `git status` / `git log` first rather than trusting this file.
2. **The user was asked to request their own exports** (TikTok, Google Takeout/YouTube as
   JSON, Instagram JSON, X archive, Spotify extended history + account data, Reddit,
   Amazon). When they arrive, run each `python <adapter>.py <path> --inspect` and compare
   counts with what the user expects; that is the remaining verification step. Settle the
   TikTok ad-interest separator from a non-empty real file.
3. Deferred-but-noticed (not built, not authorized): YouTube Takeout `comments.csv`
   ("Comment Text" format unverified), Instagram post comments (localized field names make
   the container ambiguous), X note tweets and Grok chats, TikTok DMs (deliberately never).
4. The four long-standing deferred items still need their own design conversations.
5. Before any Android release: bump `versionCode`/`versionName`, publish against a full SHA.

## Session retrospective

Three opportunities for the assistant:

1. **Think through how a new batching path interacts with existing rules before
   building.** Splitting imports into per-file parts quietly changed two things that
   had worked: which passages count as duplicates (timestamps in the key) and which
   entries "keep newest" keeps (per part instead of per source). Both were foreseeable
   from the design; both were caught only by a real-size run.
2. **Prefer primary data over summaries from the start.** The first research round used
   blog summaries and web-search digests that were too vague to code against; the useful
   facts all came from real files on GitHub, an official README, and a schema dataset.
   Going there first would have saved a round.
3. **Race-proof test scripts too.** A throwaway screenshot script clicked a button while
   the app's busy lock was held and timed out; a leftover full session later broke the
   smoke test. Wait for `!busy` and clean up state in `finally`.

Three opportunities for the user, if named at the outset:

1. "Continue developing and building the app" left direction open, which took an
   orientation pass plus a choice between options. Naming a goal ("make it work on my
   real exports", "better exploration") would skip that step.
2. Real export files are the single most valuable input for this project. Requesting them
   now, even ones that take weeks (Spotify extended history, Reddit, Amazon), means they'll
   be ready for the next session.
3. Policy decisions like size limits and what to drop come up mid-task. Stating
   preferences up front ("never silently drop data", "phone memory matters more than
   completeness") lets them be applied without a pause.

Vocabulary:

- **Parity test:** one set of cases run against two implementations of the same logic
  (here Python desktop and hand-ported Java Android) so a difference shows up as a test
  failure instead of a user-visible inconsistency.
- **Faceted counts:** filter counts that reflect every other active filter, so a chip
  shows how many results picking it would actually give.

Reusable prompt:

"My exports are in [folder]. Run each adapter's --inspect against them, compare with
[what I expect], fix mismatches on both desktop and Android, add a parity case for each
fix, and verify on the phone."
