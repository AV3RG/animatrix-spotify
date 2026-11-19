"""Minimal Spotify Web API client: PKCE login + current playback state."""
import base64
import hashlib
import http.server
import json
import os
import secrets
import time
import urllib.error
import urllib.parse
import urllib.request
import webbrowser

SCOPES = "user-read-playback-state user-read-currently-playing"
AUTH_URL = "https://accounts.spotify.com/authorize"
TOKEN_URL = "https://accounts.spotify.com/api/token"
PLAYER_URL = "https://api.spotify.com/v1/me/player?additional_types=episode"
QUEUE_URL = "https://api.spotify.com/v1/me/player/queue"


class NotLoggedIn(Exception):
    pass


class RateLimited(Exception):
    def __init__(self, retry_after):
        self.retry_after = retry_after


def _post_form(url, fields):
    req = urllib.request.Request(url, urllib.parse.urlencode(fields).encode())
    with urllib.request.urlopen(req, timeout=15) as r:
        return json.load(r)


class Spotify:
    def __init__(self, client_id, token_path, port=8888):
        self.client_id = client_id
        self.token_path = token_path
        self.redirect_uri = f"http://127.0.0.1:{port}/callback"
        self.port = port
        self.token = None

    # -- token storage ------------------------------------------------------
    def _save(self, tok):
        tok["expires_at"] = time.time() + tok.get("expires_in", 3600) - 60
        if "refresh_token" not in tok and self.token:
            tok["refresh_token"] = self.token["refresh_token"]
        self.token = tok
        fd = os.open(self.token_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            json.dump(tok, f)

    def _access_token(self):
        if self.token is None:
            try:
                with open(self.token_path) as f:
                    self.token = json.load(f)
            except FileNotFoundError:
                raise NotLoggedIn("no Spotify login yet - run: animatrix-spotify login")
        if time.time() >= self.token["expires_at"]:
            try:
                self._save(_post_form(TOKEN_URL, {
                    "grant_type": "refresh_token",
                    "refresh_token": self.token["refresh_token"],
                    "client_id": self.client_id,
                }))
            except urllib.error.HTTPError as e:
                if e.code in (400, 401):
                    self.token = None
                    raise NotLoggedIn("Spotify login expired or revoked - run: animatrix-spotify login")
                raise
        return self.token["access_token"]

    # -- login --------------------------------------------------------------
    def login(self, timeout=300):
        verifier = secrets.token_urlsafe(64)
        challenge = base64.urlsafe_b64encode(
            hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
        state = secrets.token_urlsafe(16)
        url = AUTH_URL + "?" + urllib.parse.urlencode({
            "client_id": self.client_id, "response_type": "code",
            "redirect_uri": self.redirect_uri, "scope": SCOPES, "state": state,
            "code_challenge_method": "S256", "code_challenge": challenge,
        })
        result = {}

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
                if q.get("state", [""])[0] == state:
                    result.update(code=q.get("code", [None])[0], error=q.get("error", [None])[0])
                ok = bool(result.get("code"))
                self.send_response(200 if ok else 400)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.end_headers()
                self.wfile.write(("<h2>AniMe Matrix is connected to Spotify. You can close this tab.</h2>"
                                  if ok else "<h2>Login failed. Check the terminal.</h2>").encode())

            def log_message(self, *a):
                pass

        server = http.server.HTTPServer(("127.0.0.1", self.port), Handler)
        server.timeout = 1
        print("Open this URL in a browser on this laptop and approve access:\n\n" + url + "\n", flush=True)
        try:
            webbrowser.open(url)
        except Exception:
            pass
        deadline = time.time() + timeout
        while not result and time.time() < deadline:
            server.handle_request()
        server.server_close()
        if not result.get("code"):
            raise SystemExit(f"Login failed: {result.get('error') or 'timed out waiting for approval'}")
        self._save(_post_form(TOKEN_URL, {
            "grant_type": "authorization_code", "code": result["code"],
            "redirect_uri": self.redirect_uri, "client_id": self.client_id,
            "code_verifier": verifier,
        }))

    # -- playback -----------------------------------------------------------
    def _get(self, url, _retry=True):
        # Spotify's bans can last hours and every request during one may extend
        # it, so the "banned until" time is kept on disk and honoured by all commands
        ban = self.token_path + ".ratelimit"
        try:
            with open(ban) as f:
                left = float(f.read()) - time.time()
            if left > 0:
                raise RateLimited(int(left))
        except (FileNotFoundError, ValueError):
            pass
        req = urllib.request.Request(url, headers={"Authorization": "Bearer " + self._access_token()})
        try:
            with urllib.request.urlopen(req, timeout=10) as r:
                return json.load(r) if r.status == 200 else None  # 204: no active device
        except urllib.error.HTTPError as e:
            if e.code == 401 and _retry:
                self.token["expires_at"] = 0  # force a refresh
                return self._get(url, _retry=False)
            if e.code == 429:
                wait = int(e.headers.get("Retry-After", "10"))
                with open(ban, "w") as f:
                    f.write(str(time.time() + wait))
                raise RateLimited(wait)
            raise

    @staticmethod
    def _track(item):
        if item.get("type") == "episode":
            artist = (item.get("show") or {}).get("name", "")
            images = item.get("images") or []
        else:
            artist = ", ".join(a["name"] for a in item.get("artists", []))
            images = (item.get("album") or {}).get("images") or []
        # smallest image that is still >= 200px; the matrix has ~1.4k LEDs
        images = sorted(images, key=lambda i: i.get("width") or 0)
        art = next((i["url"] for i in images if (i.get("width") or 0) >= 200),
                   images[-1]["url"] if images else None)
        return {
            "id": item.get("uri") or item.get("name"),
            "kind": item.get("type", "track"),
            "title": item.get("name", ""),
            "artist": artist,
            "art_url": art,
            "duration_ms": item.get("duration_ms") or 0,
        }

    def now_playing(self):
        """Current playback on any device, or None when nothing is active."""
        sent = time.monotonic()
        data = self._get(PLAYER_URL)
        if not data or not data.get("item"):
            return None
        st = self._track(data["item"])
        st.update(
            playing=bool(data.get("is_playing")),
            progress_ms=data.get("progress_ms") or 0,
            at=(sent + time.monotonic()) / 2,   # when that progress was true, give or take
            device=(data.get("device") or {}).get("name", ""),
        )
        return st

    def queue(self, limit=2):
        """The next few tracks, so their beat data can be prepared early."""
        try:
            data = self._get(QUEUE_URL) or {}
        except urllib.error.HTTPError:
            return []
        return [self._track(i) for i in (data.get("queue") or [])[:limit] if i]


def fetch_image(url):
    with urllib.request.urlopen(url, timeout=15) as r:
        return r.read()
