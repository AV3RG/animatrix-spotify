import json
import os
import tempfile
import time
import unittest

from animatrix_spotify import lyrics


class Parsing(unittest.TestCase):
    def test_plain_lrc_estimates_words(self):
        lines = lyrics.parse_lrc("[00:12.00]Hello world\n[00:15.50]Second line\n[00:20.00]")
        self.assertEqual(2, len(lines))
        a = lines[0]
        self.assertEqual((12000, 14500), (a["start"], a["end"]))  # capped at 2.5 s (wordglow rule)
        self.assertEqual(["Hello", "world"], [w["text"] for w in a["words"]])
        self.assertEqual(12000, a["words"][0]["start"])
        self.assertLess(a["words"][0]["end"], a["words"][1]["end"])
        self.assertLessEqual(a["words"][1]["end"], 14500)

    def test_enhanced_lrc_keeps_word_times(self):
        lines = lyrics.parse_lrc("[00:01.00]<00:01.00>One <00:01.50>two <00:02.25>three")
        self.assertEqual([1000, 1500, 2250], [w["start"] for w in lines[0]["words"]])
        self.assertEqual(["One", "two", "three"], [w["text"] for w in lines[0]["words"]])
        self.assertEqual(1500, lines[0]["words"][0]["end"])

    def test_lyricsfile_word_sync(self):
        text = ("lines:\n"
                "  - text: Midnight city\n    start_ms: 1000\n    end_ms: 3000\n"
                "    words:\n      - {text: Midnight, start_ms: 1000, end_ms: 1800}\n"
                "      - {text: city, start_ms: 2000, end_ms: 2900}\n")
        lines = lyrics.parse_lyricsfile(text)
        self.assertEqual([(1000, 1800), (2000, 2900)], [(w["start"], w["end"]) for w in lines[0]["words"]])

    def test_blank_stamp_ends_the_line(self):
        # LRCLIB marks breaks with empty [mm:ss] lines; they end the line before (wordglow rule)
        lines = lyrics.parse_lrc("[00:10.00]Hello world\n[00:11.20]\n[00:30.00]Next")
        self.assertEqual(2, len(lines))
        self.assertEqual(11200, lines[0]["end"])
        self.assertLessEqual(lines[0]["words"][-1]["end"], 11200)

    def test_long_line_without_end_is_capped(self):
        lines = lyrics.parse_lrc("[00:00.00]one two\n[01:00.00]later")
        self.assertLess(lines[0]["end"], 5000)  # a line doesn't stay live through a long gap


class Picking(unittest.TestCase):
    def test_prefers_word_sync_and_matching_duration(self):
        recs = [{"id": 1, "duration": 200, "syncedLyrics": "[00:01.00]a"},
                {"id": 2, "duration": 201, "syncedLyrics": "[00:01.00]b", "hasWordSync": True},
                {"id": 3, "duration": 260, "syncedLyrics": "[00:01.00]c", "hasWordSync": True}]
        self.assertEqual(2, lyrics.pick(recs, 200)["id"])
        self.assertIsNone(lyrics.pick([recs[2]], 200))  # a different recording

    def test_non_latin_lyrics_dropped(self):
        self.assertIsNone(lyrics.from_record({"syncedLyrics": "[00:01.00]सजना तेरे बिना\n[00:04.00]दिल लगे ना"}))
        words = lyrics.from_record({"syncedLyrics": "[00:01.00]Sajna tere bina\n[00:04.00]Dil lage na"})
        self.assertEqual("Sajna", words[0][2])
        self.assertIsNone(lyrics.from_record({"instrumental": True}))
        self.assertIsNone(lyrics.from_record(None))

    def test_word_at(self):
        words = [[1000, 1500, "one"], [1500, 2000, "two"], [5000, 5600, "three"]]
        self.assertEqual((0, words[0]), lyrics.word_at(words, 1000))
        self.assertEqual((1, words[1]), lyrics.word_at(words, 1999))
        self.assertIsNone(lyrics.word_at(words, 3000))
        self.assertIsNone(lyrics.word_at(words, 500))
        self.assertEqual(2, lyrics.word_at(words, 5100)[0])
        self.assertIsNone(lyrics.word_at([], 10))


class Store(unittest.TestCase):
    def wait(self, store, track_id, timeout=3):
        end = time.time() + timeout
        while time.time() < end:
            got = store.get(track_id)
            if got is not None:
                return got
            time.sleep(0.02)
        return None

    def test_fetches_once_and_caches_on_disk(self):
        calls = []

        def lookup(track):
            calls.append(track["id"])
            return {"duration": 200, "syncedLyrics": "[00:01.00]Hello there"}

        with tempfile.TemporaryDirectory() as d:
            s = lyrics.LyricsStore(d, lookup)
            s.start()
            track = {"id": "spotify:track:a", "title": "T", "artist": "A", "duration_ms": 200000, "kind": "track"}
            s.want(track)
            self.assertEqual("Hello", self.wait(s, track["id"])[0][2])
            s2 = lyrics.LyricsStore(d, lookup)  # a restart reads the disk cache
            s2.start()
            s2.want(track)
            self.assertEqual("Hello", self.wait(s2, track["id"])[0][2])
            self.assertEqual(1, len(calls))

    def test_lookup_failure_is_a_miss(self):
        def lookup(track):
            raise OSError("lrclib down")

        with tempfile.TemporaryDirectory() as d:
            s = lyrics.LyricsStore(d, lookup)
            s.start()
            track = {"id": "x", "title": "T", "artist": "A", "duration_ms": 1000, "kind": "track"}
            s.want(track)
            self.assertEqual([], self.wait(s, "x"))  # known to have none
            files = os.listdir(d)
            self.assertTrue(any(f.endswith(".lyrics.json") for f in files))
            with open(os.path.join(d, [f for f in files if f.endswith(".lyrics.json")][0])) as f:
                self.assertEqual([], json.load(f)["words"])


if __name__ == "__main__":
    unittest.main()
