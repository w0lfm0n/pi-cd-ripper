#!/usr/bin/env python3
from flask import Flask, jsonify, Response, request
import json, re, subprocess, glob, os, urllib.request, urllib.error, urllib.parse, threading, time, shutil

app = Flask(__name__)

# All deployment-specific values come from the environment (systemd loads them
# from /etc/cd-ripper.env — see config.env.example). Defaults are safe for a
# fresh checkout; nothing sensitive is hard-coded.
STATUS_FILE  = '/tmp/rip-status.json'
LOG_FILE     = '/var/log/rip-cd.log'
HISTORY_FILE = '/opt/ripper/history.json'
MUSIC_DIR    = os.environ.get('MUSIC_DIR', '/mnt/music/Music')
PLEX_URL     = os.environ.get('PLEX_URL', '')          # e.g. http://plex.local:32400 — blank disables Plex refresh
PLEX_TOKEN   = os.environ.get('PLEX_TOKEN', '')
PLEX_SECTION = os.environ.get('PLEX_SECTION', '1')     # your Music library's section id
NOTIFY_URL   = os.environ.get('NOTIFY_URL', '')        # optional webhook for push notifications

# In-memory cover art cache {mbid: bytes}
cover_cache = {}
# Album art cache {album_dir: (bytes, content_type)}
album_art_cache = {}

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def read_json(path, default):
    try:
        with open(path) as f:
            return json.load(f)
    except:
        return default

def _rip_active():
    """True if a rip is actually running (rip-cd.sh orchestrator or abcde/cdparanoia)."""
    try:
        return subprocess.run(['pgrep', '-f', r'rip-cd\.sh|/usr/bin/abcde|cdparanoia'],
                              capture_output=True).returncode == 0
    except Exception:
        return True  # if we can't tell, don't hide an in-progress rip

def get_status():
    data = read_json(STATUS_FILE, {'status': 'idle'})
    # If the rip script has marked things idle, trust it.
    if data.get('status') == 'idle':
        return {'status': 'idle'}
    # No rip process alive → it finished or died; don't show a frozen "ripping".
    if not _rip_active():
        return {'status': 'idle'}
    # Otherwise derive live progress by parsing the abcde log — reliable, unlike
    # the fragile pipe parser in rip-cd.sh (which never updates the status file).
    live = parse_log_status()
    if live:
        if live.get('status') == 'idle':
            return {'status': 'idle'}
        for k in ('artist', 'album', 'mbid'):
            if not live.get(k) and data.get(k):
                live[k] = data[k]
        if not live.get('artist') or not live.get('album'):
            info = find_album_info_from_abcde()
            if info:
                for k, v in info.items():
                    if not live.get(k):
                        live[k] = v
        return live
    # Nothing parseable yet — still reading TOC / doing the MusicBrainz lookup.
    if data.get('status') not in ('idle', None) and not data.get('artist'):
        info = find_album_info_from_abcde()
        if info:
            data.update(info)
    return data

def parse_log_status():
    """Derive {status, track, total, artist, album} from the abcde output log."""
    try:
        r = subprocess.run(['tail', '-n', '400', LOG_FILE], capture_output=True, text=True)
        lines = r.stdout.splitlines()
    except Exception:
        return None
    total = grab = enc = tag = 0
    moving = complete = False
    artist = album = ''
    for l in lines:
        # Each rip logs a "CD inserted" marker — reset so only the CURRENT rip's
        # lines count (the log tail can still hold the previous rip's progress).
        if 'CD inserted' in l:
            total = grab = enc = tag = 0
            moving = complete = False
            artist = album = ''
            continue
        m = re.search(r'Grabbing entire CD - tracks:\s*([0-9 ]+)', l)
        if m:
            total = max(total, len(m.group(1).split()))
        m = re.search(r'Grabbing track 0*([0-9]+)', l)
        if m:
            grab = max(grab, int(m.group(1)))
        m = re.search(r'Encoding track 0*([0-9]+) of 0*([0-9]+)', l)
        if m:
            enc = max(enc, int(m.group(1)))
            total = max(total, int(m.group(2)))
        m = re.search(r'Tagging track 0*([0-9]+) of 0*([0-9]+)', l)
        if m:
            tag = max(tag, int(m.group(1)))
            total = max(total, int(m.group(2)))
        if l.startswith('Moving ') or 'Moving track' in l:
            moving = True
        if 'Rip complete' in l:
            complete = True
        m = re.search(r'\(Musicbrainz\)\s*\((.+?)\s*/\s*(.+?)\)\s*$', l)
        if m:
            artist, album = m.group(1).strip(), m.group(2).strip()
    if complete:
        return {'status': 'idle'}
    if not (total or grab or enc or tag or moving):
        return None
    cur = grab or enc or tag or 1
    if moving:
        status = 'moving'
    elif total and grab >= total and enc >= total:
        status = 'tagging' if tag < total else 'moving'
    elif total and grab >= total:
        status = 'encoding'
        cur = enc or cur
    else:
        status = 'ripping'
    out = {'status': status, 'track': cur, 'total': total}
    if artist:
        out['artist'] = artist
    if album:
        out['album'] = album
    return out

def find_album_info_from_abcde():
    for d in glob.glob('/tmp/abcde.*') + glob.glob('/home/pi/abcde.*') + glob.glob('/home/*/abcde.*'):
        for path in glob.glob(f'{d}/cddbread.*'):
            try:
                content = open(path).read()
                m = re.search(r'DTITLE=(.+)', content)
                if m:
                    parts = m.group(1).strip().split(' / ', 1)
                    return {'artist': parts[0].strip(),
                            'album':  parts[1].strip() if len(parts) > 1 else ''}
            except:
                pass
    return None

def get_log(n=40):
    try:
        r = subprocess.run(['tail', '-n', str(n), LOG_FILE], capture_output=True, text=True)
        return [l for l in r.stdout.split('\n') if l.strip()][-30:]
    except:
        return []

def get_history():
    return read_json(HISTORY_FILE, [])

def get_recent_albums(limit=8):
    try:
        entries = []
        for name in os.listdir(MUSIC_DIR):
            path = os.path.join(MUSIC_DIR, name)
            if os.path.isdir(path):
                entries.append({'name': name, 'mtime': os.path.getmtime(path)})
        entries.sort(key=lambda x: x['mtime'], reverse=True)
        return [e['name'] for e in entries[:limit]]
    except:
        return []

def get_album_art(album_name):
    if album_name in album_art_cache:
        return album_art_cache[album_name]
    album_path = os.path.realpath(os.path.join(MUSIC_DIR, album_name))
    if not album_path.startswith(os.path.realpath(MUSIC_DIR)):
        return None, None
    # Check for image files — standard names first, then any jpg/png
    for name in ['cover.jpg', 'folder.jpg', 'front.jpg', 'Cover.jpg', 'cover.png']:
        p = os.path.join(album_path, name)
        if os.path.exists(p):
            data = open(p, 'rb').read()
            ctype = 'image/png' if name.endswith('.png') else 'image/jpeg'
            album_art_cache[album_name] = (data, ctype)
            return data, ctype
    # Fall back to any image file in the directory
    for ext, ctype in [('*.jpg', 'image/jpeg'), ('*.jpeg', 'image/jpeg'), ('*.png', 'image/png')]:
        imgs = sorted(glob.glob(os.path.join(album_path, ext)))
        if imgs:
            data = open(imgs[0], 'rb').read()
            album_art_cache[album_name] = (data, ctype)
            return data, ctype
    # Extract from first FLAC file via metaflac
    flacs = glob.glob(os.path.join(album_path, '*.flac'))
    if flacs:
        r = subprocess.run(['metaflac', '--export-picture-to=-', flacs[0]],
                           capture_output=True)
        if r.returncode == 0 and r.stdout:
            album_art_cache[album_name] = (r.stdout, 'image/jpeg')
            return r.stdout, 'image/jpeg'
    # Nested Artist/Album/ layout (Lidarr-organised folders have no cover at the top
    # level) — descend one level and use the first album subfolder's cover / embedded art.
    for sub in sorted(glob.glob(os.path.join(album_path, '*'))):
        if not os.path.isdir(sub):
            continue
        for name in ['cover.jpg', 'folder.jpg', 'front.jpg', 'Cover.jpg', 'cover.png']:
            p = os.path.join(sub, name)
            if os.path.exists(p):
                data = open(p, 'rb').read()
                ctype = 'image/png' if name.endswith('.png') else 'image/jpeg'
                album_art_cache[album_name] = (data, ctype)
                return data, ctype
        subimgs = sorted(glob.glob(os.path.join(sub, '*.jpg')) + glob.glob(os.path.join(sub, '*.png')))
        if subimgs:
            ctype = 'image/png' if subimgs[0].lower().endswith('.png') else 'image/jpeg'
            data = open(subimgs[0], 'rb').read()
            album_art_cache[album_name] = (data, ctype)
            return data, ctype
        subflacs = glob.glob(os.path.join(sub, '*.flac'))
        if subflacs:
            r = subprocess.run(['metaflac', '--export-picture-to=-', subflacs[0]], capture_output=True)
            if r.returncode == 0 and r.stdout:
                album_art_cache[album_name] = (r.stdout, 'image/jpeg')
                return r.stdout, 'image/jpeg'
    album_art_cache[album_name] = (None, None)
    return None, None

def fetch_cover(mbid):
    if not mbid or mbid in cover_cache:
        return
    try:
        url = f'https://coverartarchive.org/release/{mbid}/front-500'
        req = urllib.request.Request(url, headers={'User-Agent': 'cd-ripper/1.0'})
        with urllib.request.urlopen(req, timeout=10) as r:
            cover_cache[mbid] = (r.read(), r.headers.get('Content-Type', 'image/jpeg'))
    except:
        pass

# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@app.route('/')
def index():
    return HTML

@app.route('/api/status')
def api_status():
    status = get_status()
    mbid = status.get('mbid', '')
    if mbid and mbid not in cover_cache:
        threading.Thread(target=fetch_cover, args=(mbid,), daemon=True).start()
    return jsonify({
        'rip':    status,
        'log':    get_log(),
        'has_cover': mbid in cover_cache,
    })

@app.route('/api/cover')
def api_cover():
    mbid = get_status().get('mbid', '')
    if not mbid or mbid not in cover_cache:
        return '', 404
    data, ctype = cover_cache[mbid]
    return Response(data, content_type=ctype)

@app.route('/api/eject', methods=['POST'])
def api_eject():
    result = subprocess.run(['eject', '/dev/sr0'], capture_output=True)
    return jsonify({'ok': result.returncode == 0})

@app.route('/api/plex-refresh', methods=['POST'])
def api_plex_refresh():
    if not PLEX_URL or not PLEX_TOKEN:
        return jsonify({'ok': False, 'error': 'Plex not configured'})
    try:
        url = f'{PLEX_URL}/library/sections/{PLEX_SECTION}/refresh?X-Plex-Token={PLEX_TOKEN}'
        urllib.request.urlopen(url, timeout=5)
        return jsonify({'ok': True})
    except Exception as e:
        return jsonify({'ok': False, 'error': str(e)})

@app.route('/api/history')
def api_history():
    return jsonify(get_history())

@app.route('/api/recent')
def api_recent():
    return jsonify(get_recent_albums())

@app.route('/api/album-art')
def api_album_art():
    album = request.args.get('album', '')
    if not album:
        return '', 404
    data, ctype = get_album_art(album)
    if data:
        return Response(data, content_type=ctype,
                        headers={'Cache-Control': 'public, max-age=3600'})
    return '', 404

# ---------------------------------------------------------------------------
# Vinyl (turntable → tagged FLAC)
# ---------------------------------------------------------------------------
VINYL_STATUS   = '/tmp/vinyl-status.json'
VINYL_SESSION  = '/tmp/vinyl-session.json'
VINYL_WORK     = '/home/pi/vinyl-work'
RECORD_SCRIPT  = '/usr/local/bin/record-side.sh'
PROCESS_SCRIPT = '/usr/local/bin/process-vinyl.sh'
MB_UA = {'User-Agent': 'cd-ripper-vinyl/1.0 ( pi@home )'}

def _record_active():
    try:
        return subprocess.run(['pgrep', '-f', 'arecord'], capture_output=True).returncode == 0
    except Exception:
        return False

def _mb_get(url):
    req = urllib.request.Request(url, headers=MB_UA)
    with urllib.request.urlopen(req, timeout=12) as r:
        return json.load(r)

def find_capture_device():
    """First USB audio capture card from `arecord -l`, or None."""
    try:
        out = subprocess.run(['arecord', '-l'], capture_output=True, text=True).stdout
    except Exception:
        return None
    for line in out.splitlines():
        m = re.match(r'card (\d+):\s*(\S+)\s*\[([^\]]+)\]', line)
        if m and re.search(r'USB|CODEC|LP60|Audio', line, re.I):
            return {'card': int(m.group(1)), 'id': m.group(2), 'name': m.group(3).strip()}
    return None

@app.route('/api/vinyl/status')
def api_vinyl_status():
    st   = read_json(VINYL_STATUS, {'status': 'idle'})
    sess = read_json(VINYL_SESSION, {})
    phase = st.get('status', 'idle')
    # A finished side leaves a stale "recording" once arecord has exited.
    if phase == 'recording' and not _record_active():
        phase = 'ready'
    sid = sess.get('sid')
    sides = len(glob.glob(os.path.join(VINYL_WORK, str(sid), 'side*.wav'))) if sid else 0
    return jsonify({
        'phase': phase,
        'level': st.get('level', 0),
        'elapsed': st.get('elapsed', 0),
        'side': st.get('side'),
        'message': st.get('message', ''),
        'recording': _record_active(),
        'sides_recorded': sides,
        'session': {k: sess.get(k) for k in ('artist', 'album', 'year', 'mbid', 'tracks', 'sid')} if sess else {},
    })

@app.route('/api/vinyl/devices')
def api_vinyl_devices():
    return jsonify({'device': find_capture_device()})

@app.route('/api/vinyl/search')
def api_vinyl_search():
    q = request.args.get('q', '').strip()
    if not q:
        return jsonify({'results': []})
    try:
        url = ('https://musicbrainz.org/ws/2/release/?query='
               + urllib.parse.quote(q) + '&fmt=json&limit=8')
        data = _mb_get(url)
    except Exception as e:
        return jsonify({'results': [], 'error': str(e)})
    out = []
    for r in data.get('releases', []):
        ac = r.get('artist-credit', [])
        artist = ac[0].get('name') if ac else (r.get('artist-credit-phrase') or '')
        tc = sum(m.get('track-count', 0) for m in r.get('media', []))
        fmt = (r.get('media', [{}])[0].get('format') if r.get('media') else '') or ''
        out.append({'mbid': r.get('id'), 'album': r.get('title'), 'artist': artist,
                    'year': (r.get('date', '') or '')[:4], 'tracks': tc,
                    'country': r.get('country', ''), 'format': fmt})
    return jsonify({'results': out})

@app.route('/api/vinyl/select', methods=['POST'])
def api_vinyl_select():
    body = request.get_json(force=True, silent=True) or {}
    mbid = body.get('mbid', '')
    if not mbid:
        return jsonify({'ok': False, 'error': 'mbid required'}), 400
    try:
        data = _mb_get(f'https://musicbrainz.org/ws/2/release/{mbid}'
                       '?inc=recordings+artist-credits&fmt=json')
    except Exception as e:
        return jsonify({'ok': False, 'error': str(e)}), 502
    titles = [t.get('title', '') for m in data.get('media', []) for t in m.get('tracks', [])]
    ac = data.get('artist-credit', [])
    artist = ac[0].get('name') if ac else (body.get('artist') or '')
    sess = {'sid': str(int(time.time())), 'mbid': mbid, 'artist': artist,
            'album': data.get('title', body.get('album', '')),
            'year': (data.get('date', '') or '')[:4], 'date': data.get('date', ''),
            'tracks': titles}
    with open(VINYL_SESSION, 'w') as f:
        json.dump(sess, f)
    with open(VINYL_STATUS, 'w') as f:
        json.dump({'status': 'ready'}, f)
    os.makedirs(os.path.join(VINYL_WORK, sess['sid']), exist_ok=True)
    threading.Thread(target=fetch_cover, args=(mbid,), daemon=True).start()
    return jsonify({'ok': True, 'session': sess})

@app.route('/api/vinyl/start', methods=['POST'])
def api_vinyl_start():
    body = request.get_json(force=True, silent=True) or {}
    side = int(body.get('side', 1))
    sess = read_json(VINYL_SESSION, {})
    if not sess.get('sid'):
        return jsonify({'ok': False, 'error': 'no session — pick an album first'}), 400
    if _record_active():
        return jsonify({'ok': False, 'error': 'already recording'}), 409
    if not find_capture_device():
        return jsonify({'ok': False, 'error': 'no capture device — plug in / power on the turntable'}), 400
    subprocess.Popen([RECORD_SCRIPT, str(sess['sid']), str(side)],
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
    return jsonify({'ok': True, 'side': side})

@app.route('/api/vinyl/stop', methods=['POST'])
def api_vinyl_stop():
    subprocess.run(['pkill', '-INT', '-f', 'arecord'], capture_output=True)
    return jsonify({'ok': True})

@app.route('/api/vinyl/finish', methods=['POST'])
def api_vinyl_finish():
    sess = read_json(VINYL_SESSION, {})
    sid = sess.get('sid')
    if not sid:
        return jsonify({'ok': False, 'error': 'no session'}), 400
    if _record_active():
        subprocess.run(['pkill', '-INT', '-f', 'arecord'], capture_output=True)
        time.sleep(1)
    if not glob.glob(os.path.join(VINYL_WORK, str(sid), 'side*.wav')):
        return jsonify({'ok': False, 'error': 'no sides recorded yet'}), 400
    with open(VINYL_STATUS, 'w') as f:
        json.dump({'status': 'processing', 'message': 'Starting…'}, f)
    subprocess.Popen([PROCESS_SCRIPT, str(sid)],
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
    return jsonify({'ok': True})

@app.route('/api/vinyl/reset', methods=['POST'])
def api_vinyl_reset():
    if _record_active():
        subprocess.run(['pkill', '-INT', '-f', 'arecord'], capture_output=True)
    sess = read_json(VINYL_SESSION, {})
    sid = sess.get('sid')
    if sid:
        shutil.rmtree(os.path.join(VINYL_WORK, str(sid)), ignore_errors=True)
    try:
        os.remove(VINYL_SESSION)
    except Exception:
        pass
    with open(VINYL_STATUS, 'w') as f:
        json.dump({'status': 'idle'}, f)
    return jsonify({'ok': True})

@app.route('/api/vinyl/cover')
def api_vinyl_cover():
    mbid = read_json(VINYL_SESSION, {}).get('mbid', '')
    if mbid and mbid not in cover_cache:
        fetch_cover(mbid)
    if not mbid or mbid not in cover_cache:
        return '', 404
    data, ctype = cover_cache[mbid]
    return Response(data, content_type=ctype)

# ---------------------------------------------------------------------------
# HTML
# ---------------------------------------------------------------------------

HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<title>CD Ripper</title>
<style>
*{box-sizing:border-box;margin:0;padding:0}
body{background:#0d0d0d;color:#e0e0e0;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;min-height:100vh;padding:env(safe-area-inset-top,20px) 16px env(safe-area-inset-bottom,20px)}
.wrap{max-width:520px;margin:0 auto;padding-top:20px}
h1{font-size:1rem;font-weight:600;color:#fff;margin-bottom:16px;display:flex;align-items:center;gap:8px}
/* Cards */
.card{background:#181818;border:1px solid #252525;border-radius:14px;padding:18px;margin-bottom:12px}
/* Status card */
.status-inner{display:flex;gap:16px;align-items:flex-start}
.cover{width:90px;height:90px;border-radius:8px;object-fit:cover;flex-shrink:0;background:#222;display:flex;align-items:center;justify-content:center;font-size:2rem}
.cover img{width:90px;height:90px;border-radius:8px;object-fit:cover}
.status-info{flex:1;min-width:0}
.label{font-size:.68rem;font-weight:500;text-transform:uppercase;letter-spacing:.08em;color:#555;margin-bottom:8px;display:flex;align-items:center;gap:5px}
.dot{width:7px;height:7px;border-radius:50%;flex-shrink:0}
.idle .dot{background:#333} .ripping .dot{background:#3b82f6;animation:blink 1s infinite}
.encoding .dot{background:#f59e0b;animation:blink 1s infinite} .moving .dot{background:#10b981;animation:blink 1s infinite}
.starting .dot{background:#8b5cf6;animation:blink 1s infinite} .tagging .dot{background:#ec4899;animation:blink 1s infinite}
@keyframes blink{0%,100%{opacity:1}50%{opacity:.3}}
.album{font-size:1.05rem;font-weight:700;color:#fff;line-height:1.25;margin-bottom:3px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.artist{font-size:.85rem;color:#888;margin-bottom:12px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.track-row{display:flex;justify-content:space-between;margin-bottom:5px}
.track-label{font-size:.75rem;color:#666} .track-count{font-size:.75rem;color:#888;font-variant-numeric:tabular-nums}
.bar{background:#222;border-radius:99px;height:4px;overflow:hidden}
.fill{height:100%;background:#3b82f6;border-radius:99px;transition:width .6s ease}
.idle-state{color:#444;text-align:center;padding:20px 0;font-size:.9rem}
/* Buttons */
.btn-row{display:flex;gap:8px;margin-top:12px}
.btn{flex:1;border:none;border-radius:8px;padding:9px 12px;font-size:.8rem;font-weight:500;cursor:pointer;transition:opacity .15s}
.btn:active{opacity:.7}
.btn-eject{background:#2a2a2a;color:#aaa}
.btn-plex{background:#1f3a1f;color:#4ade80}
.btn-plex.loading{opacity:.5}
/* Section headers */
.section-head{font-size:.7rem;font-weight:500;text-transform:uppercase;letter-spacing:.08em;color:#555;margin-bottom:10px}
/* Recent grid */
.recent-grid{display:grid;grid-template-columns:repeat(4,1fr);gap:8px}
.recent-item{background:#222;border-radius:8px;overflow:hidden;aspect-ratio:1;position:relative;cursor:default}
.recent-item img{width:100%;height:100%;object-fit:cover;display:block}
.recent-item .no-art{width:100%;height:100%;display:flex;align-items:center;justify-content:center;font-size:1.6rem;background:#1e1e1e}
.recent-item .caption{position:absolute;bottom:0;left:0;right:0;background:linear-gradient(transparent,rgba(0,0,0,.85));padding:16px 6px 5px;font-size:.6rem;color:#ddd;line-height:1.3;text-align:center;overflow:hidden;display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical}
/* History list */
.history-list{display:flex;flex-direction:column;gap:8px}
.history-item{display:flex;justify-content:space-between;align-items:center;gap:8px}
.history-info{min-width:0}
.history-album{font-size:.85rem;color:#ddd;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.history-artist{font-size:.75rem;color:#666;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.history-meta{font-size:.7rem;color:#444;white-space:nowrap;text-align:right}
/* Log */
.log{font-family:'SF Mono',Menlo,monospace;font-size:.7rem;color:#555;line-height:1.6;max-height:160px;overflow-y:auto;word-break:break-all}
.log div:last-child{color:#888}
.footer{font-size:.65rem;color:#2a2a2a;text-align:right;margin-top:6px}
/* Mode tabs */
.tabs{display:flex;gap:8px;margin-bottom:14px}
.tab{flex:1;text-align:center;padding:9px;border-radius:10px;background:#181818;border:1px solid #252525;color:#888;cursor:pointer;font-size:.85rem;font-weight:600;transition:.15s}
.tab.active{background:#1f2a3a;color:#fff;border-color:#3b82f6}
/* Vinyl */
.recording .dot,.processing .dot,.splitting .dot{background:#ef4444;animation:blink 1s infinite}
.tagging .dot{background:#ec4899;animation:blink 1s infinite}
.done .dot{background:#10b981} .ready .dot{background:#3b82f6} .error .dot{background:#ef4444}
.vlevel{background:#222;border-radius:99px;height:9px;overflow:hidden;margin:10px 0 4px}
.vfill{height:100%;background:linear-gradient(90deg,#10b981 0%,#f59e0b 78%,#ef4444 100%);transition:width .25s}
.vmeta{font-size:.75rem;color:#888;display:flex;justify-content:space-between;margin-top:4px}
.vinput{width:100%;padding:11px;border-radius:9px;border:1px solid #333;background:#111;color:#eee;font-size:.9rem}
.vresult{padding:9px 11px;border-radius:9px;background:#1e1e1e;margin-top:7px;cursor:pointer;display:flex;justify-content:space-between;gap:10px;align-items:center}
.vresult:active{opacity:.6}
.vresult .vr-sub{font-size:.72rem;color:#666;white-space:nowrap}
.vsides{font-size:.78rem;color:#8fbf8f;margin:8px 0}
.btn-rec{background:#3a1f1f;color:#f87171}
.btn-stop{background:#7f1d1d;color:#fff}
.btn-finish{background:#1f3a1f;color:#4ade80}
.btn-cancel{background:#2a2a2a;color:#999}
.btn:disabled{opacity:.4;cursor:not-allowed}
.chip{display:inline-block;font-size:.68rem;color:#888;background:#222;border-radius:99px;padding:2px 8px;margin-left:6px}
</style>
</head>
<body>
<div class="wrap">
  <h1 id="title">💿 Ripper</h1>

  <!-- Mode tabs -->
  <div class="tabs">
    <div class="tab active" id="tab-cd" onclick="switchMode('cd')">💿 CD</div>
    <div class="tab" id="tab-vinyl" onclick="switchMode('vinyl')">🎵 Vinyl</div>
  </div>

  <!-- CD panel -->
  <div id="cd-panel">
    <!-- Status -->
    <div class="card" id="status-card">
      <div class="idle-state">Insert a CD to start ripping</div>
    </div>

    <!-- Actions -->
    <div class="card">
      <div class="section-head">Actions</div>
      <div class="btn-row">
        <button class="btn btn-eject" onclick="eject()">⏏ Eject</button>
        <button class="btn btn-plex" id="plex-btn" onclick="plexRefresh()">▶ Refresh Plex</button>
      </div>
    </div>
  </div>

  <!-- Vinyl panel (rendered by renderVinyl) -->
  <div id="vinyl-panel" style="display:none">
    <div class="card"><div id="vinyl-body"><div class="idle-state">🎵 Loading…</div></div></div>
  </div>

  <!-- Recently ripped from NAS -->
  <div class="card">
    <div class="section-head">Recently Added</div>
    <div class="recent-grid" id="recent-grid"><div style="color:#333;font-size:.8rem">Loading…</div></div>
  </div>

  <!-- Rip history -->
  <div class="card">
    <div class="section-head">Rip History</div>
    <div class="history-list" id="history-list"><div style="color:#333;font-size:.8rem">No rips yet</div></div>
  </div>

  <!-- Log -->
  <div class="card">
    <div class="section-head">Activity Log</div>
    <div class="log" id="log"><div style="color:#333">No activity yet</div></div>
  </div>

  <div class="footer" id="footer"></div>
</div>

<script>
const SM = {idle:'Idle',starting:'Starting',ripping:'Ripping',encoding:'Encoding FLAC',moving:'Saving to NAS',tagging:'Tagging',
            recording:'Recording',ready:'Ready',processing:'Processing',splitting:'Splitting tracks',done:'Done',error:'Error'};
let lastStatus = 'idle';
let mode = 'cd';
let vsearch = [];
let vLastKey = '';   // structural view signature — only full-rebuild when it changes

function esc(s){ return (s||'').replace(/</g,'&lt;'); }
function fmtTime(s){ s=s||0; const m=Math.floor(s/60), ss=s%60; return m+':'+String(ss).padStart(2,'0'); }

function switchMode(m){
  mode = m;
  document.getElementById('tab-cd').classList.toggle('active', m==='cd');
  document.getElementById('tab-vinyl').classList.toggle('active', m==='vinyl');
  document.getElementById('cd-panel').style.display = m==='cd' ? '' : 'none';
  document.getElementById('vinyl-panel').style.display = m==='vinyl' ? '' : 'none';
  document.getElementById('title').textContent = m==='cd' ? '💿 Ripper' : '🎵 Vinyl';
  if (m==='vinyl'){ vLastKey=''; vinylTick(); }
}

function fmtDate(iso) {
  if (!iso) return '';
  const d = new Date(iso);
  return d.toLocaleDateString(undefined, {day:'numeric',month:'short'});
}

async function tick() {
  try {
    const r = await fetch('/api/status');
    const {rip, log, has_cover} = await r.json();
    const s = rip.status || 'idle';
    const card = document.getElementById('status-card');

    if (s === 'idle') {
      card.className = 'card idle';
      card.innerHTML = '<div class="idle-state">💿 Insert a CD to start ripping</div>';
    } else {
      const pct = rip.total ? Math.round(rip.track / rip.total * 100) : 0;
      const coverHtml = has_cover
        ? `<div class="cover"><img src="/api/cover?t=${Date.now()}" alt="cover"></div>`
        : `<div class="cover">💿</div>`;
      card.className = `card ${s}`;
      card.innerHTML = `
        <div class="status-inner">
          ${coverHtml}
          <div class="status-info">
            <div class="label"><span class="dot"></span>${SM[s] || s}</div>
            <div class="album">${rip.album || 'Reading disc…'}</div>
            <div class="artist">${rip.artist || ''}</div>
            ${rip.total ? `
              <div class="track-row">
                <span class="track-label">Track</span>
                <span class="track-count">${rip.track} / ${rip.total}</span>
              </div>
              <div class="bar"><div class="fill" style="width:${pct}%"></div></div>
            ` : ''}
          </div>
        </div>`;
    }

    // Log
    const logEl = document.getElementById('log');
    if (log && log.length) {
      logEl.innerHTML = log.map(l => `<div>${l.replace(/</g,'&lt;')}</div>`).join('');
      logEl.scrollTop = logEl.scrollHeight;
    }

    document.getElementById('footer').textContent = 'Updated ' + new Date().toLocaleTimeString();
    lastStatus = s;
  } catch(e) {
    document.getElementById('footer').textContent = 'Reconnecting…';
  }
  setTimeout(tick, 2000);
}

async function loadRecent() {
  try {
    const r = await fetch('/api/recent');
    const albums = await r.json();
    const el = document.getElementById('recent-grid');
    if (!albums.length) { el.innerHTML = '<div style="color:#333;font-size:.8rem">Nothing yet</div>'; return; }
    el.innerHTML = albums.map(a => {
      const enc = encodeURIComponent(a);
      const safe = a.replace(/</g,'&lt;');
      return `<div class="recent-item">
        <img src="/api/album-art?album=${enc}" alt="" onerror="this.style.display='none';this.nextElementSibling.style.display='flex'">
        <div class="no-art" style="display:none">💿</div>
        <div class="caption">${safe}</div>
      </div>`;
    }).join('');
  } catch(e) {}
}

async function loadHistory() {
  try {
    const r = await fetch('/api/history');
    const items = await r.json();
    const el = document.getElementById('history-list');
    if (!items.length) { el.innerHTML = '<div style="color:#333;font-size:.8rem">No rips yet</div>'; return; }
    el.innerHTML = items.slice().reverse().slice(0, 20).map(h => `
      <div class="history-item">
        <div class="history-info">
          <div class="history-album">${(h.album||'Unknown').replace(/</g,'&lt;')}</div>
          <div class="history-artist">${(h.artist||'').replace(/</g,'&lt;')}</div>
        </div>
        <div class="history-meta">${h.tracks ? h.tracks + ' tracks' : ''}<br>${fmtDate(h.date)}</div>
      </div>`).join('');
  } catch(e) {}
}

async function eject() {
  await fetch('/api/eject', {method:'POST'});
}

async function plexRefresh() {
  const btn = document.getElementById('plex-btn');
  btn.classList.add('loading');
  btn.textContent = '⏳ Refreshing…';
  try {
    await fetch('/api/plex-refresh', {method:'POST'});
    btn.textContent = '✓ Done';
    setTimeout(() => { btn.classList.remove('loading'); btn.textContent = '▶ Refresh Plex'; }, 2000);
  } catch(e) {
    btn.textContent = '✗ Failed';
    setTimeout(() => { btn.classList.remove('loading'); btn.textContent = '▶ Refresh Plex'; }, 2000);
  }
}

// ---- Vinyl mode ----
async function vPost(path, body){
  try {
    const r = await fetch(path, {method:'POST', headers:{'Content-Type':'application/json'},
                                 body: body ? JSON.stringify(body) : null});
    return await r.json();
  } catch(e){ return {ok:false, error:'network'}; }
}

async function vSearch(){
  const q = document.getElementById('v-q').value.trim();
  if(!q) return;
  const box = document.getElementById('v-results');
  box.innerHTML = '<div style="color:#555;font-size:.8rem;margin-top:10px">Searching…</div>';
  try{
    const r = await fetch('/api/vinyl/search?q='+encodeURIComponent(q));
    const {results} = await r.json();
    vsearch = results || [];
    if(!vsearch.length){ box.innerHTML='<div style="color:#555;font-size:.8rem;margin-top:10px">No matches</div>'; return; }
    box.innerHTML = vsearch.map((x,i)=>`<div class="vresult" onclick="vSelect(${i})">
        <div style="min-width:0">
          <div style="color:#eee;font-size:.85rem;white-space:nowrap;overflow:hidden;text-overflow:ellipsis">${esc(x.album)}</div>
          <div style="color:#888;font-size:.75rem;white-space:nowrap;overflow:hidden;text-overflow:ellipsis">${esc(x.artist)}</div>
        </div>
        <div class="vr-sub">${x.year||''}${x.tracks?' · '+x.tracks+'tr':''}${x.format?' · '+esc(x.format):''}</div>
      </div>`).join('');
  }catch(e){ box.innerHTML='<div style="color:#a55;font-size:.8rem;margin-top:10px">Search failed</div>'; }
}

async function vSelect(i){
  const x = vsearch[i]; if(!x) return;
  await vPost('/api/vinyl/select', {mbid:x.mbid, artist:x.artist, album:x.album});
  vinylTick();
}
async function vStart(side){ const r = await vPost('/api/vinyl/start', {side}); if(r && r.ok===false && r.error) alert(r.error); vinylTick(); }
async function vStop(){ await vPost('/api/vinyl/stop'); setTimeout(vinylTick, 700); }
async function vFinish(){ const r = await vPost('/api/vinyl/finish'); if(r && r.ok===false && r.error) alert(r.error); vinylTick(); }
async function vReset(){ if(!confirm('Discard this vinyl session?')) return; await vPost('/api/vinyl/reset'); vinylTick(); }

async function vinylTick(){
  if(mode!=='vinyl') return;
  try{ const r = await fetch('/api/vinyl/status'); renderVinyl(await r.json()); }catch(e){}
}

function renderVinyl(v){
  const el = document.getElementById('vinyl-body');
  const card = el.parentElement;
  const s = v.session || {};
  const phase = v.phase;

  // Only rebuild the DOM when the structural state changes — otherwise the 2s
  // poll would wipe the search box (and its focus) while you type. Between
  // rebuilds, just live-update the volatile recording meter/timer.
  const key = `${phase}|${s.mbid||''}|${v.recording?1:0}|${v.sides_recorded||0}`;
  if(key === vLastKey){
    if(v.recording){
      const f = document.getElementById('vfill');   if(f) f.style.width = (v.level||0)+'%';
      const t = document.getElementById('v-elapsed'); if(t) t.textContent = fmtTime(v.elapsed);
    }
    return;
  }
  vLastKey = key;

  if(['processing','splitting','tagging'].includes(phase)){
    card.className = 'card '+phase;
    el.innerHTML = `<div class="label"><span class="dot"></span>${SM[phase]||phase}</div>
      <div class="album">${esc(s.album||'')}</div><div class="artist">${esc(s.artist||'')}</div>
      <div class="track-label" style="margin-top:12px">${esc(v.message||'Working…')}</div>`;
    return;
  }
  if(phase==='done'){
    card.className = 'card done';
    el.innerHTML = `<div class="label"><span class="dot"></span>Done</div>
      <div class="album">${esc(s.album||'Saved')}</div><div class="artist">${esc(s.artist||'')}</div>
      <div class="track-label" style="margin:10px 0">${esc(v.message||'')}</div>
      <div class="btn-row"><button class="btn btn-finish" onclick="vReset()">＋ Record another</button></div>`;
    return;
  }
  if(phase==='error'){
    card.className = 'card error';
    el.innerHTML = `<div class="label"><span class="dot"></span>Error</div>
      <div class="track-label" style="margin:10px 0;color:#f87171">${esc(v.message||'Something went wrong')}</div>
      <div class="btn-row"><button class="btn btn-cancel" onclick="vReset()">Reset</button></div>`;
    return;
  }
  if(!s.mbid){
    card.className = 'card';
    el.innerHTML = `<div class="section-head">Record a record</div>
      <div style="display:flex;gap:8px">
        <input class="vinput" id="v-q" placeholder="Search album — e.g. Nirvana Nevermind" onkeydown="if(event.key==='Enter')vSearch()">
        <button class="btn btn-plex" style="flex:0 0 auto;width:auto;padding:0 14px" onclick="vSearch()">Search</button>
      </div>
      <div id="v-results"></div>`;
    return;
  }
  // album selected → record controls
  const rec = v.recording;
  const nextSide = (v.sides_recorded||0) + 1;
  card.className = 'card ' + (rec ? 'recording' : 'ready');
  const tracks = (s.tracks||[]).length;
  const meter = rec ? `
    <div class="vlevel"><div class="vfill" id="vfill" style="width:${v.level||0}%"></div></div>
    <div class="vmeta"><span>Side ${v.side||nextSide}</span><span id="v-elapsed">${fmtTime(v.elapsed)}</span></div>` : '';
  el.innerHTML = `
    <div class="status-inner">
      <div class="cover"><img src="/api/vinyl/cover?t=${s.mbid}" alt="" onerror="this.parentElement.textContent='🎵'"></div>
      <div class="status-info">
        <div class="label"><span class="dot"></span>${rec?'Recording':'Ready'}${tracks?`<span class="chip">${tracks} tracks</span>`:''}</div>
        <div class="album">${esc(s.album||'')}</div>
        <div class="artist">${esc(s.artist||'')}${s.year?' · '+s.year:''}</div>
        ${meter}
      </div>
    </div>
    <div class="vsides">${v.sides_recorded||0} side(s) recorded — play the record, then Stop at the end of the side.</div>
    <div class="btn-row">
      ${rec ? `<button class="btn btn-stop" onclick="vStop()">⏹ Stop Side ${v.side||nextSide}</button>`
            : `<button class="btn btn-rec" onclick="vStart(${nextSide})">⏺ Record Side ${nextSide}</button>`}
    </div>
    <div class="btn-row">
      <button class="btn btn-cancel" onclick="vReset()">Cancel</button>
      <button class="btn btn-finish" onclick="vFinish()" ${(rec || !(v.sides_recorded>0))?'disabled':''}>✓ Finish &amp; Save${v.sides_recorded?` (${v.sides_recorded})`:''}</button>
    </div>`;
}

tick();
loadRecent();
loadHistory();
// Reload recent + history every 30s
setInterval(() => { loadRecent(); loadHistory(); }, 30000);
setInterval(vinylTick, 2000);   // vinyl poll — no-op unless in Vinyl mode
</script>
</body>
</html>"""

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=8080)
