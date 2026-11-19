import argparse
import os
import signal
import sys
import threading
import time
import tomllib

from . import analysis, render, ui
from .matrix import Matrix
from .spotify import NotLoggedIn, RateLimited, Spotify, fetch_image

CONFIG_DIR = os.path.expanduser("~/.config/animatrix-spotify")
CONFIG = os.path.join(CONFIG_DIR, "config.toml")
DEFAULTS = {
    "client_id": "",
    "port": 8888,
    "enabled": True,          # lid display on/off
    "ui_host": "0.0.0.0",     # control panel address; "127.0.0.1" for this machine only, "" to disable
    "ui_port": 8890,
    "brightness": 2,          # panel brightness 1-3
    "fps": 30,
    "beat_sync": True,        # real spectrum from matching YouTube audio
    "sync_offset_ms": 0,      # raise if the bars lag the music, lower if they lead (live: `offset` command)
    "art_pulse": False,       # pulse the artwork with the bass (costs frame rate)
    "poll_seconds": 5,        # while something is playing (1 s got the app rate limited for hours)
    "idle_poll_seconds": 15,  # while nothing is; 60 after ten idle minutes
    "pause_timeout": 45,      # seconds a paused track stays on the lid
}


def load_config():
    cfg = dict(DEFAULTS)
    try:
        with open(CONFIG, "rb") as f:
            cfg.update(tomllib.load(f))
    except FileNotFoundError:
        pass
    return cfg


def set_config(key, value):
    """Replace or add one `key = value` line in the config file."""
    os.makedirs(CONFIG_DIR, mode=0o700, exist_ok=True)
    lines = []
    if os.path.exists(CONFIG):
        with open(CONFIG) as f:
            lines = [l for l in f.read().splitlines() if l.split("=")[0].strip() != key]
    with open(CONFIG, "w") as f:
        f.write("\n".join(lines + [f"{key} = {value}"]) + "\n")


def log(*a):
    print(*a, file=sys.stderr, flush=True)


def spotify_client(cfg):
    return Spotify(cfg["client_id"], os.path.join(CONFIG_DIR, "token.json"), cfg["port"])


class Poller(threading.Thread):
    """Keeps `state` (latest playback) and `scene` up to date in the background."""

    def __init__(self, cfg):
        super().__init__(daemon=True)
        self.cfg = cfg
        self.state = None      # dict from Spotify.now_playing + "at" (monotonic)
        self.scene = None
        self.scene_started = 0.0
        self.paused_since = None
        self.message = ""      # current problem, shown in the control panel
        self.idle_since = time.monotonic()
        self.queue_due = False
        self.anchor = 0.0      # song position (s) minus monotonic clock, smoothed across polls
        self.analyzer = analysis.Analyzer()
        self.analyzer.start()

    def position(self, now):
        """Best estimate of the song position in seconds."""
        st = self.state
        if not st["playing"]:
            return st["progress_ms"] / 1000
        return self.anchor + now

    def run(self):
        sp, last_msg = None, None
        while True:
            idle = time.monotonic() - self.idle_since > 600
            delay = 60 if idle else self.cfg["idle_poll_seconds"]
            try:
                cfg = self.cfg = load_config()     # live: edits apply without a restart
                if not cfg["client_id"]:
                    raise NotLoggedIn(f"no client_id in {CONFIG} - run: animatrix-spotify login --client-id <id>")
                if sp is None or sp.client_id != cfg["client_id"]:
                    sp = spotify_client(cfg)
                st = sp.now_playing()
                msg = None
                if st:
                    anchor = st["progress_ms"] / 1000 - st["at"]
                    same = self.scene is not None and self.scene.track["id"] == st["id"]
                    # small disagreements are network jitter: ease towards them.
                    # big ones are a seek or a new track: jump.
                    drift = anchor - self.anchor
                    self.anchor = self.anchor + 0.3 * drift if same and abs(drift) < 0.35 else anchor
                    if self.scene is None or self.scene.track["id"] != st["id"]:
                        art = None
                        if st["art_url"]:
                            try:
                                art = render.prepare_art(fetch_image(st["art_url"]))
                            except Exception as e:
                                log("album art failed:", e)
                        self.scene = render.Scene(st, art)
                        self.scene_started = time.monotonic()
                        log(f"now playing: {st['title']} - {st['artist']} (on {st['device']})")
                        if cfg["beat_sync"]:
                            self.analyzer.want(st)
                        self.queue_due = cfg["beat_sync"]
                    elif self.queue_due:
                        # second sighting of a track: it was not skipped, so its
                        # queue is worth a request
                        self.queue_due = False
                        self.analyzer.want(st, sp.queue())
                    if st["playing"]:
                        self.idle_since = time.monotonic()
                        self.paused_since = None
                        delay = self.cfg["poll_seconds"]
                    elif self.paused_since is None:
                        self.paused_since = time.monotonic()
                self.state = st
            except NotLoggedIn as e:
                self.state, msg, sp, delay = None, str(e), None, 15
            except RateLimited as e:
                # dark lid rather than a frozen track
                self.state = None
                msg, delay = f"rate limited by Spotify for {e.retry_after // 60 + 1} more minutes", e.retry_after + 5
            except Exception as e:  # network down, Spotify hiccup: keep last state briefly
                msg, delay = f"Spotify request failed: {e}", 15
                if self.state and time.monotonic() - self.state["at"] > 30:
                    self.state = None
            if msg != last_msg and msg:
                log(msg)
            last_msg = self.message = msg or ""
            st = self.state
            if st and st["playing"] and st["duration_ms"]:
                # look again the moment this track should end, to catch the next one at once
                left = st["duration_ms"] / 1000 - self.position(time.monotonic())
                if left > 0:
                    delay = min(delay, left + 0.25)
            time.sleep(max(delay, 0.2))


def run(cfg):
    signal.signal(signal.SIGTERM, lambda *a: sys.exit(0))
    poller = Poller(cfg)
    poller.start()
    matrix, last_err, lit, last_tick = None, None, False, time.monotonic()
    if cfg["ui_host"]:
        ui.serve(cfg["ui_host"], cfg["ui_port"], poller, load_config, set_config,
                 lambda: matrix is not None, log)
    lag = 0.08                 # seconds a frame takes to reach the LEDs, measured below
    try:
        while True:
            now = time.monotonic()
            resumed = now - last_tick > 20      # woke from suspend: panel needs re-init
            last_tick = now
            cfg = poller.cfg                    # live: the control panel swaps this
            try:
                if matrix is None:
                    matrix = Matrix(cfg["brightness"])
                    log("AniMe Matrix ready on", matrix.path)
                    last_err = None
                elif resumed or matrix.brightness != cfg["brightness"]:
                    matrix.brightness = cfg["brightness"]
                    matrix.init()
                st, scene = poller.state, poller.scene
                paused_for = now - poller.paused_since if poller.paused_since else 0
                if not cfg["enabled"] or not st or not scene or paused_for > cfg["pause_timeout"]:
                    if lit:
                        matrix.clear()
                        lit = False
                    time.sleep(0.5)
                    continue
                t = now - poller.scene_started
                pos = poller.position(now)
                progress = pos * 1000 / st["duration_ms"] if st["duration_ms"] else 0
                level = min(1.0, t / 0.6) * (1.0 if st["playing"] else 0.35)
                bands, kick = None, 0.0
                beats = poller.analyzer.get(st["id"])
                if beats is not None and st["playing"] and cfg["beat_sync"]:
                    # the song's future is known, so draw the moment this frame will
                    # actually be on the LEDs: transfer time plus half the time it stays up
                    i = int((pos + 1.5 * lag + cfg["sync_offset_ms"] / 1000) * analysis.FPS)
                    if 0 <= i < len(beats):
                        row = beats[i] / 255
                        bands, kick = row[:-1], row[-1] if cfg["art_pulse"] else 0.0
                leds = render.sample(scene.frame(t, progress, st["playing"], level, bands, kick))
                sent = time.monotonic()
                matrix.show(leds)
                if bands is not None:
                    lag += 0.1 * (min(time.monotonic() - sent, 0.3) - lag)
                lit = True
                time.sleep(max(0.0, (1 / cfg["fps"] if st["playing"] else 0.5) - (time.monotonic() - now)))
            except OSError as e:
                if str(e) != last_err:
                    hint = " (install the udev rule: see README)" if isinstance(e, PermissionError) else ""
                    log(f"AniMe Matrix unavailable: {e}{hint}")
                    last_err = str(e)
                matrix = None
                time.sleep(10)
    finally:
        if matrix:
            try:
                matrix.close()
            except OSError:
                pass


DEMO = {"id": "demo", "kind": "track", "title": "Midnight City", "artist": "M83", "art_url": None,
        "playing": True, "progress_ms": 80000, "duration_ms": 240000, "device": "demo"}


def demo_scene(art_path=None):
    art = None
    if art_path:
        with open(art_path, "rb") as f:
            art = render.prepare_art(f.read())
    return render.Scene(DEMO, art)


def main():
    ap = argparse.ArgumentParser(prog="animatrix-spotify")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("run", help="run the daemon (what the systemd service does)")
    p = sub.add_parser("login", help="connect your Spotify account")
    p.add_argument("--client-id", help="Client ID of your Spotify developer app (saved to config)")
    sub.add_parser("status", help="print what Spotify reports as playing")
    p = sub.add_parser("offset", help="nudge bar timing live: positive if bars lag the music, negative if they lead")
    p.add_argument("ms", type=int)
    p = sub.add_parser("test", help="play a demo animation on the matrix for a few seconds")
    p.add_argument("--art", help="image file to use as album art")
    p.add_argument("--seconds", type=float, default=8)
    p = sub.add_parser("preview", help="render a demo frame to a PNG (no hardware needed)")
    p.add_argument("out")
    p.add_argument("--art")
    p.add_argument("--t", type=float, default=3.0)
    p.add_argument("--beats", help=".npy beat file to drive the bars")
    args = ap.parse_args()
    cfg = load_config()

    if args.cmd == "run":
        run(cfg)
    elif args.cmd == "login":
        if args.client_id:
            cfg["client_id"] = args.client_id.strip()
            set_config("client_id", f'"{cfg["client_id"]}"')
        if not cfg["client_id"]:
            sys.exit("Pass --client-id <id> the first time (see README for creating the Spotify app).")
        spotify_client(cfg).login()
        print("Logged in. The service picks this up within ~15 seconds.")
    elif args.cmd == "offset":
        set_config("sync_offset_ms", str(args.ms))
        print(f"sync_offset_ms = {args.ms} (applies within a second)")
    elif args.cmd == "status":
        try:
            st = spotify_client(cfg).now_playing()
        except NotLoggedIn as e:
            sys.exit(str(e))
        except RateLimited as e:
            sys.exit(f"rate limited by Spotify for {e.retry_after // 60 + 1} more minutes")
        print("nothing playing" if not st else
              f"{'playing' if st['playing'] else 'paused'}: {st['title']} - {st['artist']} on {st['device']}")
    elif args.cmd == "test":
        scene, m = demo_scene(args.art), Matrix(cfg["brightness"])
        start = time.monotonic()
        try:
            while (t := time.monotonic() - start) < args.seconds:
                m.show(render.sample(scene.frame(t, t / args.seconds, True, min(1, t / 0.6))))
                time.sleep(1 / cfg["fps"])
        finally:
            m.close()
    elif args.cmd == "preview":
        bands, kick = None, 0.0
        if args.beats:
            import numpy as np
            row = np.load(args.beats)[int(args.t * analysis.FPS)] / 255
            bands, kick = row[:-1], row[-1]
        render.preview(render.sample(demo_scene(args.art).frame(args.t, 0.33, True, 1.0, bands, kick)), args.out)


if __name__ == "__main__":
    main()
