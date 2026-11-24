"""Time-synced lyrics from LRCLIB (lrclib.net, a free public lyrics database), for the LED
Arcade panel's word-by-word display. Same rules as the wordglow app: a word-synced
`lyricsfile` (YAML) first, then enhanced or plain LRC with word timings estimated inside
each line by word length. Only Latin-script lyrics are kept (the panel font is ASCII).

Lyrics are reduced to a flat word list [[start_ms, end_ms, text], ...] and cached per track.
"""
import bisect
import json
import os
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

API = "https://lrclib.net/api/"
USER_AGENT = "animatrix-spotify (github.com/AV3RG/animatrix-spotify)"
MISS_RETRY = 7 * 86400     # "no lyrics" is re-checked after a week...
ERROR_RETRY = 3600         # ...a failed lookup after an hour

_TIME = re.compile(r"\[(\d{1,3}):(\d{1,2}(?:[.:]\d{1,3})?)\]")
_WORD = re.compile(r"<(\d{1,3}):(\d{1,2}(?:[.:]\d{1,3})?)>")


def _ms(minutes, seconds):
    return round((int(minutes) * 60 + float(str(seconds).replace(":", "."))) * 1000)


def estimate_words(line):
    """Spread a line's duration over its words, weighted by length."""
    parts = line["text"].split()
    if not parts:
        return []
    span = max(300, (line["end"] - line["start"]) * 0.9)
    weights = [len(p) + 2 for p in parts]
    total = sum(weights)
    t, out = float(line["start"]), []
    for p, w in zip(parts, weights):
        d = w / total * span
        out.append({"text": p, "start": round(t), "end": round(t + d)})
        t += d
    return out


def _finish(lines):
    lines = sorted((l for l in lines if l["text"].strip()), key=lambda l: l["start"])
    for i, line in enumerate(lines):
        nxt = lines[i + 1] if i + 1 < len(lines) else None
        if not line.get("end") or line["end"] <= line["start"]:
            cap = line["start"] + max(2500, len(line["text"].split()) * 650)
            line["end"] = min(nxt["start"], cap) if nxt else cap
        if not line.get("words"):
            line["words"] = estimate_words(line)
        words = line["words"]
        for j, w in enumerate(words):
            if not w.get("end") or w["end"] <= w["start"]:
                w["end"] = words[j + 1]["start"] if j + 1 < len(words) else max(line["end"], w["start"] + 250)
    return lines


def parse_lrc(text):
    """Plain LRC or enhanced LRC (<mm:ss.xx> word tags) -> lines with words (ms)."""
    lines = []
    for raw in str(text).splitlines():
        stamps, pos = [], 0
        while (m := _TIME.match(raw, pos)):
            stamps.append(_ms(m[1], m[2]))
            pos = m.end()
        if not stamps:
            continue
        body = raw[pos:]
        words = []
        pieces = _WORD.split(body)  # [pre, min, sec, text, min, sec, text, ...]
        for i in range(1, len(pieces) - 2, 3):
            if pieces[i + 2].strip():
                words.append({"text": pieces[i + 2].strip(), "start": _ms(pieces[i], pieces[i + 1])})
        clean = " ".join(_WORD.sub("", body).split())
        for start in stamps:
            lines.append({"text": clean, "start": start, "words": [dict(w) for w in words]})
    return _finish(lines)


def parse_lyricsfile(text):
    """LRCLIB's word-synced YAML -> lines with words (ms), or None if unreadable."""
    try:
        import yaml
        doc = yaml.safe_load(text)
    except Exception:
        return None
    if not isinstance(doc, dict) or not isinstance(doc.get("lines"), list):
        return None
    lines = []
    for l in doc["lines"]:
        if not isinstance(l, dict) or not isinstance(l.get("text"), str) or not isinstance(l.get("start_ms"), int):
            continue
        words = [{"text": w["text"], "start": w["start_ms"], "end": w.get("end_ms")}
                 for w in l.get("words") or []
                 if isinstance(w, dict) and isinstance(w.get("text"), str) and isinstance(w.get("start_ms"), int)]
        lines.append({"text": l["text"], "start": l["start_ms"], "end": l.get("end_ms"), "words": words})
    return _finish(lines)


def _latin(c):
    return ord(c) < 0x250 or 0x2000 <= ord(c) < 0x2070


def from_record(rec):
    """An LRCLIB record -> flat word list, or None (no synced lyrics, instrumental, non-Latin)."""
    if not rec or rec.get("instrumental"):
        return None
    lines = parse_lyricsfile(rec["lyricsfile"]) if rec.get("lyricsfile") else None
    if not lines and rec.get("syncedLyrics"):
        lines = parse_lrc(rec["syncedLyrics"])
    if not lines:
        return None
    letters = [c for l in lines for c in l["text"] if c.isalpha()]
    if not letters or sum(map(_latin, letters)) < 0.9 * len(letters):
        return None
    words = []
    for l in lines:
        for w in l["words"]:
            text = "".join(c for c in w["text"] if _latin(c)).strip()
            if text:
                words.append([int(w["start"]), int(w["end"]), text])
    words.sort(key=lambda w: w[0])
    return words or None


def word_at(words, ms):
    """(index, word) being sung at ms, or None."""
    i = bisect.bisect_right([w[0] for w in words], ms) - 1
    if i >= 0 and ms < words[i][1]:
        return i, words[i]
    return None


def score(rec, duration_s):
    s = 0
    if rec.get("hasWordSync"):
        s += 100
    if rec.get("syncedLyrics") or rec.get("lyricsfile"):
        s += 50
    if duration_s and isinstance(rec.get("duration"), (int, float)):
        diff = abs(rec["duration"] - duration_s)
        if diff > 8:
            return -1  # almost certainly a different recording
        s -= diff * 4
    return s


def pick(records, duration_s):
    scored = [(score(r, duration_s), r) for r in records if r]
    scored = [x for x in scored if x[0] >= 0]
    return max(scored, key=lambda x: x[0])[1] if scored else None


def _call(path, params):
    req = urllib.request.Request(API + path + "?" + urllib.parse.urlencode(params), headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            return json.load(r)
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return None
        raise


def _clean_title(title):
    title = re.sub(r"\s+-\s+(.*remaster.*|.*version.*|.*edit.*|.*mix.*|live.*|mono|stereo)$", "", title, flags=re.I)
    return re.sub(r"\s*[(\[](feat\.?|ft\.?|with|from|remaster|live|bonus)[^)\]]*[)\]]", "", title, flags=re.I).strip()


def lookup(track):
    """Best LRCLIB record for a Spotify track, or None."""
    secs = round(track["duration_ms"] / 1000) if track.get("duration_ms") else None
    artist = track.get("artist", "")
    first_artist = re.split(r"\s*(?:,|&|\bfeat\.?|\bft\.?|;)\s*", artist, flags=re.I)[0].strip()
    found = []
    params = {"track_name": track["title"], "artist_name": artist}
    if secs:
        params["duration"] = secs
    hit = _call("get", params)
    if hit:
        found.append(hit)
    if not any(r.get("hasWordSync") for r in found):
        for q in ({"track_name": track["title"], "artist_name": artist},
                  {"track_name": _clean_title(track["title"]), "artist_name": first_artist}):
            found += _call("search", q) or []
            if any(r.get("hasWordSync") and score(r, secs) >= 0 for r in found):
                break
    return pick(found, secs)


def _safe(track_id):
    return re.sub(r"[^\w.-]", "_", str(track_id))


class LyricsStore:
    """Looks up the current track's lyrics in the background and caches them on disk.
    get(track_id): the word list ([] = none), or None while not known yet."""

    def __init__(self, cache_dir, lookup_fn=lookup, log=lambda *a: None):
        os.makedirs(cache_dir, exist_ok=True)
        self.dir = cache_dir
        self.lookup = lookup_fn
        self.log = log
        self.ready = {}
        self.wanted = None
        self.cv = threading.Condition()

    def start(self):
        threading.Thread(target=self._work, daemon=True).start()

    def want(self, track):
        with self.cv:
            self.wanted = track if track and track.get("kind", "track") == "track" else None
            self.cv.notify_all()

    def get(self, track_id):
        return self.ready.get(track_id)

    def _path(self, track_id):
        return os.path.join(self.dir, _safe(track_id) + ".lyrics.json")

    def _cached(self, track_id):
        try:
            with open(self._path(track_id)) as f:
                data = json.load(f)
        except (OSError, ValueError):
            return None
        retry = ERROR_RETRY if data.get("error") else MISS_RETRY
        if not data["words"] and time.time() - data.get("at", 0) > retry:
            return None  # worth asking again
        return data["words"]

    def _work(self):
        while True:
            with self.cv:
                while (t := self.wanted) is None or t["id"] in self.ready:
                    self.cv.wait(timeout=30)
            words = self._cached(t["id"])
            if words is None:
                error = False
                try:
                    words = from_record(self.lookup(t)) or []
                except Exception as e:
                    self.log(f"lyrics lookup failed for {t.get('title')}: {e}")
                    words, error = [], True
                try:
                    with open(self._path(t["id"]), "w") as f:
                        json.dump({"words": words, "at": time.time(), "error": error}, f)
                except OSError:
                    pass
                if words:
                    self.log(f"lyrics ready: {t.get('title')} ({len(words)} words)")
            self.ready[t["id"]] = words
            for old in list(self.ready)[:-12]:  # keep memory bounded
                del self.ready[old]
