"""Small web control panel served by the daemon: status plus live settings."""
import http.server
import json
import threading
import time

# key: (type, min, max). Only these can be changed from the page.
SETTINGS = {
    "enabled": (bool, None, None),
    "sync_offset_ms": (int, -2000, 2000),
    "brightness": (int, 1, 3),
    "beat_sync": (bool, None, None),
    "art_pulse": (bool, None, None),
    "pause_timeout": (int, 0, 3600),
    "poll_seconds": (int, 3, 60),      # floor: faster polling gets the app banned by Spotify
    "fps": (int, 5, 30),
}

PAGE = r"""<!doctype html><html lang="en"><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>AniMe Matrix</title>
<style>
:root{color-scheme:dark;--bg:#0e0f12;--card:#17191e;--line:#262a31;--fg:#e8eaed;--dim:#8b929c;--acc:#1ed760}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--fg);font:15px/1.45 system-ui,sans-serif}
main{max-width:520px;margin:0 auto;padding:20px 16px 40px}
h1{font-size:18px;margin:0 0 14px;font-weight:600}
.card{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:16px;margin-bottom:14px}
#title{font-size:17px;font-weight:600;overflow-wrap:anywhere}#artist{color:var(--dim)}
#bar{height:4px;background:var(--line);border-radius:2px;margin:12px 0 8px;overflow:hidden}
#fill{height:100%;width:0;background:var(--acc)}
#meta{color:var(--dim);font-size:13px}#warn{color:#ffb454;font-size:13px;margin-top:8px}
.row{display:flex;align-items:center;justify-content:space-between;gap:12px;padding:11px 0;border-top:1px solid var(--line)}
.row:first-child{border-top:0;padding-top:0}.row:last-child{padding-bottom:0}
.row small{display:block;color:var(--dim);font-size:12.5px}
.ctl{display:flex;align-items:center;gap:6px;flex:none}
button{background:#23262d;color:var(--fg);border:1px solid var(--line);border-radius:8px;padding:7px 11px;font:inherit;cursor:pointer}
button:active{background:#2d313a}button.on{background:var(--acc);color:#000;border-color:var(--acc)}
input[type=number]{width:76px;background:#0e0f12;color:var(--fg);border:1px solid var(--line);border-radius:8px;padding:7px 8px;font:inherit;text-align:right}
input[type=checkbox]{width:42px;height:24px;accent-color:var(--acc)}
</style>
<main>
<h1>AniMe Matrix &middot; Spotify</h1>
<div class="card"><div id="title">&hellip;</div><div id="artist"></div>
<div id="bar"><div id="fill"></div></div><div id="meta"></div><div id="warn"></div></div>
<div class="card">
<div class="row"><div>Display<small>Turn the lid display on or off</small></div><div class="ctl"><input type="checkbox" data-k="enabled"></div></div>
<div class="row"><div>Bar timing<small>Minus if bars are late, plus if early &middot; <span id="off"></span> ms</small></div>
<div class="ctl"><button data-step="-50">&minus;50</button><input type="number" step="10" data-k="sync_offset_ms"><button data-step="50">+50</button></div></div>
<div class="row"><div>Brightness</div><div class="ctl" id="bri"><button data-b="1">Low</button><button data-b="2">Med</button><button data-b="3">High</button></div></div>
<div class="row"><div>Beat sync<small>Real spectrum instead of ambient bars</small></div><div class="ctl"><input type="checkbox" data-k="beat_sync"></div></div>
<div class="row"><div>Album art pulse<small>Art pulses with the bass; lowers frame rate</small></div><div class="ctl"><input type="checkbox" data-k="art_pulse"></div></div>
<div class="row"><div>Dark after pause<small>Seconds a paused track stays lit</small></div><div class="ctl"><input type="number" min="0" max="3600" data-k="pause_timeout"></div></div>
<div class="row"><div>Spotify check interval<small>Seconds; lower notices skips sooner but risks a ban (min 3)</small></div><div class="ctl"><input type="number" min="3" max="60" data-k="poll_seconds"></div></div>
<div class="row"><div>Frame rate<small>Target frames per second (panel tops out near 15)</small></div><div class="ctl"><input type="number" min="5" max="30" data-k="fps"></div></div>
</div></main>
<script>
const $=s=>document.querySelector(s),all=s=>document.querySelectorAll(s);let cfg={};
const fmt=s=>Math.floor(s/60)+":"+String(Math.floor(s%60)).padStart(2,"0");
async function save(k,v){const r=await fetch("api/config",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({[k]:v})});
  if(!r.ok)alert(await r.text());await load(true)}
async function load(force){let s;try{s=await(await fetch("api/state")).json()}catch(e){$("#warn").textContent="Service not reachable";return}
  const t=s.track;$("#title").textContent=t?t.title:"Nothing playing";$("#artist").textContent=t?t.artist:"";
  $("#fill").style.width=t&&t.duration?Math.min(100,100*t.position/t.duration)+"%":"0";
  $("#meta").textContent=t?[t.playing?"Playing":"Paused",t.device&&"on "+t.device,fmt(t.position)+" / "+fmt(t.duration),t.beats?"beat sync on":(s.config.beat_sync?"beat sync loading or unavailable":"")].filter(Boolean).join(" · "):"";
  $("#warn").textContent=[s.message,s.matrix?"":"Matrix not connected"].filter(Boolean).join(" · ");
  cfg=s.config;$("#off").textContent=cfg.sync_offset_ms;
  all("[data-k]").forEach(e=>{if(document.activeElement===e&&!force)return;e.type=="checkbox"?e.checked=cfg[e.dataset.k]:e.value=cfg[e.dataset.k]});
  all("#bri button").forEach(b=>b.classList.toggle("on",+b.dataset.b===cfg.brightness))}
all("[data-k]").forEach(e=>e.addEventListener("change",()=>save(e.dataset.k,e.type=="checkbox"?e.checked:+e.value)));
all("[data-step]").forEach(b=>b.onclick=()=>save("sync_offset_ms",cfg.sync_offset_ms+ +b.dataset.step));
all("#bri button").forEach(b=>b.onclick=()=>save("brightness",+b.dataset.b));
load(true);setInterval(load,1000);
</script></html>"""


def serve(host, port, poller, load_config, set_config, matrix_ok, log):
    class Handler(http.server.BaseHTTPRequestHandler):
        def _send(self, code, body, ctype="application/json"):
            body = body.encode() if isinstance(body, str) else body
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            if self.path in ("/", "/index.html"):
                return self._send(200, PAGE, "text/html; charset=utf-8")
            if self.path != "/api/state":
                return self._send(404, "not found", "text/plain")
            st, cfg, track = poller.state, poller.cfg, None
            if st:
                track = {
                    "title": st["title"], "artist": st["artist"], "device": st["device"],
                    "playing": st["playing"], "duration": st["duration_ms"] / 1000,
                    "position": max(0, poller.position(time.monotonic())),
                    "beats": poller.analyzer.get(st["id"]) is not None,
                }
            self._send(200, json.dumps({
                "track": track, "message": poller.message, "matrix": matrix_ok(),
                "config": {k: cfg[k] for k in SETTINGS},
            }))

        def do_POST(self):
            # JSON only: a page on another site cannot send this without a
            # preflight, which is never answered
            if self.path != "/api/config" or self.headers.get("Content-Type") != "application/json":
                return self._send(400, "bad request", "text/plain")
            try:
                data = json.loads(self.rfile.read(min(int(self.headers.get("Content-Length", 0)), 4096)))
                clean = {}
                for k, v in data.items():
                    kind, lo, hi = SETTINGS[k]
                    if kind is bool:
                        if not isinstance(v, bool):
                            raise ValueError(k)
                    else:
                        v = int(v)
                        if not lo <= v <= hi:
                            raise ValueError(f"{k} must be between {lo} and {hi}")
                    clean[k] = v
            except (KeyError, ValueError, TypeError, AttributeError) as e:
                return self._send(400, f"invalid setting: {e}", "text/plain")
            for k, v in clean.items():
                set_config(k, str(v).lower() if isinstance(v, bool) else str(v))
            poller.cfg = load_config()      # applies on the next frame
            self._send(200, "{}")

        def log_message(self, *a):
            pass

    try:
        server = http.server.ThreadingHTTPServer((host, port), Handler)
    except OSError as e:
        log(f"control panel not started on {host}:{port}: {e}")
        return
    threading.Thread(target=server.serve_forever, daemon=True).start()
    log(f"control panel on http://{host}:{port}/")
