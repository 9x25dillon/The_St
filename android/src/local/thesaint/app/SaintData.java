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
    public SaintData() { try { state = summarize(new JSONArray(), new TreeSet<>(), 0); } catch (JSONException error) { throw new IllegalStateException(error); } }

    public synchronized String request(String path, String payload) {
        try {
            if (payload.length() > 8_000_000 || payload.getBytes(StandardCharsets.UTF_8).length > 8_000_000) throw new IllegalArgumentException("Choose a smaller import (maximum 8 MB).");
            if (path.equals("/api/state")) return state.toString();
            if (path.equals("/api/clear")) state = summarize(new JSONArray(), new TreeSet<>(), 0);
            else if (path.equals("/api/import")) state = importData(new JSONObject(payload));
            else throw new IllegalArgumentException("This action is unavailable on Android.");
            return "{\"ok\":true}";
        } catch (StackOverflowError error) {
            return "{\"error\":\"JSON nesting is too deep.\"}";
        } catch (Exception error) {
            return "{\"error\":" + JSONObject.quote(error.getMessage() == null ? "Import failed. Check the selected files." : error.getMessage()) + "}";
        }
    }

    private static void addRecord(JSONArray records, String text, String source, String detail) throws JSONException {
        if (records.length() >= 5000) throw new IllegalArgumentException("Choose fewer files (maximum 5,000 passages).");
        records.put(new JSONObject().put("text", text).put("source", source).put("detail", detail));
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
            for (String[] theme : themes) for (int i=1; i<=12; i++) addRecord(records, theme[1] + " Reflection " + i + ".", "note", "Sample journal · " + theme[0]);
        } else if (source.equals("notes") || source.equals("tiktok")) {
            JSONArray files = input.optJSONArray("files");
            if (files == null || files.length() == 0 || files.length() > 500) throw new IllegalArgumentException("Choose between 1 and 500 files.");
            Set<String> seen = new HashSet<>();
            for (int i=0; i<files.length(); i++) {
                JSONObject file = files.getJSONObject(i);
                String text = file.getString("text");
                if (source.equals("notes")) parseNote(file.getString("name"), text, records);
                else {
                    if (text.startsWith("\ufeff")) text = text.substring(1);
                    JSONTokener tokener = new JSONTokener(text);
                    Object blob = tokener.nextValue();
                    if (!(blob instanceof JSONObject) && !(blob instanceof JSONArray)) throw new IllegalArgumentException("Choose a TikTok JSON object or array.");
                    if (tokener.nextClean() != 0) throw new IllegalArgumentException("Unexpected text after JSON data.");
                    walk(blob, records, categories, seen, watches, 0);
                }
            }
        } else throw new IllegalArgumentException("Choose personal notes or a TikTok export.");
        if (records.length() == 0 && categories.isEmpty() && watches[0] == 0) throw new IllegalArgumentException("No usable entries found in the selected files.");
        return summarize(records, categories, watches[0]);
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
                addRecord(records, String.join(" ", Arrays.copyOfRange(words,start,Math.min(start+120,words.length))), "note", name + " · " + heading);
            }
        }
    }

    private static boolean isDate(Object value) {
        if (!(value instanceof String)) return false;
        String text = ((String)value).trim().replace(' ', 'T');
        try { OffsetDateTime.parse(text); return true; } catch (Exception ignored) { }
        try { LocalDateTime.parse(text); return true; } catch (Exception ignored) { }
        try { LocalDate.parse(text); return true; } catch (Exception ignored) { return false; }
    }

    private static void walk(Object value, JSONArray records, Set<String> categories, Set<String> seen, int[] watches, int depth) throws JSONException {
        if (depth > 100) throw new IllegalArgumentException("JSON nesting is too deep.");
        if (value instanceof JSONArray) {
            JSONArray list = (JSONArray)value;
            for (int i=0; i<list.length(); i++) walk(list.get(i),records,categories,seen,watches,depth+1);
        } else if (value instanceof JSONObject) {
            JSONObject object = (JSONObject)value;
            boolean dated = false;
            Iterator<String> keys = object.keys();
            while (keys.hasNext()) {
                String key = keys.next();
                if (key.toLowerCase(Locale.ROOT).matches(".*(date|time).*")) dated |= isDate(object.get(key));
            }
            keys = object.keys();
            while (keys.hasNext()) {
                String key = keys.next(); Object item = object.get(key);
                if (item instanceof String) {
                    String text = ((String)item).trim();
                    String source = SEARCH.matcher(key).find() ? "search" : HASH.matcher(key).find() ? "hashtag" : SOUND.matcher(key).find() ? "sound" : key.trim().equalsIgnoreCase("comment") ? "comment" : null;
                    if (source != null) {
                        text = text.replaceFirst("^#+", "");
                        if (!text.isEmpty() && seen.add(text.toLowerCase(Locale.ROOT))) addRecord(records,text,source,"tiktok");
                    } else if (dated && URL.matcher(text).find()) watches[0]++;
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

    private static JSONObject summarize(JSONArray records, Set<String> categories, int watches) throws JSONException {
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
            .put("map",JSONObject.NULL).put("job",new JSONObject().put("status","idle"))
            .put("semantic_installed",false).put("platform","android").put("token","local-android-session");
    }
}
