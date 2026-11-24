# animatrix-spotify

Lights up the AniMe Matrix on the lid of a ROG Zephyrus G14 (GA402) with whatever
your Spotify account is playing, on any device: album art, scrolling title,
progress bar and an ambient bar animation. Paused tracks dim, then the lid goes
dark after 45 s; it stays dark while nothing is playing.

It talks to the matrix directly over USB HID (no asusctl needed) and polls the
Spotify Web API every few seconds.

## Beat sync

The bars are the real spectrum of the song. For each new track the service
finds matching audio on YouTube (yt-dlp), reduces it to a small loudness
timeline, caches that in `~/.cache/animatrix-spotify/` and deletes the audio.
It then shows the frame for Spotify's reported playback position, so it works
whichever device is playing. The next two tracks in your queue are prepared in
advance. Until a track's data is ready (about 15 s the first time it is ever
seen), and for podcasts or tracks with no match, the bars fall back to an
ambient animation.

If the bars run consistently early or late, nudge them live with
`animatrix-spotify offset <ms>` (positive if the bars lag the music, negative if
they lead; try steps of 50). `beat_sync = false`
turns the feature off. A weekly timer (`animatrix-spotify-update.timer`) keeps
yt-dlp current, since YouTube changes often.

## LED Arcade panel

The service can also drive an [LED Arcade](https://github.com/AV3RG/led-arcade)
panel (a 16x16 IKEA OBEGRÄNSAD on an ESP32) on the same network: the panel shows
its own visualizer (album art, scrolling title, beat-synced bars) and switches to
it by itself while music plays, inside the hours set in its app (Settings, Music).
Set the panel's address in the config and the service starts sending within a
second, whether or not the lid is on:

    panel_host = "192.168.1.27"

It sends small UDP packets to port 4210 (`panel_port`) about 30 times a second
while something plays, once a second otherwise, and makes no extra Spotify
requests. The panel's bars follow `sync_offset_ms` too; `panel_offset_ms` nudges
them further if they need it (positive if they lag the music). The panel only listens on the local network, so the laptop has to be
on the same WiFi.

While a panel is set, the service also looks up time-synced lyrics on
[LRCLIB](https://lrclib.net) for each track (word-timed where LRCLIB has it,
otherwise estimated inside each line) and sends the word being sung, so the
panel can flash the lyrics word by word. Only Latin-script lyrics are used,
since the panel's font is ASCII; romanised Hindi or Urdu works, Devanagari
doesn't. Lookups are cached in `~/.cache/animatrix-spotify/`; `lyrics = false`
turns them off.

## Control panel

The service hosts a small web page on port 8890 (`http://<laptop address>:8890/`)
showing what is playing and letting you change, live: display on/off, bar
timing, brightness, beat sync, art pulse, pause timeout, Spotify check interval
and frame rate. It has no password and listens on every network interface; it
can only change those settings. Set `ui_host = "127.0.0.1"` in the config to
restrict it to the laptop itself, or `ui_host = ""` to turn it off.

## Spotify rate limits

Spotify bans apps that poll too often, for up to hours. The service polls every
5 s while playing (plus once at the expected end of each track), every 15 s
when idle and every 60 s after ten idle minutes. If a ban happens the lid goes
dark and the log says how long it lasts.

## Install

Needs [uv](https://docs.astral.sh/uv/), `ffmpeg` and `node` (yt-dlp uses it). The
paths below assume the repo lives at `~/animatrix-spotify`.

    git clone https://github.com/AV3RG/animatrix-spotify ~/animatrix-spotify
    cd ~/animatrix-spotify && uv sync
    cp systemd/* ~/.config/systemd/user/
    systemctl --user daemon-reload
    systemctl --user enable --now animatrix-spotify animatrix-spotify-update.timer
    printf '#!/bin/sh\ncd "$HOME/animatrix-spotify" && exec .venv/bin/python -m animatrix_spotify "$@"\n' > ~/.local/bin/animatrix-spotify && chmod +x ~/.local/bin/animatrix-spotify

## One-time setup

1. Device permission:

       sudo cp ~/animatrix-spotify/70-animatrix.rules /etc/udev/rules.d/
       sudo udevadm control --reload && sudo udevadm trigger --subsystem-match=hidraw

2. Spotify app: at https://developer.spotify.com/dashboard create an app, tick
   "Web API", and add the redirect URI `http://127.0.0.1:8888/callback`.
   (Spotify requires the app owner to have Premium.) Copy its Client ID.

3. Log in: `animatrix-spotify login --client-id <Client ID>` and approve in the browser.

The running service picks both up on its own within about 15 seconds.

On a machine without a browser, open the printed URL elsewhere; when the
redirect to `127.0.0.1:8888` fails there, copy the full address from the
browser and fetch it on the laptop: `curl '<that URL>'`.

## Commands

    animatrix-spotify test                 # demo animation on the lid
    animatrix-spotify status               # what Spotify says is playing
    animatrix-spotify preview out.png      # simulated frame, no hardware
    systemctl --user status animatrix-spotify
    journalctl --user -u animatrix-spotify -f

Settings live in `~/.config/animatrix-spotify/config.toml` (see `DEFAULTS` in
`animatrix_spotify/__main__.py`): `brightness` (1-3), `fps`, `pause_timeout`, ...
Restart the service after changing them.
