package local.thesaint.app;

import org.json.*;
import java.nio.charset.StandardCharsets;
import java.util.*;
import java.util.regex.*;
import java.time.*;

/** In-memory data adapter. No file, network, database, or permission access. */
public final class SaintData {
    private JSONObject state;
    private static final Set<String> STOP = new HashSet<>(Arrays.asList("the and that this with from have was were are for but not you your my our had has will into just about they them then when what some more been very can".split(" ")));
    private static final Pattern WORD = Pattern.compile("[\\p{L}]{3,}");
    private static final Pattern SEARCH = Pattern.compile("search.?term", Pattern.CASE_INSENSITIVE);
    private static final Pattern HASH = Pattern.compile("hashtag.?name|^hashtag$", Pattern.CASE_INSENSITIVE);
    private static final Pattern SOUND = Pattern.compile("sound.?name|song.?name", Pattern.CASE_INSENSITIVE);
    private static final Pattern INTEREST = Pattern.compile("interest|categor", Pattern.CASE_INSENSITIVE);
    private static final Pattern URL = Pattern.compile("https?://\\S*tiktok\\.com/\\S+", Pattern.CASE_INSENSITIVE);
    private static final Pattern SEARCHED = Pattern.compile("^searched for\\s+", Pattern.CASE_INSENSITIVE);
    private static final Pattern WATCHED = Pattern.compile("^watched\\s+", Pattern.CASE_INSENSITIVE);
    private static final Pattern REMOVED = Pattern.compile("a video that has been removed|a video that isn.t available", Pattern.CASE_INSENSITIVE);
    private static final Pattern IG_TOPIC_FIELD = Pattern.compile("^name$", Pattern.CASE_INSENSITIVE);
    private static final Pattern IG_SEARCH_FIELD = Pattern.compile("search", Pattern.CASE_INSENSITIVE);
    private static final Pattern X_WRAPPER = Pattern.compile("^\\s*window\\.YTD\\.\\w+\\.part\\d+\\s*=\\s*");
    private static final Pattern X_QUERY_FIELD = Pattern.compile("query", Pattern.CASE_INSENSITIVE);
    private static final Pattern X_TWEET_TEXT_FIELD = Pattern.compile("^full_?text$", Pattern.CASE_INSENSITIVE);
    private static final Pattern X_INTEREST_FIELD = Pattern.compile("interest", Pattern.CASE_INSENSITIVE);
    private static final Pattern X_DATE_FIELD = Pattern.compile("date|time", Pattern.CASE_INSENSITIVE);
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
        {".*(watch-history|search-history)\\.json$", "youtube"},
        {".*(your_topics|word_or_phrase_searches|ads_viewed|ads_and_topics).*", "instagram"},
        {".*(streaming_history_audio|streaminghistory).*", "spotify"},
        {".*(posts\\.csv|comments\\.csv)$", "reddit"},
        {".*retail\\.orderhistory.*", "amazon"},
        {".*(usage|screen.?time).*", "usage"},
        {".*(user_data\\.json|tiktok).*", "tiktok"},
        {".*\\.js$", "x"},
    };
    public SaintData() { try { state = summarize(new JSONArray(), new TreeSet<>(), 0, new JSONArray()); } catch (JSONException error) { throw new IllegalStateException(error); } }

    public synchronized String request(String path, String payload) {
        try {
            if (payload.length() > 8_000_000 || payload.getBytes(StandardCharsets.UTF_8).length > 8_000_000) throw new IllegalArgumentException("Choose a smaller import (maximum 8 MB).");
            if (path.equals("/api/state")) return state.toString();
            if (path.equals("/api/clear")) { state = summarize(new JSONArray(), new TreeSet<>(), 0, new JSONArray()); return "{\"ok\":true}"; }
            if (path.equals("/api/import")) {
                JSONObject input = new JSONObject(payload);
                String requested = input.optString("source");
                String resolved = requested.equals("auto") ? detectSource(input.optJSONArray("files")) : requested;
                input.put("source", resolved);
                JSONObject incoming = importData(input);
                state = mergeSnapshot(state, incoming, resolved);
                return new JSONObject().put("ok", true).put("detected_source", resolved).toString();
            }
            throw new IllegalArgumentException("This action is unavailable on Android.");
        } catch (StackOverflowError error) {
            return "{\"error\":\"JSON nesting is too deep.\"}";
        } catch (Exception error) {
            return "{\"error\":" + JSONObject.quote(error.getMessage() == null ? "Import failed. Check the selected files." : error.getMessage()) + "}";
        }
    }

    private static String detectSource(JSONArray files) throws JSONException {
        if (files == null || files.length() == 0) throw new IllegalArgumentException("Choose one or more files to auto-detect a source.");
        Set<String> guesses = new TreeSet<>();
        for (int i = 0; i < files.length(); i++) {
            String name = files.getJSONObject(i).optString("name", "").toLowerCase(Locale.ROOT);
            for (String[] entry : FILENAME_PATTERNS) {
                if (name.matches(entry[0])) { guesses.add(entry[1]); break; }
            }
        }
        if (guesses.size() == 1) return guesses.iterator().next();
        if (guesses.isEmpty()) throw new IllegalArgumentException("Could not detect a source from these filenames. Choose one manually from the dropdown.");
        throw new IllegalArgumentException("These files look like different sources (" + String.join(", ", guesses) + "). Import one source at a time.");
    }

    private static JSONObject mergeSnapshot(JSONObject existing, JSONObject incoming, String sourceName) throws JSONException {
        JSONArray records = new JSONArray();
        JSONArray existingRecords = existing.getJSONArray("records");
        for (int i = 0; i < existingRecords.length(); i++) records.put(existingRecords.get(i));
        JSONArray incomingRecords = incoming.getJSONArray("records");
        for (int i = 0; i < incomingRecords.length(); i++) records.put(incomingRecords.get(i));
        if (records.length() > 5000) throw new IllegalArgumentException("Combined session exceeds 5,000 passages. Clear the session or import fewer files.");
        TreeSet<String> categories = new TreeSet<>();
        JSONArray existingCats = existing.getJSONArray("categories");
        for (int i = 0; i < existingCats.length(); i++) categories.add(existingCats.getString(i));
        JSONArray incomingCats = incoming.getJSONArray("categories");
        for (int i = 0; i < incomingCats.length(); i++) categories.add(incomingCats.getString(i));
        int watches = existing.getInt("watches") + incoming.getInt("watches");
        JSONArray sources = new JSONArray();
        boolean matched = false;
        JSONArray existingSources = existing.optJSONArray("sources");
        if (existingSources != null) {
            for (int i = 0; i < existingSources.length(); i++) {
                JSONObject entry = existingSources.getJSONObject(i);
                if (entry.getString("source").equals(sourceName)) {
                    entry.put("count", entry.getInt("count") + incomingRecords.length());
                    matched = true;
                }
                sources.put(entry);
            }
        }
        if (!matched) sources.put(new JSONObject().put("source", sourceName).put("count", incomingRecords.length()));
        return summarize(records, categories, watches, sources);
    }

    private static void addRecord(JSONArray records, String text, String source, String detail, Long when) throws JSONException {
        if (records.length() >= 5000) throw new IllegalArgumentException("Choose fewer files (maximum 5,000 passages).");
        JSONObject record = new JSONObject().put("text", text).put("source", source).put("detail", detail);
        record.put("when", when == null ? JSONObject.NULL : when);
        records.put(record);
    }

    private JSONObject importData(JSONObject input) throws JSONException {
        String source = input.optString("source");
        JSONArray records = new JSONArray();
        TreeSet<String> categories = new TreeSet<>();
        int[] watches = {0};
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
                    case "spotify": parseSpotify(text, records, seen); break;
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
                        walk(blob, records, categories, seen, watches, 0);
                    }
                }
            }
        } else throw new IllegalArgumentException("Choose personal notes or a supported export (TikTok, YouTube, Instagram, X/Twitter, Spotify, Reddit, Amazon, or device usage).");
        if (records.length() == 0 && categories.isEmpty() && watches[0] == 0) throw new IllegalArgumentException("No usable entries found in the selected files.");
        return summarize(records, categories, watches[0], new JSONArray());
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
                recordSource = "watch";
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
        walkInstagram(blob, records, categories, seen, 0);
    }

    private static void walkInstagram(Object value, JSONArray records, Set<String> categories, Set<String> seen, int depth) throws JSONException {
        if (depth > 100) throw new IllegalArgumentException("JSON nesting is too deep.");
        if (value instanceof JSONArray) {
            JSONArray list = (JSONArray) value;
            for (int i = 0; i < list.length(); i++) walkInstagram(list.get(i), records, categories, seen, depth + 1);
        } else if (value instanceof JSONObject) {
            JSONObject object = (JSONObject) value;
            Object smdObj = object.opt("string_map_data");
            if (smdObj instanceof JSONObject) {
                JSONObject smd = (JSONObject) smdObj;
                Long when = null;
                Iterator<String> tsKeys = smd.keys();
                while (tsKeys.hasNext()) {
                    Object v = smd.opt(tsKeys.next());
                    if (v instanceof JSONObject) {
                        Object ts = ((JSONObject) v).opt("timestamp");
                        if (ts instanceof Number) when = ((Number) ts).longValue();
                    }
                }
                Iterator<String> fieldKeys = smd.keys();
                while (fieldKeys.hasNext()) {
                    String field = fieldKeys.next();
                    Object v = smd.opt(field);
                    if (!(v instanceof JSONObject)) continue;
                    Object valueObj = ((JSONObject) v).opt("value");
                    if (!(valueObj instanceof String)) continue;
                    String valueStr = ((String) valueObj).trim();
                    if (valueStr.isEmpty() || valueStr.equalsIgnoreCase("not_stored")) continue;
                    if (IG_TOPIC_FIELD.matcher(field).matches()) {
                        if (valueStr.length() < 60) categories.add(valueStr);
                    } else if (IG_SEARCH_FIELD.matcher(field).find()) {
                        String key = "search:" + valueStr.toLowerCase(Locale.ROOT);
                        if (seen.add(key)) addRecord(records, valueStr, "search", "instagram", when);
                    }
                }
            }
            Iterator<String> keys = object.keys();
            while (keys.hasNext()) {
                Object item = object.get(keys.next());
                if (item instanceof JSONObject || item instanceof JSONArray) walkInstagram(item, records, categories, seen, depth + 1);
            }
        }
    }

    /** Spotify Extended Streaming History JSON: a stable, documented schema, so fields are
     * read directly rather than scanned. Matches spotify.py's load_blobs exactly, including
     * the modern/older field-name fallback and the podcast-episode fallback. */
    private static void parseSpotify(String text, JSONArray records, Set<String> seen) throws JSONException {
        if (text.startsWith("﻿")) text = text.substring(1);
        Object blob = new JSONTokener(text).nextValue();
        JSONArray entries;
        if (blob instanceof JSONArray) entries = (JSONArray) blob;
        else if (blob instanceof JSONObject) { entries = new JSONArray(); entries.put(blob); }
        else throw new IllegalArgumentException("Choose a Spotify streaming history JSON file.");
        for (int i = 0; i < entries.length(); i++) {
            Object entryObj = entries.opt(i);
            if (!(entryObj instanceof JSONObject)) continue;
            JSONObject entry = (JSONObject) entryObj;
            String track = textField(entry, "master_metadata_track_name");
            if (track == null) track = textField(entry, "trackName");
            String artist = textField(entry, "master_metadata_album_artist_name");
            if (artist == null) artist = textField(entry, "artistName");
            Object whenRaw = entry.has("ts") ? entry.opt("ts") : entry.opt("endTime");
            Long when = whenRaw instanceof String ? parseTimestamp((String) whenRaw) : null;
            String recordText, recordSource;
            if (track != null && artist != null) {
                recordText = track + " — " + artist; recordSource = "track";
            } else {
                String episode = textField(entry, "episode_name");
                String show = textField(entry, "episode_show_name");
                if (episode == null) continue; // neither a track nor a podcast episode -- nothing usable
                recordText = show != null ? episode + " — " + show : episode;
                recordSource = "podcast";
            }
            String key = recordSource + ":" + recordText.toLowerCase(Locale.ROOT);
            if (seen.add(key)) addRecord(records, recordText, recordSource, "spotify", when);
        }
    }

    private static String textField(JSONObject obj, String key) {
        Object v = obj.opt(key);
        return (v instanceof String && !((String) v).trim().isEmpty()) ? ((String) v).trim() : null;
    }

    /** X/Twitter archive export: .js files wrapped as window.YTD.&lt;stream&gt;.part0 = [...];
     * strips that wrapper, then scans by key name wherever it appears, matching x.py's
     * load_blobs exactly. */
    private static void parseX(String text, JSONArray records, Set<String> categories, Set<String> seen) throws JSONException {
        String stripped = text.startsWith("﻿") ? text.substring(1) : text;
        if (stripped.startsWith("window.YTD")) {
            Matcher wrapper = X_WRAPPER.matcher(stripped);
            if (wrapper.find()) stripped = stripped.substring(wrapper.end());
            stripped = stripped.trim();
            if (stripped.endsWith(";")) stripped = stripped.substring(0, stripped.length() - 1);
        }
        Object blob;
        try {
            blob = new JSONTokener(stripped).nextValue();
        } catch (JSONException exc) {
            throw new IllegalArgumentException("Cannot read JSON export: " + exc.getMessage());
        }
        walkX(blob, records, categories, seen, 0);
    }

    private static void walkX(Object value, JSONArray records, Set<String> categories, Set<String> seen, int depth) throws JSONException {
        if (depth > 100) throw new IllegalArgumentException("JSON nesting is too deep.");
        if (value instanceof JSONArray) {
            JSONArray list = (JSONArray) value;
            for (int i = 0; i < list.length(); i++) walkX(list.get(i), records, categories, seen, depth + 1);
        } else if (value instanceof JSONObject) {
            JSONObject object = (JSONObject) value;
            Long nodeDate = null;
            Iterator<String> keys = object.keys();
            while (keys.hasNext()) {
                String key = keys.next();
                Object v = object.get(key);
                if (v instanceof String && X_DATE_FIELD.matcher(key).find()) {
                    Long parsed = parseTimestamp((String) v);
                    if (parsed != null) nodeDate = parsed;
                }
            }
            keys = object.keys();
            while (keys.hasNext()) {
                String key = keys.next(); Object item = object.get(key);
                if (item instanceof String) {
                    String text = ((String) item).trim();
                    if (X_TWEET_TEXT_FIELD.matcher(key).matches()) {
                        addXExpressed(records, seen, text, "post", nodeDate);
                    } else if (X_QUERY_FIELD.matcher(key).find()) {
                        addXExpressed(records, seen, text, "search", nodeDate);
                    } else if (X_INTEREST_FIELD.matcher(key).find()) {
                        String label = text.trim();
                        if (!label.isEmpty() && label.length() < 60) categories.add(label);
                    }
                } else if (item instanceof JSONArray && X_INTEREST_FIELD.matcher(key).find()) {
                    JSONArray list = (JSONArray) item;
                    for (int i = 0; i < list.length(); i++) if (list.get(i) instanceof String) {
                        String label = list.getString(i).trim();
                        if (!label.isEmpty() && label.length() < 60) categories.add(label);
                    }
                }
                if (item instanceof JSONObject || item instanceof JSONArray) walkX(item, records, categories, seen, depth + 1);
            }
        }
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
        else return; // not a recognized posts/comments shape -- skip rather than guess wrong
        String titleCol = findColumn(header, REDDIT_TITLE_COL);
        String bodyCol = findColumn(header, REDDIT_BODY_COL);
        String subredditCol = findColumn(header, REDDIT_SUBREDDIT_COL);
        String dateCol = findColumn(header, REDDIT_DATE_COL);
        for (Map<String,String> row : csvRowMaps(rows)) {
            String title = titleCol != null ? row.getOrDefault(titleCol, "").trim() : "";
            String body = bodyCol != null ? row.getOrDefault(bodyCol, "").trim() : "";
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

    private static void walk(Object value, JSONArray records, Set<String> categories, Set<String> seen, int[] watches, int depth) throws JSONException {
        if (depth > 100) throw new IllegalArgumentException("JSON nesting is too deep.");
        if (value instanceof JSONArray) {
            JSONArray list = (JSONArray)value;
            for (int i=0; i<list.length(); i++) walk(list.get(i),records,categories,seen,watches,depth+1);
        } else if (value instanceof JSONObject) {
            JSONObject object = (JSONObject)value;
            Long nodeDate = null;
            Iterator<String> keys = object.keys();
            while (keys.hasNext()) {
                String key = keys.next();
                if (key.toLowerCase(Locale.ROOT).matches(".*(date|time).*") && object.get(key) instanceof String) {
                    Long parsed = parseTimestamp((String) object.get(key));
                    if (parsed != null) nodeDate = parsed;
                }
            }
            keys = object.keys();
            while (keys.hasNext()) {
                String key = keys.next(); Object item = object.get(key);
                if (item instanceof String) {
                    String text = ((String)item).trim();
                    String source = SEARCH.matcher(key).find() ? "search" : HASH.matcher(key).find() ? "hashtag" : SOUND.matcher(key).find() ? "sound" : key.trim().equalsIgnoreCase("comment") ? "comment" : null;
                    if (source != null) {
                        text = text.replaceFirst("^#+", "");
                        if (!text.isEmpty() && seen.add(text.toLowerCase(Locale.ROOT))) addRecord(records,text,source,"tiktok",nodeDate);
                    } else if (nodeDate != null && URL.matcher(text).find()) watches[0]++;
                } else if (item instanceof JSONArray && INTEREST.matcher(key).find()) {
                    JSONArray list = (JSONArray)item;
                    for (int i=0; i<list.length(); i++) if (list.get(i) instanceof String) {
                        String label = list.getString(i).trim();
                        if (!label.isEmpty() && label.length()<60) categories.add(label);
                    }
                }
                if (item instanceof JSONObject || item instanceof JSONArray) walk(item,records,categories,seen,watches,depth+1);
            }
        }
    }

    private static JSONObject summarize(JSONArray records, Set<String> categories, int watches, JSONArray sources) throws JSONException {
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
        return new JSONObject().put("records",records).put("terms",terms).put("categories",new JSONArray(categories)).put("watches",watches)
            .put("sources",sources)
            .put("map",JSONObject.NULL).put("job",new JSONObject().put("status","idle"))
            .put("semantic_installed",false).put("platform","android").put("token","local-android-session");
    }
}
