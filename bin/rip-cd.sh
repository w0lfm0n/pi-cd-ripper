#!/bin/bash
# CD rip wrapper. Launched by cd-rip.service (systemd), NOT directly by udev,
# so udev's ~3-min RUN timeout can't kill it — it runs to completion.
STATUS_FILE=/tmp/rip-status.json
LOG_FILE=/var/log/rip-cd.log
HISTORY_FILE=/opt/ripper/history.json

# Deployment config (music path, Plex, notify webhook) — see config.env.example.
[ -f /etc/cd-ripper.env ] && . /etc/cd-ripper.env
export MUSIC_DIR="${MUSIC_DIR:-/mnt/music/Music}"
PLEX_URL="${PLEX_URL:-}"; PLEX_TOKEN="${PLEX_TOKEN:-}"; PLEX_SECTION="${PLEX_SECTION:-1}"
NOTIFY_URL="${NOTIFY_URL:-}"

log(){ echo "[$(date)] $*" >> "$LOG_FILE"; }
# Fire an optional webhook (phone push etc). No-op if NOTIFY_URL is unset.
notify(){ [ -n "$NOTIFY_URL" ] && curl -s -m 8 -X POST "$NOTIFY_URL" -H "Content-Type: application/json" -d "$1" >/dev/null 2>&1; }

update_status(){
    python3 - "$1" <<'PY' 2>/dev/null
import json, sys
try:
    s = json.load(open('/tmp/rip-status.json'))
except Exception:
    s = {}
s.update(json.loads(sys.argv[1]))
open('/tmp/rip-status.json', 'w').write(json.dumps(s))
PY
}

# wait briefly for the drive to be ready after insert
for i in $(seq 1 15); do cd-discid /dev/sr0 >/dev/null 2>&1 && break; sleep 2; done
DISC_RAW=$(cd-discid /dev/sr0 2>/dev/null)
DISCID=$(printf '%s' "$DISC_RAW" | awk '{print $1}')
[ -z "$DISCID" ] && { log "No disc present, aborting"; exit 0; }

echo '{"status":"starting","track":0,"total":0,"artist":"","album":"","mbid":""}' > "$STATUS_FILE"
log "CD inserted ($DISCID) — looking up metadata"

# --- MusicBrainz lookup by TOC (the correct way; cd-discid gives a freedb id, so we
#     build the MB TOC from the track offsets instead) — used for early UI + dedup ---
MBINFO=$(python3 - "$DISC_RAW" <<'PY' 2>/dev/null
import json, sys, urllib.request
raw = sys.argv[1].split()
try:
    ntr = int(raw[1]); offs = raw[2:2+ntr]; totalsec = int(raw[2+ntr])
    leadout = totalsec*75 + 150
    toc = '+'.join(['1', str(ntr), str(leadout)] + offs)
    url = ('https://musicbrainz.org/ws/2/discid/-?toc=' + toc +
           '&fmt=json&inc=artist-credits&cdstubs=no')
    req = urllib.request.Request(url, headers={'User-Agent': 'cd-ripper/1.0 ( pi@home )'})
    d = json.load(urllib.request.urlopen(req, timeout=12))
    rel = d.get('releases', [])
    if rel:
        r = rel[0]; ac = r.get('artist-credit', [])
        print(json.dumps({'mbid': r.get('id',''), 'album': r.get('title',''),
                          'artist': ac[0].get('artist',{}).get('name','') if ac else ''}))
except Exception:
    pass
PY
)

ALREADY=""
if [ -n "$MBINFO" ]; then
    update_status "$MBINFO"
    ARTIST=$(printf '%s' "$MBINFO" | python3 -c 'import json,sys;print(json.load(sys.stdin).get("artist",""))' 2>/dev/null)
    ALBUM=$(printf '%s' "$MBINFO" | python3 -c 'import json,sys;print(json.load(sys.stdin).get("album",""))' 2>/dev/null)
    MBID=$(printf '%s' "$MBINFO" | python3 -c 'import json,sys;print(json.load(sys.stdin).get("mbid",""))' 2>/dev/null)
    log "MusicBrainz: $ARTIST / $ALBUM"
    # --- dedup: skip the rip if the album is already in the library ---
    if [ -n "$ALBUM" ]; then
        ALREADY=$(python3 - "$ARTIST" "$ALBUM" <<'PY' 2>/dev/null
import sys, os, re, glob, subprocess
def norm(s):
    s = re.sub(r'[^a-z0-9]', '', s.lower())
    return re.sub(r'^the', '', s)          # ignore a leading "The" (MB is inconsistent)
artist, album = norm(sys.argv[1]), norm(sys.argv[2])
root = os.environ.get('MUSIC_DIR', '/mnt/music/Music')
def audio(d):
    for x in ('flac', 'mp3', 'm4a'):
        g = sorted(glob.glob(os.path.join(d, '*.' + x)))
        if g: return g[0]
    return None
cands = []
try:
    for e in os.scandir(root):
        if e.is_dir():
            cands.append(e.path)
            try:
                for e2 in os.scandir(e.path):
                    if e2.is_dir(): cands.append(e2.path)
            except OSError: pass
except OSError: pass
hit = ''
# pass 1: fast — album name in the folder name, artist somewhere in the path.
# Only stat for audio on a name match (avoids an SMB listdir on every folder).
for d in cands:
    if album and album in norm(os.path.basename(d)) and (not artist or artist in norm(d)):
        if audio(d): hit = d; break
# pass 2: tag-based — only for folders whose path matches the artist (cheap: few folders).
# Catches flat/oddly-named layouts (e.g. an album dropped straight in the artist folder).
if not hit and artist:
    for d in cands:
        if artist not in norm(d):
            continue
        f = audio(d)
        if not f or not f.endswith('.flac'):
            continue
        try:
            out = subprocess.run(['metaflac', '--show-tag=ALBUM', '--show-tag=ARTIST', f],
                                 capture_output=True, text=True, timeout=5).stdout
        except Exception:
            continue
        talb = tar = ''
        for ln in out.splitlines():
            if ln.startswith('ALBUM='):  talb = norm(ln[6:])
            elif ln.startswith('ARTIST='): tar = norm(ln[7:])
        if album and talb and album in talb and (not artist or artist in tar or artist in norm(d)):
            hit = d; break
print(hit)
PY
)
    fi
fi

if [ -n "$ALREADY" ]; then
    log "Already in library ($ALREADY) — skipping rip"
    update_status "{\"status\":\"exists\",\"artist\":\"$ARTIST\",\"album\":\"$ALBUM\"}"
    notify "{\"event\":\"CD already owned\",\"context\":\"$ARTIST - $ALBUM is already in the music library, so I skipped ripping this disc. Eject and swap it.\"}"
    eject /dev/sr0 2>/dev/null
    rm -rf "/home/pi/abcde.$DISCID" 2>/dev/null
    sleep 2; echo '{"status":"idle"}' > "$STATUS_FILE"
    exit 0
fi

log "Not in library — starting rip ($ARTIST / $ALBUM)"

# --- rip (single run, no timeout — cd-rip.service lets it run as long as needed) ---
# -N = non-interactive (auto-pick the first CDDB/MusicBrainz match); without it,
# under systemd's /dev/null stdin abcde picks "none" -> empty artist/album -> all
# tracks collide into a " - " folder. Do NOT add "</dev/null" (that reintroduces it).
su -l pi -c "abcde -d /dev/sr0 -N -B 2>&1" | while IFS= read -r line; do
    echo "$line" >> "$LOG_FILE"
    if [[ "$line" =~ Grabbing\ track\ 0*([0-9]+) ]]; then
        update_status "{\"status\":\"ripping\",\"track\":${BASH_REMATCH[1]}}"
    elif [[ "$line" =~ Encoding\ track\ 0*([0-9]+)\ of\ 0*([0-9]+) ]]; then
        update_status "{\"status\":\"encoding\",\"track\":${BASH_REMATCH[1]},\"total\":${BASH_REMATCH[2]}}"
    elif [[ "$line" =~ Tagging\ track\ 0*([0-9]+)\ of\ 0*([0-9]+) ]]; then
        update_status "{\"status\":\"tagging\",\"track\":${BASH_REMATCH[1]},\"total\":${BASH_REMATCH[2]}}"
    elif [[ "$line" =~ ^Moving ]]; then
        update_status '{"status":"moving"}'
    fi
done

# --- resolve final metadata from abcde's OWN log (authoritative) + write history ---
META=$(python3 - <<'PY' 2>/dev/null
import json, re
from datetime import datetime
LOG='/var/log/rip-cd.log'; STATUS='/tmp/rip-status.json'; HIST='/opt/ripper/history.json'
lines = open(LOG, errors='replace').read().splitlines()
start = max([i for i, l in enumerate(lines) if 'CD inserted' in l] or [0])
seg = '\n'.join(lines[start:])
artist = album = ''
m = re.search(r'Selected:\s*#\d+\s*\(([^/]+?)\s*/\s*(.+?)\)', seg)
if m:
    artist, album = m.group(1).strip(), m.group(2).strip()
tot = re.findall(r'of\s+0*(\d+):', seg)
total = int(tot[-1]) if tot else 0
try:
    s = json.load(open(STATUS))
except Exception:
    s = {}
artist = artist or s.get('artist', '')
album  = album  or s.get('album', '')
total  = total  or s.get('total', 0)
h = []
try:
    t = open(HIST).read().strip()
    h = json.loads(t) if t else []
except Exception:
    h = []
if not isinstance(h, list):
    h = []
if artist or album:
    h.append({'artist': artist, 'album': album, 'tracks': total,
              'date': datetime.now().isoformat(timespec='seconds')})
    open(HIST, 'w').write(json.dumps(h, indent=2))
print(f"{artist}\t{album}")
PY
)
ARTIST=$(printf '%s' "$META" | cut -f1)
ALBUM=$(printf '%s' "$META" | cut -f2)
[ -z "$ARTIST$ALBUM" ] && { ARTIST="Unknown Artist"; ALBUM="Unknown Album"; }

# --- save cover art into the album folder (UI recent-view + Plex use cover.jpg) ---
DEST="$MUSIC_DIR/$ARTIST - $ALBUM"
[ -d "$DEST" ] || DEST=$(find "$MUSIC_DIR" -maxdepth 2 -name '*.flac' -newermt '-40 minutes' -printf '%h\n' 2>/dev/null | sort -u | head -1)
if [ -n "$DEST" ] && [ -d "$DEST" ]; then
    python3 /usr/local/bin/fetch_cover.py "$DEST" "$MBID" >> "$LOG_FILE" 2>&1
fi

# --- tell Plex to scan the music library so the album + art show up (optional) ---
[ -n "$PLEX_URL" ] && [ -n "$PLEX_TOKEN" ] && \
    curl -s -m 10 "$PLEX_URL/library/sections/$PLEX_SECTION/refresh?X-Plex-Token=$PLEX_TOKEN" >/dev/null 2>&1

notify "{\"event\":\"CD rip complete\",\"context\":\"$ARTIST - $ALBUM has been ripped to FLAC and saved to the music library.\"}"
echo '{"status":"idle"}' > "$STATUS_FILE"
log "Rip complete: $ARTIST - $ALBUM"
