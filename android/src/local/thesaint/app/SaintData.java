package local.thesaint.app;

import org.json.*;
import java.nio.charset.StandardCharsets;
import java.util.*;
import java.util.regex.*;
import java.time.*;
import java.time.format.DateTimeFormatter;
import java.nio.ByteBuffer;
import java.nio.charset.CharacterCodingException;
import java.nio.charset.CodingErrorAction;

/** In-memory data adapter. No file, network, database, or permission access. */
public final class SaintData {
    private JSONObject state;
    private static final Set<String> STOP = new HashSet<>(Arrays.asList("the and that this with from have was were are for but not you your my our had has will into just about they them then when what some more been very can".split(" ")));
    private static final Pattern WORD = Pattern.compile("[\\p{L}]{3,}");
    private static final Pattern SEARCH = Pattern.compile("search.?term", Pattern.CASE_INSENSITIVE);
    private static final Pattern HASH = Pattern.compile("hashtag.?name|^hashtag$", Pattern.CASE_INSENSITIVE);
    private static final Pattern SOUND = Pattern.compile("sound.?name|song.?name", Pattern.CASE_INSENSITIVE);
    private static final int MAX_RECORDS = 5000;
    private static final int MAX_IMPORT_RECORDS = 100_000;
    private static final int MAX_PAYLOAD_CHARS = 24_000_000; // one file (up to 16 MB, see app.js) per request, with room for JSON escaping
    private static final Set<String> DATED_IDENTITY_SOURCES = Collections.singleton("usage");
    // TikTok, checked against real exports (see tiktok.py): only watch-history sections hold
    // served videos; likes, favorites, shares and your own posts share the {Date, Link} shape.
    private static final Pattern URL = Pattern.compile("https?://\\S*tiktokv?\\.com/\\S+", Pattern.CASE_INSENSITIVE);
    private static final Pattern TIKTOK_COMMENT = Pattern.compile("^comment(content)?$", Pattern.CASE_INSENSITIVE);
    private static final Pattern TIKTOK_WATCH_SECTION = Pattern.compile("watch.?history|video.?browsing", Pattern.CASE_INSENSITIVE);
    private static final Pattern TIKTOK_AD_INTEREST = Pattern.compile("ad.?interest", Pattern.CASE_INSENSITIVE);
    private static final Pattern TIKTOK_OWN_POSTS = Pattern.compile("^(posts?|videos?)$", Pattern.CASE_INSENSITIVE);
    private static final Pattern LABEL_SPLIT = Pattern.compile("\\s*[|,\\n]\\s*");
    private static final Pattern YT_BARE_URL = Pattern.compile("^https?://", Pattern.CASE_INSENSITIVE);
    private static final Pattern YT_AD_DETAIL = Pattern.compile("google ads", Pattern.CASE_INSENSITIVE);
    private static final Pattern IG_TOPIC_CONTAINER = Pattern.compile("^topics_", Pattern.CASE_INSENSITIVE);
    private static final Pattern IG_SEARCH_CONTAINER = Pattern.compile("^searches_", Pattern.CASE_INSENSITIVE);
    private static final Pattern IG_PEOPLE_SEARCH_CONTAINER = Pattern.compile("user|profile|account", Pattern.CASE_INSENSITIVE);
    private static final Pattern SPOTIFY_ZONE_SUFFIX = Pattern.compile("\\[[^\\]]*\\]$");
    private static final int SPOTIFY_MIN_PLAY_MS = 30_000;
    private static final DateTimeFormatter X_CLASSIC_DATE = DateTimeFormatter.ofPattern("EEE MMM dd HH:mm:ss Z yyyy", Locale.ENGLISH);
    private static final Pattern SEARCHED = Pattern.compile("^searched for\\s+", Pattern.CASE_INSENSITIVE);
    private static final Pattern WATCHED = Pattern.compile("^watched\\s+", Pattern.CASE_INSENSITIVE);
    private static final Pattern REMOVED = Pattern.compile("a video that has been removed|a video that isn.t available", Pattern.CASE_INSENSITIVE);
    private static final Pattern IG_TOPIC_FIELD = Pattern.compile("^name$", Pattern.CASE_INSENSITIVE);
    private static final Pattern IG_SEARCH_FIELD = Pattern.compile("search", Pattern.CASE_INSENSITIVE);
    private static final Pattern X_WRAPPER = Pattern.compile("^\\s*window\\.YTD\\.(\\w+)\\.part\\d+\\s*=\\s*");
    private static final Pattern X_QUERY_FIELD = Pattern.compile("^query$", Pattern.CASE_INSENSITIVE);
    private static final Pattern X_TWEET_TEXT_FIELD = Pattern.compile("^full_?text$", Pattern.CASE_INSENSITIVE);
    private static final Pattern X_INTEREST_LIST = Pattern.compile("^(interests|partnerInterests)$", Pattern.CASE_INSENSITIVE);
    private static final Pattern X_DATE_FIELD = Pattern.compile("date|time|created.?at", Pattern.CASE_INSENSITIVE);
    private static final Pattern REDDIT_TITLE_COL = Pattern.compile("title", Pattern.CASE_INSENSITIVE);
    private static final Pattern REDDIT_BODY_COL = Pattern.compile("^body$", Pattern.CASE_INSENSITIVE);
    private static final Pattern REDDIT_SUBREDDIT_COL = Pattern.compile("subreddit", Pattern.CASE_INSENSITIVE);
    private static final Pattern REDDIT_DATE_COL = Pattern.compile("^date$", Pattern.CASE_INSENSITIVE);
    private static final Pattern AMAZON_PRODUCT_COL = Pattern.compile("product.*name|^title$", Pattern.CASE_INSENSITIVE);
    private static final Pattern AMAZON_DATE_COL = Pattern.compile("order.*date|^date$", Pattern.CASE_INSENSITIVE);
    // Auto-detect: filename patterns for the standard export tools ship, checked in order,
    // mirroring app.py's _SOURCE_FILENAME_PATTERNS. Matched against a lowercased filename.
    private static final String[][] FILENAME_PATTERNS = {
        {".*\\.(md|markdown|txt)$", "notes"},
        {".*(watch-history|search-history)\\.(json|html)$", "youtube"},
        {".*(your_topics|recommended_topics|word_or_phrase_searches|ads_viewed|ads_and_topics).*", "instagram"},
        {".*(streaming_history_audio|streaminghistory).*|.*(searchqueries|inferences)\\.json$", "spotify"},
        {".*(posts\\.csv|comments\\.csv)$", "reddit"},
        {".*retail\\.orderhistory.*|.*order history\\.csv$", "amazon"},
        {".*(usage|screen.?time).*", "usage"},
        {".*(user_data\\.json|tiktok).*", "tiktok"},
        {".*\\.js$", "x"},
    };
    public SaintData() { try { state = summarize(new JSONArray(), new TreeSet<>(), new JSONArray(), new JSONArray()); } catch (JSONException error) { throw new IllegalStateException(error); } }

    public synchronized String request(String path, String payload) {
        try {
            if (payload.length() > MAX_PAYLOAD_CHARS) throw new IllegalArgumentException("Choose a smaller file (maximum 16 MB per file on Android).");
            if (path.equals("/api/state")) {
                // watch_times stays private, matching app.py's PRIVATE_SNAPSHOT_KEYS; the page only needs the count.
                List<String> names = new ArrayList<>();
                Iterator<String> keys = state.keys();
                while (keys.hasNext()) { String key = keys.next(); if (!key.equals("watch_times")) names.add(key); }
                return new JSONObject(state, names.toArray(new String[0])).toString();
            }
            if (path.equals("/api/clear")) { state = summarize(new JSONArray(), new TreeSet<>(), new JSONArray(), new JSONArray()); return "{\"ok\":true}"; }
            if (path.equals("/api/import")) {
                JSONObject input = new JSONObject(payload);
                String requested = input.optString("source");
                // The page sends large selections one file per request with every selected name
                // in "names", so auto-detect still checks the whole selection (as app.py does).
                JSONArray names = input.optJSONArray("names");
                String resolved = requested.equals("auto") ? detectSource(names != null ? names : input.optJSONArray("files")) : requested;
                input.put("source", resolved);
                JSONObject incoming = importData(input);
                Merge merge = mergeSnapshot(state, incoming, resolved);
                boolean changed = merge.snapshot != state;
                state = merge.snapshot;
                return new JSONObject().put("ok", true).put("detected_source", resolved).put("changed", changed)
                    .put("added", merge.added).put("skipped", merge.skipped).put("dropped", merge.dropped).toString();
            }
            throw new IllegalArgumentException("This action is unavailable on Android.");
        } catch (StackOverflowError error) {
            return "{\"error\":\"JSON nesting is too deep.\"}";
        } catch (OutOfMemoryError error) {
            return "{\"error\":\"This file is too large for this phone. Choose a smaller export file.\"}";
        } catch (Exception error) {
            return "{\"error\":" + JSONObject.quote(error.getMessage() == null ? "Import failed. Check the selected files." : error.getMessage()) + "}";
        }
    }

    private static String detectSource(JSONArray files) throws JSONException {
        if (files == null || files.length() == 0) throw new IllegalArgumentException("Choose one or more files to auto-detect a source.");
        Set<String> guesses = new TreeSet<>();
        for (int i = 0; i < files.length(); i++) {
            Object item = files.opt(i);
            String name = (item instanceof JSONObject ? ((JSONObject) item).optString("name", "") : item instanceof String ? (String) item : "").toLowerCase(Locale.ROOT);
            for (String[] entry : FILENAME_PATTERNS) {
                if (name.matches(entry[0])) { guesses.add(entry[1]); break; }
            }
        }
        if (guesses.size() == 1) return guesses.iterator().next();
        if (guesses.isEmpty()) throw new IllegalArgumentException("Could not detect a source from these filenames. Choose one manually from the dropdown.");
        throw new IllegalArgumentException("These files look like different sources (" + String.join(", ", guesses) + "). Import one source at a time.");
    }

    private static final class Merge {
        final JSONObject snapshot;
        final int added, skipped, dropped;
        Merge(JSONObject snapshot, int added, int skipped, int dropped) { this.snapshot = snapshot; this.added = added; this.skipped = skipped; this.dropped = dropped; }
    }

    /** Combines an import into the session, matching app.py's merge_snapshot: records and
     * watch timestamps are compared as multisets (an entry present n times absorbs up to n
     * incoming copies), so re-importing an export adds nothing and an overlapping newer
     * export adds only what's new. Past MAX_RECORDS, this source's passages (present and
     * incoming together) are trimmed to the newest; other sources are never evicted. The
     * snapshot is {@code existing} itself when nothing is new. */
    private static Merge mergeSnapshot(JSONObject existing, JSONObject incoming, String sourceName) throws JSONException {
        JSONArray existingRecords = existing.getJSONArray("records");
        Map<String,Integer> remaining = new HashMap<>();
        List<JSONObject> records = new ArrayList<>();
        for (int i = 0; i < existingRecords.length(); i++) {
            JSONObject record = existingRecords.getJSONObject(i);
            remaining.merge(recordKey(record), 1, Integer::sum);
            records.add(record);
        }
        Set<JSONObject> added = Collections.newSetFromMap(new IdentityHashMap<>());
        JSONArray incomingRecords = incoming.getJSONArray("records");
        for (int i = 0; i < incomingRecords.length(); i++) {
            JSONObject record = incomingRecords.getJSONObject(i).put("origin", sourceName);
            String key = recordKey(record);
            if (remaining.getOrDefault(key, 0) > 0) remaining.merge(key, -1, Integer::sum);
            else { records.add(record); added.add(record); }
        }
        int skipped = incomingRecords.length() - added.size();
        int dropped = 0;
        if (records.size() > MAX_RECORDS) {
            List<JSONObject> same = new ArrayList<>();
            for (JSONObject record : records) if (sourceName.equals(record.optString("origin"))) same.add(record);
            Set<JSONObject> kept = newest(same, Math.max(MAX_RECORDS - (records.size() - same.size()), 0));
            dropped = same.size() - kept.size();
            List<JSONObject> trimmed = new ArrayList<>();
            for (JSONObject record : records) if (!sourceName.equals(record.optString("origin")) || kept.contains(record)) trimmed.add(record);
            records = trimmed;
            added.retainAll(kept);
        }
        JSONArray existingWatches = existing.getJSONArray("watch_times");
        Map<Long,Integer> remainingWatches = new HashMap<>();
        for (int i = 0; i < existingWatches.length(); i++) remainingWatches.merge(existingWatches.getLong(i), 1, Integer::sum);
        JSONArray newWatches = new JSONArray();
        JSONArray incomingWatches = incoming.getJSONArray("watch_times");
        for (int i = 0; i < incomingWatches.length(); i++) {
            long time = incomingWatches.getLong(i);
            if (remainingWatches.getOrDefault(time, 0) > 0) remainingWatches.merge(time, -1, Integer::sum);
            else newWatches.put(time);
        }
        TreeSet<String> categories = new TreeSet<>();
        JSONArray existingCats = existing.getJSONArray("categories");
        for (int i = 0; i < existingCats.length(); i++) categories.add(existingCats.getString(i));
        JSONArray incomingCats = incoming.getJSONArray("categories");
        for (int i = 0; i < incomingCats.length(); i++) categories.add(incomingCats.getString(i));
        if (added.isEmpty() && newWatches.length() == 0 && categories.size() == existingCats.length()) return new Merge(existing, 0, skipped, dropped);
        JSONArray recordArray = new JSONArray();
        int count = 0;
        for (JSONObject record : records) {
            recordArray.put(record);
            if (sourceName.equals(record.optString("origin"))) count++;
        }
        JSONArray watchTimes = new JSONArray();
        for (int i = 0; i < existingWatches.length(); i++) watchTimes.put(existingWatches.get(i));
        for (int i = 0; i < newWatches.length(); i++) watchTimes.put(newWatches.get(i));
        JSONArray sources = new JSONArray();
        boolean matched = false;
        JSONArray existingSources = existing.optJSONArray("sources");
        if (existingSources != null) {
            for (int i = 0; i < existingSources.length(); i++) {
                JSONObject entry = new JSONObject(existingSources.getJSONObject(i).toString());
                if (entry.getString("source").equals(sourceName)) {
                    entry.put("count", count);
                    matched = true;
                }
                sources.put(entry);
            }
        }
        if (!matched) sources.put(new JSONObject().put("source", sourceName).put("count", count));
        return new Merge(summarize(recordArray, categories, watchTimes, sources), added.size(), skipped, dropped);
    }

    /** The {@code count} most recent records, like app.py's _newest: dated before undated,
     * newer first, and file order among equals. */
    private static Set<JSONObject> newest(List<JSONObject> records, int count) {
        List<Integer> order = new ArrayList<>();
        for (int i = 0; i < records.size(); i++) order.add(i);
        order.sort((a, b) -> {
            Object whenA = records.get(a).opt("when"), whenB = records.get(b).opt("when");
            boolean datedA = whenA instanceof Number, datedB = whenB instanceof Number;
            if (datedA != datedB) return datedA ? -1 : 1;
            int byTime = datedA ? Long.compare(((Number) whenB).longValue(), ((Number) whenA).longValue()) : 0;
            return byTime != 0 ? byTime : Integer.compare(a, b);
        });
        Set<JSONObject> kept = Collections.newSetFromMap(new IdentityHashMap<>());
        for (int i = 0; i < Math.min(count, order.size()); i++) kept.add(records.get(order.get(i)));
        return kept;
    }

    /** Matches app.py's _record_key: text compared case-insensitively, and the date only for
     * sources where it is part of an entry's identity (a usage row is one app on one day). */
    private static String recordKey(JSONObject record) {
        String origin = record.optString("origin");
        String when = DATED_IDENTITY_SOURCES.contains(origin) ? String.valueOf(record.opt("when")) : "";
        return origin + "\0" + record.optString("source") + "\0" + record.optString("text").toLowerCase(Locale.ROOT)
            + "\0" + record.optString("detail") + "\0" + when;
    }

    private static void addRecord(JSONArray records, String text, String source, String detail, Long when) throws JSONException {
        if (records.length() >= MAX_IMPORT_RECORDS) throw new IllegalArgumentException("This file has more entries than the app can read at once. Choose a smaller export file.");
        JSONObject record = new JSONObject().put("text", text).put("source", source).put("detail", detail);
        record.put("when", when == null ? JSONObject.NULL : when);
        records.put(record);
    }

    private JSONObject importData(JSONObject input) throws JSONException {
        String source = input.optString("source");
        JSONArray records = new JSONArray();
        TreeSet<String> categories = new TreeSet<>();
        JSONArray watchTimes = new JSONArray();
        if (source.equals("demo")) {
            String[][] themes = {{"Garden", "I planted tomatoes and watered the herbs. The garden feels peaceful."},
                {"Music", "I practiced piano chords and recorded a new melody."},
                {"Walking", "I walked through the woods and enjoyed the quiet trail."},
                {"Making", "I built a small wooden shelf and sketched my next project."},
                {"Friends", "I cooked dinner with friends and enjoyed our conversation."}};
            for (String[] theme : themes) for (int i=1; i<=12; i++) addRecord(records, theme[1] + " Reflection " + i + ".", "note", "Sample journal · " + theme[0], null);
        } else if (source.equals("notes") || source.equals("tiktok") || source.equals("youtube")
                || source.equals("instagram") || source.equals("spotify") || source.equals("reddit")
                || source.equals("amazon") || source.equals("usage") || source.equals("x")) {
            JSONArray files = input.optJSONArray("files");
            if (files == null || files.length() == 0 || files.length() > 500) throw new IllegalArgumentException("Choose between 1 and 500 files.");
            Set<String> seen = new HashSet<>();
            for (int i=0; i<files.length(); i++) {
                JSONObject file = files.getJSONObject(i);
                String text = file.getString("text");
                switch (source) {
                    case "notes": parseNote(file.getString("name"), text, records); break;
                    case "youtube": parseYoutube(text, records, seen); break;
                    case "instagram": parseInstagram(text, records, categories, seen); break;
                    case "spotify": parseSpotify(text, records, categories, seen); break;
                    case "reddit": parseReddit(text, records, seen); break;
                    case "amazon": parseAmazon(text, records, seen); break;
                    case "usage": parseUsage(text, records, seen); break;
                    case "x": parseX(text, records, categories, seen); break;
                    default: {
                        if (text.startsWith("﻿")) text = text.substring(1);
                        JSONTokener tokener = new JSONTokener(text);
                        Object blob = tokener.nextValue();
                        if (!(blob instanceof JSONObject) && !(blob instanceof JSONArray)) throw new IllegalArgumentException("Choose a TikTok JSON object or array.");
                        if (tokener.nextClean() != 0) throw new IllegalArgumentException("Unexpected text after JSON data.");
                        walk(blob, new ArrayList<>(), records, categories, seen, watchTimes, 0);
                    }
                }
            }
        } else throw new IllegalArgumentException("Choose personal notes or a supported export (TikTok, YouTube, Instagram, X/Twitter, Spotify, Reddit, Amazon, or device usage).");
        if (records.length() == 0 && categories.isEmpty() && watchTimes.length() == 0) throw new IllegalArgumentException("No usable entries found in the selected files.");
        return summarize(records, categories, watchTimes, new JSONArray());
    }

    private static void parseNote(String name, String text, JSONArray records) throws JSONException {
        if (!name.toLowerCase(Locale.ROOT).matches(".*\\.(md|markdown|txt)$")) throw new IllegalArgumentException("Choose Markdown (.md) or plain-text (.txt) notes.");
        if (text.getBytes(StandardCharsets.UTF_8).length > 1_000_000) throw new IllegalArgumentException(name + ": exceeds the 1 MB per-file limit.");
        if (text.indexOf('\0') >= 0) throw new IllegalArgumentException(name + ": appears to be a binary file.");
        text = text.replaceFirst("^\\x{FEFF}", "").replace("\r\n", "\n");
        text = text.replaceFirst("(?s)\\A---\\n.*?\\n---(?:\\n|$)", "");
        text = text.replaceAll("(?sm)^(`{3,}|~{3,})[^\\n]*\\n.*?^\\1[^\\n]*(?:\\n|$)", "");
        String heading = name.replaceFirst("\\.[^.]+$", "");
        for (String paragraph : text.split("\\n\\s*\\n")) {
            StringBuilder prose = new StringBuilder();
            for (String line : paragraph.split("\\n")) {
                if (line.matches("^#{1,6}\\s.*")) heading = line.replaceFirst("^#+\\s*", "").trim();
                else prose.append(line.trim()).append(' ');
            }
            String clean = prose.toString().trim();
            if (clean.isEmpty()) continue;
            String[] words = clean.split("\\s+");
            for (int start=0; start<words.length; start+=120) {
                addRecord(records, String.join(" ", Arrays.copyOfRange(words,start,Math.min(start+120,words.length))), "note", name + " · " + heading, null);
            }
        }
    }

    /** Google Takeout YouTube export: watch-history.json / search-history.json, one JSON
     * array of {title, titleUrl, time} entries. Classifies each entry by its own "Searched
     * for " / "Watched " title prefix, matching youtube.py exactly. */
    private static void parseYoutube(String text, JSONArray records, Set<String> seen) throws JSONException {
        if (text.startsWith("﻿")) text = text.substring(1);
        if (text.trim().startsWith("<")) throw new IllegalArgumentException("This is the HTML version of your YouTube history. Export it again from Google Takeout with the history format set to JSON.");
        JSONTokener tokener = new JSONTokener(text);
        Object blob = tokener.nextValue();
        JSONArray entries;
        if (blob instanceof JSONArray) entries = (JSONArray) blob;
        else if (blob instanceof JSONObject) { entries = new JSONArray(); entries.put(blob); }
        else throw new IllegalArgumentException("Choose a YouTube Takeout JSON file.");
        for (int i = 0; i < entries.length(); i++) {
            Object entryObj = entries.opt(i);
            if (!(entryObj instanceof JSONObject)) continue;
            JSONObject entry = (JSONObject) entryObj;
            String title = entry.optString("title", "");
            if (title.trim().isEmpty()) continue;
            Long when = entry.opt("time") instanceof String ? parseTimestamp(entry.getString("time")) : null;
            String detail = entry.opt("titleUrl") instanceof String ? entry.getString("titleUrl") : "youtube";
            String recordText, recordSource;
            Matcher searched = SEARCHED.matcher(title);
            if (searched.find()) {
                recordText = searched.replaceFirst("").trim();
                recordSource = "search";
            } else if (REMOVED.matcher(title).find()) {
                continue;
            } else {
                recordText = WATCHED.matcher(title).replaceFirst("").trim();
                if (YT_BARE_URL.matcher(recordText).find()) continue; // only a link, no title -- nothing to read
                boolean isAd = false; // "From Google Ads": played ads, kept apart from videos you picked
                JSONArray details = entry.optJSONArray("details");
                if (details != null) for (int d = 0; d < details.length(); d++) {
                    JSONObject detailEntry = details.optJSONObject(d);
                    if (detailEntry != null && detailEntry.opt("name") instanceof String && YT_AD_DETAIL.matcher(detailEntry.getString("name")).find()) isAd = true;
                }
                recordSource = isAd ? "ad" : "watch";
            }
            String key = recordSource + ":" + recordText.toLowerCase(Locale.ROOT);
            if (!recordText.isEmpty() && seen.add(key)) addRecord(records, recordText, recordSource, detail, when);
        }
    }

    /** Meta "Download your information" JSON export: every value wrapped in
     * {"string_map_data": {"&lt;Field&gt;": {"value": ..., "timestamp": ...}}}. Classifies by
     * field name inside that wrapper (Name -&gt; assigned topic, *search* -&gt; expressed
     * search), matching instagram.py's load_blobs exactly. */
    private static void parseInstagram(String text, JSONArray records, Set<String> categories, Set<String> seen) throws JSONException {
        if (text.startsWith("﻿")) text = text.substring(1);
        Object blob = new JSONTokener(text).nextValue();
        walkInstagram(blob, new ArrayList<>(), records, categories, seen, 0);
    }

    /** Matches instagram.py: string_map_data field names are localized ("Name", "Nome"), so the
     * stable container key (topics_* / searches_*) decides first; English field names are only
     * a fallback. Profile searches (other people's usernames) are skipped. */
    private static void walkInstagram(Object value, List<String> path, JSONArray records, Set<String> categories, Set<String> seen, int depth) throws JSONException {
        if (depth > 100) throw new IllegalArgumentException("JSON nesting is too deep.");
        if (value instanceof JSONArray) {
            JSONArray list = (JSONArray) value;
            for (int i = 0; i < list.length(); i++) walkInstagram(list.get(i), path, records, categories, seen, depth + 1);
        } else if (value instanceof JSONObject) {
            JSONObject object = (JSONObject) value;
            Object smdObj = object.opt("string_map_data");
            if (smdObj instanceof JSONObject) {
                JSONObject smd = (JSONObject) smdObj;
                Long when = null;
                List<String[]> values = new ArrayList<>();
                Iterator<String> fieldKeys = smd.keys();
                while (fieldKeys.hasNext()) {
                    String field = fieldKeys.next();
                    Object v = smd.opt(field);
                    if (!(v instanceof JSONObject)) continue;
                    Object ts = ((JSONObject) v).opt("timestamp");
                    if (ts instanceof Number && ((Number) ts).longValue() != 0) when = ((Number) ts).longValue();
                    Object valueObj = ((JSONObject) v).opt("value");
                    if (!(valueObj instanceof String)) continue;
                    String valueStr = ((String) valueObj).trim();
                    if (valueStr.isEmpty() || valueStr.equalsIgnoreCase("not_stored")) continue;
                    values.add(new String[]{field, fixMetaText(valueStr)});
                }
                String container = null;
                for (int i = path.size() - 1; i >= 0 && container == null; i--) {
                    if (IG_TOPIC_CONTAINER.matcher(path.get(i)).find() || IG_SEARCH_CONTAINER.matcher(path.get(i)).find()) container = path.get(i);
                }
                if (container != null && IG_TOPIC_CONTAINER.matcher(container).find()) {
                    for (String[] fieldValue : values) if (fieldValue[1].length() < 60) categories.add(fieldValue[1]);
                } else if (container != null) {
                    if (!IG_PEOPLE_SEARCH_CONTAINER.matcher(container).find() && !values.isEmpty()) addInstagramSearch(records, seen, values.get(0)[1], when);
                } else {
                    for (String[] fieldValue : values) {
                        if (IG_TOPIC_FIELD.matcher(fieldValue[0]).matches()) { if (fieldValue[1].length() < 60) categories.add(fieldValue[1]); }
                        else if (IG_SEARCH_FIELD.matcher(fieldValue[0]).find()) addInstagramSearch(records, seen, fieldValue[1], when);
                    }
                }
            }
            Iterator<String> keys = object.keys();
            while (keys.hasNext()) {
                String key = keys.next();
                Object item = object.get(key);
                if (item instanceof JSONObject || item instanceof JSONArray) {
                    path.add(key);
                    walkInstagram(item, path, records, categories, seen, depth + 1);
                    path.remove(path.size() - 1);
                }
            }
        }
    }

    private static void addInstagramSearch(JSONArray records, Set<String> seen, String value, Long when) throws JSONException {
        if (seen.add("search:" + value.toLowerCase(Locale.ROOT))) addRecord(records, value, "search", "instagram", when);
    }

    /** Undoes Meta's export encoding, where each UTF-8 byte was stored as a Latin-1 character;
     * matches instagram.py's fix_meta_text. Text that doesn't round-trip is left alone. */
    private static String fixMetaText(String value) {
        boolean suspicious = false;
        for (int i = 0; i < value.length(); i++) {
            char c = value.charAt(i);
            if (c > 0xFF) return value;
            if (c >= 0x80) suspicious = true;
        }
        if (!suspicious) return value;
        try {
            return StandardCharsets.UTF_8.newDecoder().onMalformedInput(CodingErrorAction.REPORT).onUnmappableCharacter(CodingErrorAction.REPORT)
                .decode(ByteBuffer.wrap(value.getBytes(StandardCharsets.ISO_8859_1))).toString();
        } catch (CharacterCodingException error) {
            return value;
        }
    }

    /** Spotify extended streaming history and account data, matching spotify.py's load_blobs:
     * each entry is classified by the fields it carries (searchQuery; track/artist; episode/
     * show, including account data's episodeName/podcastName; audiobook), Inferences.json
     * becomes assigned labels, and plays under 30 seconds are dropped as skips. */
    private static void parseSpotify(String text, JSONArray records, Set<String> categories, Set<String> seen) throws JSONException {
        if (text.startsWith("﻿")) text = text.substring(1);
        Object blob = new JSONTokener(text).nextValue();
        if (blob instanceof JSONObject && ((JSONObject) blob).opt("inferences") instanceof JSONArray) {
            JSONArray inferences = ((JSONObject) blob).getJSONArray("inferences");
            for (int i = 0; i < inferences.length(); i++) {
                if (!(inferences.opt(i) instanceof String)) continue;
                String label = inferences.getString(i).replace('_', ' ').trim();
                if (!label.isEmpty() && label.length() < 100) categories.add(label);
            }
            return;
        }
        JSONArray entries;
        if (blob instanceof JSONArray) entries = (JSONArray) blob;
        else if (blob instanceof JSONObject) { entries = new JSONArray(); entries.put(blob); }
        else throw new IllegalArgumentException("Choose a Spotify streaming history JSON file.");
        for (int i = 0; i < entries.length(); i++) {
            Object entryObj = entries.opt(i);
            if (!(entryObj instanceof JSONObject)) continue;
            JSONObject entry = (JSONObject) entryObj;
            if (entry.has("searchQuery")) {
                String query = textField(entry, "searchQuery");
                if (query != null) addSpotify(records, seen, query, "search", spotifyTime(entry.opt("searchTime")));
                continue;
            }
            Object played = entry.has("ms_played") ? entry.opt("ms_played") : entry.opt("msPlayed");
            if (played instanceof Number && ((Number) played).doubleValue() < SPOTIFY_MIN_PLAY_MS) continue;
            Long when = spotifyTime(entry.has("ts") ? entry.opt("ts") : entry.opt("endTime"));
            String track = firstText(entry, "master_metadata_track_name", "trackName");
            String artist = firstText(entry, "master_metadata_album_artist_name", "artistName");
            String episode = firstText(entry, "episode_name", "episodeName");
            String show = firstText(entry, "episode_show_name", "podcastName");
            String audiobook = textField(entry, "audiobook_title");
            String chapter = textField(entry, "audiobook_chapter_title");
            if (track != null && artist != null) addSpotify(records, seen, track + " — " + artist, "track", when);
            else if (episode != null) addSpotify(records, seen, show != null ? episode + " — " + show : episode, "podcast", when);
            else if (audiobook != null) addSpotify(records, seen, chapter != null ? chapter + " — " + audiobook : audiobook, "audiobook", when);
        }
    }

    private static void addSpotify(JSONArray records, Set<String> seen, String text, String source, Long when) throws JSONException {
        if (seen.add(source + ":" + text.toLowerCase(Locale.ROOT))) addRecord(records, text, source, "spotify", when);
    }

    private static Long spotifyTime(Object value) {
        return value instanceof String ? parseTimestamp(SPOTIFY_ZONE_SUFFIX.matcher(((String) value).trim()).replaceFirst("")) : null;
    }

    private static String firstText(JSONObject obj, String key, String fallbackKey) {
        String value = textField(obj, key);
        return value != null ? value : textField(obj, fallbackKey);
    }

    private static String textField(JSONObject obj, String key) {
        Object v = obj.opt(key);
        if (v instanceof Number) v = v.toString(); // account data allows numeric track names and queries
        return (v instanceof String && !((String) v).trim().isEmpty()) ? ((String) v).trim() : null;
    }

    /** X archive export, matching x.py: strips the window.YTD.<stream>.partN wrapper, keeps the
     * stream name, and classifies by key and by the stream/keys leading to it -- liked posts
     * (like.js) stay "like", "RT @" retweets are "repost", saved-search.js queries are
     * searches, and personalization.js interest objects ({name, isDisabled}) and shows are
     * assigned labels. */
    private static void parseX(String text, JSONArray records, Set<String> categories, Set<String> seen) throws JSONException {
        String stripped = text.startsWith("﻿") ? text.substring(1) : text;
        List<String> path = new ArrayList<>();
        Matcher wrapper = X_WRAPPER.matcher(stripped);
        if (wrapper.find()) {
            path.add(wrapper.group(1));
            stripped = stripped.substring(wrapper.end()).trim();
            if (stripped.endsWith(";")) stripped = stripped.substring(0, stripped.length() - 1);
        }
        Object blob;
        try {
            blob = new JSONTokener(stripped).nextValue();
        } catch (JSONException exc) {
            throw new IllegalArgumentException("Cannot read JSON export: " + exc.getMessage());
        }
        walkX(blob, path, records, categories, seen, 0);
    }

    private static void walkX(Object value, List<String> path, JSONArray records, Set<String> categories, Set<String> seen, int depth) throws JSONException {
        if (depth > 100) throw new IllegalArgumentException("JSON nesting is too deep.");
        if (value instanceof JSONArray) {
            JSONArray list = (JSONArray) value;
            for (int i = 0; i < list.length(); i++) walkX(list.get(i), path, records, categories, seen, depth + 1);
        } else if (value instanceof JSONObject) {
            JSONObject object = (JSONObject) value;
            boolean liked = false, inInterests = false;
            for (String key : path) {
                if (key.equalsIgnoreCase("like")) liked = true;
                if (key.equalsIgnoreCase("interests")) inInterests = true;
            }
            Long nodeDate = null;
            Iterator<String> keys = object.keys();
            while (keys.hasNext()) {
                String key = keys.next();
                Object v = object.get(key);
                if (v instanceof String && X_DATE_FIELD.matcher(key).find()) {
                    Long parsed = parseXDate((String) v);
                    if (parsed != null) nodeDate = parsed;
                }
            }
            keys = object.keys();
            while (keys.hasNext()) {
                String key = keys.next(); Object item = object.get(key);
                if (item instanceof String) {
                    String text = ((String) item).trim();
                    if (X_TWEET_TEXT_FIELD.matcher(key).matches()) {
                        addXExpressed(records, seen, text, liked ? "like" : text.startsWith("RT @") ? "repost" : "post", nodeDate);
                    } else if (X_QUERY_FIELD.matcher(key).matches()) {
                        addXExpressed(records, seen, text, "search", nodeDate);
                    }
                } else if (item instanceof JSONArray && (X_INTEREST_LIST.matcher(key).matches() || (key.equals("shows") && inInterests))) {
                    JSONArray list = (JSONArray) item;
                    for (int i = 0; i < list.length(); i++) {
                        Object entry = list.get(i);
                        if (entry instanceof JSONObject) {
                            if (Boolean.TRUE.equals(((JSONObject) entry).opt("isDisabled"))) continue;
                            entry = ((JSONObject) entry).opt("name");
                        }
                        if (entry instanceof String) {
                            String label = ((String) entry).trim();
                            if (!label.isEmpty() && label.length() < 60) categories.add(label);
                        }
                    }
                }
                if (item instanceof JSONObject || item instanceof JSONArray) {
                    path.add(key);
                    walkX(item, path, records, categories, seen, depth + 1);
                    path.remove(path.size() - 1);
                }
            }
        }
    }

    private static Long parseXDate(String value) {
        Long parsed = parseTimestamp(value);
        if (parsed != null) return parsed;
        try { return ZonedDateTime.parse(value.trim(), X_CLASSIC_DATE).toEpochSecond(); } catch (Exception ignored) { return null; }
    }

    private static void addXExpressed(JSONArray records, Set<String> seen, String text, String source, Long when) throws JSONException {
        String t = text == null ? "" : text.trim();
        String key = source + ":" + t.toLowerCase(Locale.ROOT);
        if (!t.isEmpty() && seen.add(key)) addRecord(records, t, source, "x", when);
    }

    /** Minimal RFC4180-ish CSV parser: handles quoted fields with embedded commas, newlines,
     * and doubled-quote escapes. Returns each row as a String[]; the first row is the header. */
    private static List<String[]> parseCsv(String text) {
        List<String[]> rows = new ArrayList<>();
        List<String> row = new ArrayList<>();
        StringBuilder field = new StringBuilder();
        boolean inQuotes = false;
        int i = 0, n = text.length();
        while (i < n) {
            char c = text.charAt(i);
            if (inQuotes) {
                if (c == '"') {
                    if (i + 1 < n && text.charAt(i + 1) == '"') { field.append('"'); i += 2; continue; }
                    inQuotes = false; i++; continue;
                }
                field.append(c); i++; continue;
            }
            if (c == '"') { inQuotes = true; i++; continue; }
            if (c == ',') { row.add(field.toString()); field.setLength(0); i++; continue; }
            if (c == '\r') { i++; continue; }
            if (c == '\n') {
                row.add(field.toString()); field.setLength(0);
                rows.add(row.toArray(new String[0])); row.clear();
                i++; continue;
            }
            field.append(c); i++;
        }
        if (field.length() > 0 || !row.isEmpty()) { row.add(field.toString()); rows.add(row.toArray(new String[0])); }
        return rows;
    }

    private static List<Map<String,String>> csvRowMaps(List<String[]> rows) {
        List<Map<String,String>> out = new ArrayList<>();
        if (rows.isEmpty()) return out;
        String[] header = rows.get(0);
        for (int r = 1; r < rows.size(); r++) {
            String[] row = rows.get(r);
            Map<String,String> map = new LinkedHashMap<>();
            for (int c = 0; c < header.length; c++) map.put(header[c], c < row.length ? row[c] : "");
            out.add(map);
        }
        return out;
    }

    private static String findColumn(String[] header, Pattern pattern) {
        for (String h : header) if (h != null && pattern.matcher(h).find()) return h;
        return null;
    }

    private static String stripChars(String s, String chars) {
        int start = 0, end = s.length();
        while (start < end && chars.indexOf(s.charAt(start)) >= 0) start++;
        while (end > start && chars.indexOf(s.charAt(end - 1)) >= 0) end--;
        return s.substring(start, end);
    }

    private static Long parseUtcSuffixedDate(String value) {
        if (value == null) return null;
        return parseTimestamp(value.trim().replaceAll("(?i)\\s*UTC$", "+00:00"));
    }

    /** Reddit's GDPR export: posts.csv / comments.csv, classified by header shape (a
     * title/url column -&gt; post, a parent/link column -&gt; comment), matching reddit.py
     * exactly. */
    private static void parseReddit(String text, JSONArray records, Set<String> seen) throws JSONException {
        List<String[]> rows = parseCsv(text);
        if (rows.isEmpty()) return;
        String[] header = rows.get(0);
        Set<String> names = new HashSet<>();
        for (String h : header) if (h != null) names.add(h.trim().toLowerCase(Locale.ROOT));
        String kind;
        if (names.contains("title") || names.contains("url")) kind = "post";
        else if (names.contains("parent") || names.contains("link")) kind = "comment";
        else if (names.contains("comment text")) throw new IllegalArgumentException("This comments.csv looks like a YouTube Takeout export, which isn't supported yet. Choose Reddit's posts.csv or comments.csv.");
        else return; // not a recognized posts/comments shape -- skip rather than guess wrong
        String titleCol = findColumn(header, REDDIT_TITLE_COL);
        String bodyCol = findColumn(header, REDDIT_BODY_COL);
        String subredditCol = findColumn(header, REDDIT_SUBREDDIT_COL);
        String dateCol = findColumn(header, REDDIT_DATE_COL);
        for (Map<String,String> row : csvRowMaps(rows)) {
            String title = titleCol != null ? row.getOrDefault(titleCol, "").trim() : "";
            String body = bodyCol != null ? row.getOrDefault(bodyCol, "").trim() : "";
            if (title.equals("[deleted]") || title.equals("[removed]")) title = "";
            if (body.equals("[deleted]") || body.equals("[removed]")) body = "";
            String textValue = kind.equals("post") ? stripChars(title + ". " + body, ". ") : body;
            if (textValue.isEmpty()) continue;
            String key = kind + ":" + textValue.toLowerCase(Locale.ROOT);
            if (!seen.add(key)) continue;
            String detail = subredditCol != null && !row.getOrDefault(subredditCol, "").trim().isEmpty()
                ? row.get(subredditCol).trim() : "reddit";
            Long when = dateCol != null ? parseUtcSuffixedDate(row.get(dateCol)) : null;
            addRecord(records, textValue, kind, detail, when);
        }
    }

    /** Amazon's "Request My Data" order-history CSV, classified by header shape (a
     * product-name-like column present), matching amazon.py exactly. */
    private static void parseAmazon(String text, JSONArray records, Set<String> seen) throws JSONException {
        List<String[]> rows = parseCsv(text);
        if (rows.isEmpty()) return;
        String[] header = rows.get(0);
        String productCol = findColumn(header, AMAZON_PRODUCT_COL);
        if (productCol == null) return; // not a recognized order-history shape -- skip
        String dateCol = findColumn(header, AMAZON_DATE_COL);
        for (Map<String,String> row : csvRowMaps(rows)) {
            String product = row.getOrDefault(productCol, "").trim();
            if (product.isEmpty()) continue;
            String key = product.toLowerCase(Locale.ROOT);
            if (!seen.add(key)) continue;
            Long when = dateCol != null ? parseUtcSuffixedDate(row.get(dateCol)) : null;
            addRecord(records, product, "order", "amazon", when);
        }
    }

    /** A simple, explicitly-defined screen-time contract (there is no universal export
     * standard): a JSON array of {"app":..., "minutes":..., "date":...} rows, matching
     * usage.py's load_blobs exactly. */
    private static void parseUsage(String text, JSONArray records, Set<String> seen) throws JSONException {
        if (text.startsWith("﻿")) text = text.substring(1);
        Object blob = new JSONTokener(text).nextValue();
        JSONArray rows;
        if (blob instanceof JSONArray) rows = (JSONArray) blob;
        else if (blob instanceof JSONObject) { rows = new JSONArray(); rows.put(blob); }
        else throw new IllegalArgumentException("Choose a usage.json screen-time file.");
        for (int i = 0; i < rows.length(); i++) {
            Object rowObj = rows.opt(i);
            if (!(rowObj instanceof JSONObject)) continue;
            JSONObject row = (JSONObject) rowObj;
            Object appObj = row.opt("app");
            Object minutesObj = row.opt("minutes");
            if (!(appObj instanceof String) || ((String) appObj).trim().isEmpty()) continue;
            if (!(minutesObj instanceof Number) || ((Number) minutesObj).doubleValue() < 0) continue;
            String app = ((String) appObj).trim();
            Object dateObj = row.opt("date");
            String date = dateObj instanceof String ? (String) dateObj : null;
            Long when = date != null ? parseTimestamp(date) : null;
            String key = app.toLowerCase(Locale.ROOT) + ":" + date;
            if (!seen.add(key)) continue;
            double minutes = ((Number) minutesObj).doubleValue();
            String value = minutes == Math.floor(minutes) ? String.valueOf((long) minutes)
                : String.valueOf(Math.round(minutes * 10) / 10.0);
            addRecord(records, app + ": " + value + " minutes", "usage", app, when);
        }
    }

    /** ISO-8601-ish date string -> epoch seconds, naive dates treated as UTC. Mirrors
     * mirror.py's parse_timestamp(). Returns null when the string doesn't parse. */
    private static Long parseTimestamp(String value) {
        if (value == null) return null;
        String text = value.trim().replace(' ', 'T');
        try { return OffsetDateTime.parse(text).toEpochSecond(); } catch (Exception ignored) { }
        try { return LocalDateTime.parse(text).toEpochSecond(ZoneOffset.UTC); } catch (Exception ignored) { }
        try { return LocalDate.parse(text).atStartOfDay(ZoneOffset.UTC).toEpochSecond(); } catch (Exception ignored) { return null; }
    }

    /** TikTok export walker, matching tiktok.py: carries the keys leading to each object so a
     * {Date, Link} entry only counts as a watch inside a Watch History / Video Browsing History
     * section, ad labels come only from AdInterestCategories (a single string), and a Title
     * under your own posts is a caption you wrote. */
    private static void walk(Object value, List<String> path, JSONArray records, Set<String> categories, Set<String> seen, JSONArray watchTimes, int depth) throws JSONException {
        if (depth > 100) throw new IllegalArgumentException("JSON nesting is too deep.");
        if (value instanceof JSONArray) {
            JSONArray list = (JSONArray)value;
            for (int i=0; i<list.length(); i++) walk(list.get(i),path,records,categories,seen,watchTimes,depth+1);
        } else if (value instanceof JSONObject) {
            JSONObject object = (JSONObject)value;
            boolean inWatchHistory = false, inOwnPosts = false;
            for (String key : path) {
                if (TIKTOK_WATCH_SECTION.matcher(key).find()) inWatchHistory = true;
                if (TIKTOK_OWN_POSTS.matcher(key).matches()) inOwnPosts = true;
            }
            Long nodeDate = null;
            Iterator<String> keys = object.keys();
            while (keys.hasNext()) {
                String key = keys.next();
                if (key.toLowerCase(Locale.ROOT).matches(".*(date|time).*") && object.get(key) instanceof String) {
                    Long parsed = parseTimestamp((String) object.get(key));
                    if (parsed != null) nodeDate = parsed;
                }
            }
            boolean countedWatch = false;
            keys = object.keys();
            while (keys.hasNext()) {
                String key = keys.next(); Object item = object.get(key);
                if (item instanceof String) {
                    String text = ((String)item).trim();
                    String source = SEARCH.matcher(key).find() ? "search" : HASH.matcher(key).find() ? "hashtag" : SOUND.matcher(key).find() ? "sound"
                        : TIKTOK_COMMENT.matcher(key.trim()).matches() ? "comment" : inOwnPosts && key.trim().equalsIgnoreCase("title") ? "caption" : null;
                    if (source != null) {
                        text = text.replaceFirst("^#+", "");
                        if (!text.isEmpty() && seen.add(text.toLowerCase(Locale.ROOT))) addRecord(records,text,source,"tiktok",nodeDate);
                    } else if (TIKTOK_AD_INTEREST.matcher(key).find()) {
                        addLabels(categories, text);
                    } else if (inWatchHistory && !countedWatch && nodeDate != null && URL.matcher(text).find()) {
                        watchTimes.put(nodeDate.longValue());
                        countedWatch = true;
                    }
                } else if (item instanceof JSONArray && TIKTOK_AD_INTEREST.matcher(key).find()) {
                    JSONArray list = (JSONArray)item;
                    for (int i=0; i<list.length(); i++) if (list.get(i) instanceof String) addLabels(categories, list.getString(i));
                }
                if (item instanceof JSONObject || item instanceof JSONArray) {
                    path.add(key);
                    walk(item,path,records,categories,seen,watchTimes,depth+1);
                    path.remove(path.size() - 1);
                }
            }
        }
    }

    private static void addLabels(Set<String> categories, String value) {
        for (String label : LABEL_SPLIT.split(value.trim())) if (!label.isEmpty() && label.length() < 60) categories.add(label);
    }

    private static JSONObject summarize(JSONArray records, Set<String> categories, JSONArray watchTimes, JSONArray sources) throws JSONException {
        Map<String,Integer> counts = new LinkedHashMap<>();
        for (int i=0; i<records.length(); i++) {
            Matcher matcher = WORD.matcher(records.getJSONObject(i).getString("text").toLowerCase(Locale.ROOT));
            while (matcher.find()) {
                String word = matcher.group();
                if (!STOP.contains(word)) counts.put(word, counts.getOrDefault(word,0)+1);
            }
        }
        List<Map.Entry<String,Integer>> sorted = new ArrayList<>(counts.entrySet());
        sorted.sort((a,b) -> Integer.compare(b.getValue(),a.getValue()));
        JSONArray terms = new JSONArray();
        for (int i=0; i<Math.min(16,sorted.size()); i++) terms.put(new JSONArray().put(sorted.get(i).getKey()).put(sorted.get(i).getValue()));
        return new JSONObject().put("records",records).put("terms",terms).put("categories",new JSONArray(categories)).put("watches",watchTimes.length()).put("watch_times",watchTimes)
            .put("sources",sources)
            .put("map",JSONObject.NULL).put("job",new JSONObject().put("status","idle"))
            .put("semantic_installed",false).put("platform","android").put("token","local-android-session");
    }
}
