"""Panel output: streams what is playing to an LED Arcade panel (16x16 IKEA OBEGRANSAD
on an ESP32, github.com/AV3RG/led-arcade) over UDP, so the panel can show its own
beat-synced visualizer and switch to it while music plays.

Packets ('L' 'A', version 1, type; little endian), matching the firmware's link.h:
  1 beat: flags (bit0 playing, bit1 has beat data) | track u32 | progress u16 0-10000 | bands[9] | kick
  2 meta: track u32 | len | title | len | artist      (ASCII, each <= 48 bytes)
  3 art:  track u32 | 256 bytes, 16x16 greyscale, row-major
"""
import socket
import struct
import threading
import time
import unicodedata
import zlib

from PIL import Image, ImageOps

from . import analysis

MAX_TEXT = 48
FPS = 30
LEAD = 0.05            # seconds: network hop + one panel frame, so the bars land on the beat
META_EVERY = 2.0       # re-sent so a panel that rebooted or lost a packet catches up
ART_EVERY = 10.0
IDLE_EVERY = 1.0
RESOLVE_EVERY = 60.0


def track_key(track_id):
    """Stable non-zero 32-bit id for a Spotify track id (0 means 'no track' on the panel)."""
    return zlib.crc32(str(track_id).encode()) or 1


def panel_text(s):
    """What the panel's 3x5 font can try to draw: accents folded, other scripts dropped."""
    return unicodedata.normalize("NFKD", s or "").encode("ascii", "ignore").decode()


def _clamp(v, lo, hi):
    return max(lo, min(hi, int(round(v))))


def beat_packet(playing, has_beats, track, progress, bands, kick):
    flags = (1 if playing else 0) | (2 if has_beats else 0)
    prog = _clamp(progress * 10000, 0, 10000)
    body = bytes(_clamp(v, 0, 255) for v in list(bands)[:analysis.NB])
    return b"LA\x01\x01" + struct.pack("<BIH", flags, track, prog) + body + bytes([_clamp(kick, 0, 255)])


def meta_packet(track, title, artist):
    t = panel_text(title).encode()[:MAX_TEXT]
    a = panel_text(artist).encode()[:MAX_TEXT]
    return b"LA\x01\x02" + struct.pack("<I", track) + bytes([len(t)]) + t + bytes([len(a)]) + a


def art16(img):
    """Album art (a PIL image, any size) -> 256 brightness bytes for the 16x16 panel."""
    small = ImageOps.autocontrast(img.convert("L").resize((16, 16), Image.BOX), cutoff=1)
    return small.tobytes()


def art_packet(track, pixels):
    return b"LA\x01\x03" + struct.pack("<I", track) + bytes(pixels)


class PanelSender(threading.Thread):
    """Sends the poller's state to `panel_host` about 30 times a second. Independent of the
    lid, so the panel keeps working when the matrix is off or missing. Makes no Spotify calls."""

    def __init__(self, poller, log):
        super().__init__(daemon=True)
        self.poller = poller
        self.log = log
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.addr = None
        self.target = None       # (host, port) the address was resolved for
        self.resolved_at = 0.0
        self.track = None
        self.meta_at = self.art_at = self.idle_at = -1e9
        self.last_err = None

    def close(self):
        self.sock.close()

    def run(self):
        while True:
            started = time.monotonic()
            try:
                self.step(started)
            except Exception as e:  # never take the service down
                self._error(f"panel output failed: {e}")
            time.sleep(max(0.0, 1 / FPS - (time.monotonic() - started)))

    def _error(self, msg):
        if msg != self.last_err:
            self.log(msg)
            self.last_err = msg

    def _address(self, cfg, now):
        target = (cfg.get("panel_host") or "", int(cfg.get("panel_port") or 4210))
        if not target[0]:
            return None
        if target != self.target or now - self.resolved_at > RESOLVE_EVERY:
            self.target, self.resolved_at = target, now
            try:
                self.addr = socket.getaddrinfo(target[0], target[1], socket.AF_INET, socket.SOCK_DGRAM)[0][4]
                self.log(f"panel output: sending to {target[0]} ({self.addr[0]}:{self.addr[1]})")
            except OSError as e:
                self.addr = None
                self._error(f"panel output: cannot find {target[0]}: {e}")
        return self.addr

    def _send(self, addr, packet):
        try:
            self.sock.sendto(packet, addr)
            self.last_err = None
        except OSError as e:
            self._error(f"panel output: send failed: {e}")

    def step(self, now):
        poller = self.poller
        cfg = poller.cfg
        addr = self._address(cfg, now)
        if addr is None:
            return
        st, scene = poller.state, poller.scene
        paused_for = now - poller.paused_since if poller.paused_since else 0
        if not st or not scene or paused_for > cfg.get("pause_timeout", 45):
            self.track = None
            if now - self.idle_at >= IDLE_EVERY:
                self.idle_at = now
                self._send(addr, beat_packet(False, False, 0, 0, [0] * analysis.NB, 0))
            return

        track = track_key(st["id"])
        if track != self.track:
            self.track = track
            self.meta_at = self.art_at = -1e9
        if now - self.meta_at >= META_EVERY:
            self.meta_at = now
            self._send(addr, meta_packet(track, st["title"], st["artist"]))
        if scene.art is not None and now - self.art_at >= ART_EVERY:
            self.art_at = now
            self._send(addr, art_packet(track, art16(scene.art)))

        pos = poller.position(now)
        progress = pos * 1000 / st["duration_ms"] if st["duration_ms"] else 0
        bands, kick = [0] * analysis.NB, 0
        beats = poller.analyzer.get(st["id"]) if cfg.get("beat_sync", True) else None
        has_beats = False
        if beats is not None and st["playing"]:
            i = int((pos + LEAD + cfg.get("panel_offset_ms", 0) / 1000) * analysis.FPS)
            if 0 <= i < len(beats):
                row = beats[i]
                bands, kick, has_beats = row[:analysis.NB], row[analysis.NB], True
        self._send(addr, beat_packet(st["playing"], has_beats, track, progress, bands, kick))
