"""Time-synced lyrics from LRCLIB (lrclib.net, a free public lyrics database)."""
import json
import os
import re
import urllib.parse
import urllib.request

from .analysis import CACHE, _safe

API = "https://lrclib.net/api/"
_LINE = re.compile(r"\[(\d+):(\d+(?:\.\d+)?)\]\s*(.*)")


def _call(path, params):
    req = urllib.request.Request(API + path + "?" + urllib.parse.urlencode(params),
                                 headers={"User-Agent": "animatrix-spotify"})
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            return json.load(r)
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return None
        raise


def _latin(c):
    return ord(c) < 0x250 or 0x2000 <= ord(c) < 0x2070


def parse(lrc):
    """LRC text -> [(start_seconds, end_seconds, text)], blank lines dropped."""
    raw = [(int(m[1]) * 60 + float(m[2]), m[3].strip())
           for m in map(_LINE.match, lrc.splitlines()) if m]
    letters = [c for _, t in raw for c in t if c.isalpha()]
    if not raw or sum(map(_latin, letters)) < 0.9 * len(letters):
        return []       # the lid font only covers Latin script
    lines = []
    for (start, text), (nxt, _) in zip(raw, raw[1:] + [(raw[-1][0] + 6, "")]):
        text = "".join(c for c in text if _latin(c)).strip()
        if text:
            lines.append((start, max(min(nxt, start + 9), start + 0.5), text))
    return lines


def fetch(track):
    """Synced lyric lines for a track ([] when there are none). Cached on disk."""
    path = os.path.join(CACHE, _safe(track["id"]) + ".lrc")
    if os.path.exists(path):
        with open(path) as f:
            return parse(f.read())
    secs = track["duration_ms"] / 1000
    title = re.sub(r"\s+-\s+.*$|\s*\((feat|with)[^)]*\)", "", track["title"])   # "- Remastered", "(feat. X)"
    artist = track["artist"].split(",")[0]
    hit = _call("get", {"track_name": title, "artist_name": artist,
                        "album_name": track.get("album", ""), "duration": round(secs)})
    if not (hit and hit.get("syncedLyrics")):
        found = _call("search", {"track_name": title, "artist_name": artist}) or []
        found = [h for h in found if h.get("syncedLyrics") and abs(h.get("duration", 0) - secs) <= 3]
        hit = min(found, key=lambda h: abs(h["duration"] - secs), default=None)
    lrc = (hit or {}).get("syncedLyrics") or ""
    with open(path, "w") as f:
        f.write(lrc)
    return parse(lrc)
