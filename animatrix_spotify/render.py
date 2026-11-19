"""Draws the now-playing frame on a canvas and samples it at each LED.

Canvas units: 10 px per cm of the physical panel, so shapes keep their real
proportions (LED columns are 0.77 cm apart, rows 0.283 cm, odd rows offset).
"""
import io
import math
import zlib

import numpy as np
from PIL import Image, ImageDraw, ImageFilter, ImageFont, ImageOps

from .matrix import ROWS, led_grid

SX, SY = 7.7, 2.83
W, H = round(34.5 * SX), round(ROWS * SY)
_LEDS = led_grid()
_IX = np.clip(np.array([round((x + 0.5) * SX) for x, _ in _LEDS]), 0, W - 1)
_IY = np.clip(np.array([round((y + 0.5) * SY) for _, y in _LEDS]), 0, H - 1)

ART = 124                    # album art square, top right
ART_X = W - ART
TEXT_W = ART_X - 6           # title band, top left (the 12 full-width rows)
TEXT_H = 34
BAR_Y = (129, 136)           # progress bar under the art
BAR_X = (ART_X + 10, W - 4)
BARS = 9                     # must match analysis.NB
BAR_TOP = 40
BAR_PITCH = 1.5 * SX         # two LED half-columns lit, one dark
BAR_RIGHT = ART_X - 0.5 * SX

_FONT_PATHS = (
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
)


def _font(size=24):
    for p in _FONT_PATHS:
        try:
            return ImageFont.truetype(p, size)
        except OSError:
            pass
    return ImageFont.load_default()


def sample(canvas):
    """Canvas (mode L, W x H) -> LED brightness bytes in wire order."""
    arr = np.asarray(canvas.filter(ImageFilter.BoxBlur(1.5)))
    return arr[_IY, _IX].astype(np.uint8).tobytes()


def prepare_art(image_bytes):
    img = Image.open(io.BytesIO(image_bytes)).convert("L")
    img = ImageOps.autocontrast(img, cutoff=1).resize((ART, ART), Image.LANCZOS)
    # deepen the shadows: LEDs look washed out without real blacks
    return img.point([round(255 * (v / 255) ** 1.5) for v in range(256)])


class Scene:
    """Everything needed to draw one track; build once per track change."""

    def __init__(self, track, art=None):
        self.track = track
        self.art = art
        self.font = font = _font()
        self.lyrics = []           # [(start, end, text)], filled in when fetched
        self._line = (None, None)  # cache: (index, rendered image)
        label = track["title"] + (f"  -  {track['artist']}" if track["artist"] else "")
        w = math.ceil(font.getlength(label))
        self.scrolls = w > TEXT_W
        gap = 50
        self.text_w = w + gap
        self.text = Image.new("L", (self.text_w if self.scrolls else TEXT_W, TEXT_H))
        ImageDraw.Draw(self.text).text((0, TEXT_H // 2 + 1), label, font=font, fill=255, anchor="lm")
        seed = zlib.crc32(str(track["id"]).encode())
        rng = np.random.default_rng(seed)
        self.freq = rng.uniform(1.3, 3.4, (BARS, 3))
        self.phase = rng.uniform(0, 6.28, (BARS, 3))

    def _lyric(self, pos):
        """Band image for the lyric line sung at song position `pos`, or None.
        The line slides through the band over the time it is sung."""
        for i, (start, end, text) in enumerate(self.lyrics):
            if start - 0.3 <= pos < end:
                break
        else:
            return None
        if self._line[0] != i:
            img = Image.new("L", (math.ceil(self.font.getlength(text)) + 2, TEXT_H))
            ImageDraw.Draw(img).text((0, TEXT_H // 2 + 1), text, font=self.font, fill=255, anchor="lm")
            self._line = (i, img)
        img = self._line[1]
        band = Image.new("L", (TEXT_W, TEXT_H))
        if img.width <= TEXT_W:
            band.paste(img, ((TEXT_W - img.width) // 2, 0))
        else:
            f = min(max((pos - start) / (end - start), 0), 1)
            band.paste(img, (-round(f * (img.width - TEXT_W)), 0))
        return band

    def frame(self, t, progress, playing=True, level=1.0, bands=None, kick=0.0, pos=None):
        """t: seconds since the scene started. progress: 0..1.
        bands: BARS levels 0..1 from the beat data; kick: 0..1 bass hit."""
        c = Image.new("L", (W, H))
        d = ImageDraw.Draw(c)
        if self.art:
            art = self.art
            if kick:                   # artwork breathes with the kick drum
                gain = 0.72 + 0.28 * kick
                art = art.point([round(v * gain) for v in range(256)])
            c.paste(art, (ART_X, 0))
        else:
            d.rectangle((ART_X, 0, W, ART), outline=90, width=4)

        # title: scrolls right-to-left when it does not fit
        lyric = self._lyric(pos) if pos is not None and self.lyrics else None
        if lyric:
            c.paste(lyric, (0, 0))
        elif self.scrolls:
            off = int(max(0, t - 1.5) * 28) % self.text_w
            band = Image.new("L", (TEXT_W, TEXT_H))
            band.paste(self.text, (-off, 0))
            band.paste(self.text, (self.text_w - off, 0))
            c.paste(band, (0, 0))
        else:
            c.paste(self.text, (0, 0))

        # bars hanging under the title, filling the wedge left of the art;
        # bass sits next to the art where the wedge is deepest. `bands` is the
        # real spectrum at this moment of the song; without it (beat data not
        # ready, podcast, no match) a per-track ambient motion stands in.
        for i in range(BARS):
            if bands is not None:
                v = 0.06 + 0.94 * bands[i]
            elif playing:
                v = np.sin(self.freq[i] * t + self.phase[i]).sum() / 3
                v = 0.2 + 0.8 * (0.5 + 0.5 * v) ** 1.4
            else:
                v = 0.12
            x = BAR_RIGHT - i * BAR_PITCH
            room = (x + 34.65) / 1.36 - BAR_TOP - 4       # distance down to the diagonal edge
            d.rectangle((x - SX + 1.5, BAR_TOP, x - 1.5, BAR_TOP + room * v), fill=230)

        # progress bar
        x0, x1 = BAR_X
        d.rectangle((x0, BAR_Y[0], x1, BAR_Y[1]), fill=38)
        d.rectangle((x0, BAR_Y[0], x0 + (x1 - x0) * min(max(progress, 0), 1), BAR_Y[1]), fill=255)

        if level < 1:
            c = c.point([round(v * level) for v in range(256)])
        return c


def preview(leds, path, scale=4):
    """Save a picture of how the LED values look on the lid."""
    img = Image.new("RGB", (W * scale, H * scale), (8, 8, 10))
    d = ImageDraw.Draw(img)
    r = 1.1 * scale
    for (x, y), v in zip(_LEDS, leds):
        cx, cy = (x + 0.5) * SX * scale, (y + 0.5) * SY * scale
        g = 28 + round(227 * (v / 255) ** 0.6)
        d.ellipse((cx - r, cy - r, cx + r, cy + r), fill=(g, g, g))
    img.save(path)
