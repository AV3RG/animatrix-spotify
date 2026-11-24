"""Packet layouts must match the panel firmware byte for byte (led-arcade lib/core/src/music/link.h)."""
import socket
import struct
import unittest
from unittest import mock

import numpy as np
from PIL import Image

from animatrix_spotify import panel


class Packets(unittest.TestCase):
    def test_beat_layout(self):
        p = panel.beat_packet(True, True, 0xA1B2C3D4, 0.5, [255, 10, 20, 30, 40, 50, 60, 70, 80], 77)
        self.assertEqual(21, len(p))
        self.assertEqual(b"LA\x01\x01", p[:4])
        self.assertEqual(3, p[4])
        self.assertEqual(0xA1B2C3D4, struct.unpack("<I", p[5:9])[0])
        self.assertEqual(5000, struct.unpack("<H", p[9:11])[0])
        self.assertEqual(bytes([255, 10, 20, 30, 40, 50, 60, 70, 80]), p[11:20])
        self.assertEqual(77, p[20])

    def test_beat_clamps(self):
        p = panel.beat_packet(False, False, 1, 1.7, [300, -5] + [0] * 7, 999)
        self.assertEqual(0, p[4])
        self.assertEqual(10000, struct.unpack("<H", p[9:11])[0])
        self.assertEqual(255, p[11])
        self.assertEqual(0, p[12])
        self.assertEqual(255, p[20])
        self.assertEqual(0, struct.unpack("<H", panel.beat_packet(True, True, 1, -0.2, [0] * 9, 0)[9:11])[0])

    def test_meta_layout_and_text(self):
        p = panel.meta_packet(7, "Midnight City", "M83")
        self.assertEqual(b"LA\x01\x02" + struct.pack("<I", 7) + b"\x0dMidnight City\x03M83", p)
        # accents folded to ASCII, other scripts dropped, long text cut to 48 bytes
        self.assertEqual("Cafe del Mar", panel.panel_text("Café del Mar"))
        self.assertEqual("Bjork", panel.panel_text("Björk"))
        self.assertEqual("", panel.panel_text("東京"))
        long = panel.meta_packet(1, "x" * 80, "y")
        self.assertEqual(48, long[8])
        self.assertEqual(4 + 4 + 1 + 48 + 1 + 1, len(long))

    def test_art_layout(self):
        img = Image.linear_gradient("L").resize((124, 124))
        pixels = panel.art16(img)
        self.assertEqual(256, len(pixels))
        self.assertLess(pixels[0], pixels[255])  # top dark, bottom bright, row-major
        p = panel.art_packet(9, pixels)
        self.assertEqual(b"LA\x01\x03" + struct.pack("<I", 9) + pixels, p)

    def test_word_layout(self):
        p = panel.word_packet(7, 3, 450, "Midnight")
        self.assertEqual(b"LA\x01\x04" + struct.pack("<IHH", 7, 3, 450) + b"\x08Midnight", p)
        none = panel.word_packet(7, None, 0, "")
        self.assertEqual(0xFFFF, struct.unpack("<H", none[8:10])[0])
        self.assertEqual(0, none[12])
        self.assertEqual(24, panel.word_packet(1, 0, 99999, "x" * 40)[12])  # cut to 24 bytes
        self.assertEqual(65535, struct.unpack("<H", panel.word_packet(1, 0, 99999, "x")[10:12])[0])
        self.assertEqual(b"Cafe", panel.word_packet(1, 0, 1, "Café")[13:])

    def test_track_key(self):
        a = panel.track_key("spotify:track:abc")
        self.assertEqual(a, panel.track_key("spotify:track:abc"))
        self.assertNotEqual(a, panel.track_key("spotify:track:abd"))
        self.assertNotEqual(0, panel.track_key(""))


class FakeAnalyzer:
    def __init__(self, beats):
        self.beats = beats

    def get(self, track_id):
        return self.beats


class FakeScene:
    def __init__(self, art):
        self.art = art


class FakeLyrics:
    def __init__(self, words):
        self.words = words

    def get(self, track_id):
        return self.words


class FakePoller:
    def __init__(self, cfg, state, art=None, beats=None, words=None):
        self.cfg = cfg
        self.state = state
        self.scene = FakeScene(art) if state else None
        self.analyzer = FakeAnalyzer(beats)
        self.lyrics = FakeLyrics(words)
        self.paused_since = None

    def position(self, now):
        return 60.0


class Sender(unittest.TestCase):
    def setUp(self):
        self.rx = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.rx.bind(("127.0.0.1", 0))
        self.rx.settimeout(1)
        self.cfg = {"panel_host": "127.0.0.1", "panel_port": self.rx.getsockname()[1], "panel_offset_ms": 0,
                    "beat_sync": True, "pause_timeout": 45}

    def tearDown(self):
        self.rx.close()

    def sender(self, poller):
        s = panel.PanelSender(poller, lambda *a: None)
        self.addCleanup(s.close)
        return s

    def packets(self):
        out = []
        self.rx.settimeout(0.3)
        try:
            while True:
                out.append(self.rx.recv(2048))
        except socket.timeout:
            pass
        return out

    def test_new_track_sends_meta_art_and_beat(self):
        beats = np.zeros((100 * 50, 10), dtype=np.uint8)
        beats[:, 0] = 200
        beats[:, 9] = 99
        state = {"id": "spotify:track:abc", "title": "Song", "artist": "Band", "playing": True,
                 "duration_ms": 240000, "progress_ms": 60000}
        poller = FakePoller(self.cfg, state, Image.new("L", (124, 124), 180), beats)
        s = self.sender(poller)
        s.step(1000.0)
        got = {p[3]: p for p in self.packets()}
        self.assertEqual({1, 2, 3}, set(got))
        beat = got[1]
        self.assertEqual(3, beat[4])                  # playing, has beats
        self.assertEqual(200, beat[11])
        self.assertEqual(99, beat[20])
        self.assertEqual(2500, struct.unpack("<H", beat[9:11])[0])  # 60 s of 240 s
        self.assertIn(b"Song", got[2])
        self.assertEqual(180, got[3][8])
        # the next step only sends the beat; meta again after 2 s
        s.step(1000.04)
        self.assertEqual([1], [p[3] for p in self.packets()])
        s.step(1002.1)
        self.assertEqual({1, 2}, {p[3] for p in self.packets()})

    def test_nothing_playing_sends_idle_beat_once_a_second(self):
        s = self.sender(FakePoller(self.cfg, None))
        s.step(10.0)
        s.step(10.5)
        s.step(11.1)
        got = self.packets()
        self.assertEqual(2, len(got))
        self.assertTrue(all(p[3] == 1 and p[4] == 0 for p in got))

    def test_timing_matches_the_lid(self):
        # the panel uses the lid's lead and the owner's sync_offset_ms, plus its own panel_offset_ms
        beats = np.zeros((100 * 50, 10), dtype=np.uint8)
        beats[:, 0] = np.arange(len(beats)) % 256
        state = {"id": "t", "title": "T", "artist": "", "playing": True, "duration_ms": 240000, "progress_ms": 0}
        for sync, extra in ((0, 0), (50, 0), (50, -40)):
            cfg = dict(self.cfg, sync_offset_ms=sync, panel_offset_ms=extra)
            s = self.sender(FakePoller(cfg, state, None, beats))
            s.step(1.0)
            beat = [p for p in self.packets() if p[3] == 1][0]
            want = int((60.0 + panel.LEAD + (sync + extra) / 1000) * 50) % 256
            self.assertEqual(want, beat[11], (sync, extra))
        self.assertGreaterEqual(panel.LEAD, 0.08)

    def test_without_beats_says_so(self):
        state = {"id": "x", "title": "Pod", "artist": "", "playing": True, "duration_ms": 0, "progress_ms": 0}
        s = self.sender(FakePoller(self.cfg, state))
        s.step(5.0)
        beat = [p for p in self.packets() if p[3] == 1][0]
        self.assertEqual(1, beat[4])   # playing, no beat data
        self.assertEqual(0, struct.unpack("<H", beat[9:11])[0])

    def test_long_pause_counts_as_stopped(self):
        state = {"id": "x", "title": "T", "artist": "", "playing": False, "duration_ms": 1000, "progress_ms": 0}
        poller = FakePoller(self.cfg, state)
        poller.paused_since = 100.0
        s = self.sender(poller)
        s.step(200.0)  # paused 100 s, past the 45 s timeout
        got = self.packets()
        self.assertEqual([1], [p[3] for p in got])
        self.assertEqual(0, got[0][4])

    def test_literal_ip_needs_no_lookup(self):
        s = self.sender(FakePoller(self.cfg, None))
        with mock.patch("socket.getaddrinfo", side_effect=OSError("no dns")):
            s.step(1.0)
            s.step(100.0)
        self.assertEqual(2, len(self.packets()))

    def test_failed_lookup_keeps_last_address(self):
        cfg = dict(self.cfg, panel_host="panel.local")
        good = [(socket.AF_INET, socket.SOCK_DGRAM, 0, "", ("127.0.0.1", self.cfg["panel_port"]))]
        logs = []
        s = panel.PanelSender(FakePoller(cfg, None), logs.append)
        self.addCleanup(s.close)
        with mock.patch("socket.getaddrinfo", return_value=good):
            s.step(1.0)
            s.step(70.0)  # re-resolved, same address: not logged again
        with mock.patch("socket.getaddrinfo", side_effect=OSError("mdns hiccup")):
            s.step(140.0)
            s.step(141.0)
        self.assertEqual(4, len(self.packets()))  # never went quiet
        self.assertEqual(1, sum(m.startswith("panel output: sending to") for m in logs))

    def test_sends_the_word_being_sung(self):
        # position is 60 s; the sender looks LEAD ahead
        t = int((60.0 + panel.LEAD) * 1000)
        words = [[0, 1000, "intro"], [t - 100, t + 400, "Waiting"], [t + 400, t + 900, "in"]]
        state = {"id": "w", "title": "T", "artist": "", "playing": True, "duration_ms": 240000, "progress_ms": 0}
        s = self.sender(FakePoller(self.cfg, state, None, None, words))
        s.step(1.0)
        word = [p for p in self.packets() if p[3] == 4][0]
        self.assertEqual(1, struct.unpack("<H", word[8:10])[0])
        self.assertAlmostEqual(400, struct.unpack("<H", word[10:12])[0], delta=2)
        self.assertEqual(b"Waiting", word[13:])

    def test_paused_sends_no_word(self):
        words = [[0, 999999, "held"]]
        state = {"id": "w", "title": "T", "artist": "", "playing": False, "duration_ms": 240000, "progress_ms": 0}
        s = self.sender(FakePoller(self.cfg, state, None, None, words))
        s.step(1.0)
        word = [p for p in self.packets() if p[3] == 4][0]
        self.assertEqual(0xFFFF, struct.unpack("<H", word[8:10])[0])

    def test_no_lyrics_sends_no_word_packets(self):
        state = {"id": "w", "title": "T", "artist": "", "playing": True, "duration_ms": 240000, "progress_ms": 0}
        for words in (None, []):
            s = self.sender(FakePoller(self.cfg, state, None, None, words))
            s.step(1.0)
            self.assertEqual([], [p for p in self.packets() if p[3] == 4])

    def test_disabled_without_host(self):
        cfg = dict(self.cfg, panel_host="")
        s = self.sender(FakePoller(cfg, None))
        s.step(1.0)
        self.assertEqual([], self.packets())


if __name__ == "__main__":
    unittest.main()
