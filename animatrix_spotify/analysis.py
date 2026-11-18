"""Beat data for a track: fetch matching audio from YouTube, reduce it to a
small per-band loudness timeline (50 frames/s), cache that, discard the audio.

The daemon then looks up the frame for Spotify's reported playback position,
so the bars follow the real music whichever device is playing it.
"""
import glob
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import urllib.parse

import numpy as np

SR, HOP, NFFT = 22050, 441, 1024
FPS = SR / HOP               # 50 analysis frames per second
NB = 9                       # frequency bands = bars on the lid
CACHE = os.path.expanduser("~/.cache/animatrix-spotify")
MISS_RETRY = 7 * 86400


def _tool(name, *fallbacks):
    path = shutil.which(name)
    if path:
        return path
    for pattern in fallbacks:
        hits = sorted(glob.glob(os.path.expanduser(pattern)))
        if hits:
            return hits[-1]
    return None


FFMPEG = _tool("ffmpeg", "~/.local/bin/ffmpeg")
NODE = _tool("node", "~/.nvm/versions/node/*/bin/node")
YTDLP = [sys.executable, "-m", "yt_dlp", "--no-warnings", "--no-playlist"] + (
    ["--js-runtimes", "node:" + NODE] if NODE else [])


def _log(*a):
    print(*a, file=sys.stderr, flush=True)


def _run(cmd, timeout):
    return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)


def _candidates(track):
    """YouTube video ids worth trying, best first."""
    query = f"{track['artist'].split(',')[0]} {track['title']}"
    ids = []
    for url in ("https://music.youtube.com/search?q=" + urllib.parse.quote_plus(query) + "#songs",
                "ytsearch6:" + query):
        try:
            out = _run(YTDLP + ["--flat-playlist", "--playlist-end", "4", "--print", "id", url], 40).stdout
        except subprocess.TimeoutExpired:
            continue
        ids += [i for i in out.split() if re.fullmatch(r"[\w-]{11}", i) and i not in ids]
    return ids[:7]


def _fetch_pcm(video_id, seconds):
    """Mono float PCM of a video whose length matches the track, else None."""
    with tempfile.TemporaryDirectory(prefix="animatrix-") as tmp:
        r = _run(YTDLP + [
            "-q", "--extractor-args", "youtube:player_client=web_embedded,default",
            "--match-filter", f"duration>{seconds - 3} & duration<{seconds + 3}",
            "-f", "worstaudio[abr>=40]/bestaudio/best", "--ffmpeg-location", os.path.dirname(FFMPEG),
            "-o", os.path.join(tmp, "a.%(ext)s"), "https://www.youtube.com/watch?v=" + video_id,
        ], 120)
        files = os.listdir(tmp)
        if not files:
            return None
        raw = subprocess.run(
            [FFMPEG, "-v", "error", "-i", os.path.join(tmp, files[0]),
             "-ac", "1", "-ar", str(SR), "-f", "s16le", "-"],
            capture_output=True, timeout=120).stdout
    pcm = np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768
    return pcm if len(pcm) > SR * 5 else None


def analyse(pcm):
    """PCM -> uint8 array [frames, NB + 1]: bar heights per band, plus a bass
    'kick' column used to pulse the artwork."""
    n = (len(pcm) - NFFT) // HOP
    frames = np.lib.stride_tricks.sliding_window_view(pcm, NFFT)[::HOP][:n]
    spec = np.abs(np.fft.rfft(frames * np.hanning(NFFT).astype(np.float32), axis=1)) ** 2
    edges = np.geomspace(45, 9000, NB + 1) / (SR / NFFT)
    lo = np.floor(edges[:-1]).astype(int).clip(1)
    hi = np.maximum(np.ceil(edges[1:]).astype(int), lo + 1)
    db = 10 * np.log10(np.stack([spec[:, a:b].sum(1) for a, b in zip(lo, hi)], 1) + 1e-9)
    top = np.percentile(db, 99, axis=0)
    v = np.clip((db - (top - 28)) / 28, 0, 1) ** 1.3

    bass = v[:, :2].mean(1)
    kick = np.clip(np.diff(bass, prepend=bass[:1]) * 4, 0, 1)
    v = np.column_stack([v, kick])
    fall = np.array([0.06] * NB + [0.08], dtype=np.float32)   # bars snap up, ease down
    out = np.empty_like(v)
    cur = np.zeros(NB + 1, dtype=np.float32)
    for i in range(len(v)):
        cur = np.maximum(v[i], cur - fall)
        out[i] = cur
    return (out * 255).astype(np.uint8)


def _safe(track_id):
    return re.sub(r"[^\w.-]", "_", track_id)


class Analyzer(threading.Thread):
    """Background worker. want(current, upcoming) sets what to prepare;
    get(track_id) returns the timeline once it is ready."""

    def __init__(self):
        super().__init__(daemon=True)
        os.makedirs(CACHE, exist_ok=True)
        self.ready = {}
        self.wanted = []
        self.cv = threading.Condition()
        self.enabled = bool(FFMPEG)
        if not self.enabled:
            _log("beat sync disabled: ffmpeg not found")

    def want(self, current, upcoming=()):
        with self.cv:
            self.wanted = [t for t in [current, *upcoming] if t and t.get("kind") == "track"]
            self.cv.notify()

    def get(self, track_id):
        return self.ready.get(track_id)

    def _next(self):
        for t in self.wanted:
            if t["id"] in self.ready:
                continue
            miss = os.path.join(CACHE, _safe(t["id"]) + ".miss")
            if os.path.exists(miss) and time.time() - os.path.getmtime(miss) < MISS_RETRY:
                continue
            return t
        return None

    def run(self):
        while True:
            with self.cv:
                while not self.enabled or (track := self._next()) is None:
                    self.cv.wait(timeout=30)
            path = os.path.join(CACHE, _safe(track["id"]))
            try:
                if os.path.exists(path + ".npy"):
                    data = np.load(path + ".npy")
                else:
                    data = self._build(track)
                    if data is None:
                        open(path + ".miss", "w").close()
                        _log(f"beat sync: no matching audio for {track['title']} - {track['artist']}")
                        continue
                    np.save(path + ".npy", data)
                self.ready[track["id"]] = data
                for old in list(self.ready)[:-12]:      # keep memory bounded
                    del self.ready[old]
            except Exception as e:
                _log(f"beat sync failed for {track['title']}: {e}")
                time.sleep(20)

    def _build(self, track):
        started = time.monotonic()
        seconds = track["duration_ms"] / 1000
        for vid in _candidates(track):
            pcm = _fetch_pcm(vid, seconds)
            if pcm is not None:
                data = analyse(pcm)
                _log(f"beat sync ready: {track['title']} ({time.monotonic() - started:.0f}s, youtube {vid})")
                return data
        return None
