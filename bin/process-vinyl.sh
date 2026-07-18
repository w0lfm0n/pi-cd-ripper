#!/bin/bash
# Turn recorded vinyl side WAVs into a tagged FLAC album on the NAS.
# Launched (detached) by the Flask UI /api/vinyl/finish. Runs as pi.
SID="$1"
WORK="/home/pi/vinyl-work/$SID"
STATUS=/tmp/vinyl-status.json
SESSION=/tmp/vinyl-session.json
LOG=/var/log/vinyl.log

# Deployment config (music path, Plex, notify webhook) — see config.env.example.
[ -f /etc/cd-ripper.env ] && . /etc/cd-ripper.env
export MUSIC_DIR="${MUSIC_DIR:-/mnt/music/Music}"
MUSIC="$MUSIC_DIR"
PLEX_URL="${PLEX_URL:-}"; PLEX_TOKEN="${PLEX_TOKEN:-}"; PLEX_SECTION="${PLEX_SECTION:-1}"
NOTIFY_URL="${NOTIFY_URL:-}"
log(){ echo "[$(date)] $*" >> "$LOG"; }

[ -d "$WORK" ] || { echo '{"status":"error","message":"work dir missing"}' > "$STATUS"; exit 1; }
log "=== Processing vinyl session $SID ==="
echo '{"status":"splitting","message":"Splitting sides into tracks…"}' > "$STATUS"

# 1) Split each side WAV on silence into ordered track NNN.wav files.
#    sox silence: trim leading silence, then start a new file after >=1.8s
#    below 0.3% amplitude (the inter-track gap).
rm -f "$WORK"/track*.wav "$WORK"/_split*.wav
i=0
for side in "$WORK"/side*.wav; do
    [ -f "$side" ] || continue
    log "Splitting $(basename "$side")"
    sox "$side" "$WORK/_split.wav" silence 1 0.1 0.3% 1 1.8 0.3% : newfile : restart 2>>"$LOG"
    for f in $(ls "$WORK"/_split*.wav 2>/dev/null | sort); do
        i=$((i+1))
        mv "$f" "$WORK/track$(printf '%03d' "$i").wav"
    done
done
NSPLIT=$i
log "Split into $NSPLIT track(s)"
if [ "$NSPLIT" -eq 0 ]; then
    echo '{"status":"error","message":"No tracks detected — the recording may be silent or too quiet."}' > "$STATUS"
    exit 1
fi

# 2) Encode → tag → move → cover → history (python handles JSON + metaflac).
echo "{\"status\":\"tagging\",\"message\":\"Encoding + tagging $NSPLIT tracks…\"}" > "$STATUS"
RES=$(python3 - "$SID" <<'PY' 2>>"$LOG"
import json, os, re, glob, subprocess, sys
from datetime import datetime
SID = sys.argv[1]
WORK = f"/home/pi/vinyl-work/{SID}"; MUSIC = os.environ.get("MUSIC_DIR", "/mnt/music/Music")
sess = json.load(open("/tmp/vinyl-session.json"))
artist = (sess.get("artist") or "Unknown Artist").strip()
album  = (sess.get("album")  or "Unknown Album").strip()
mbid   = sess.get("mbid", "")
date   = sess.get("date", "") or sess.get("year", "")
titles = sess.get("tracks", []) or []
def munge(s): return re.sub(r'[:*?"<>|/]', '', str(s)).strip() or "Unknown"
dest = os.path.join(MUSIC, f"{munge(artist)} - {munge(album)}")
os.makedirs(dest, exist_ok=True)
wavs = sorted(glob.glob(f"{WORK}/track*.wav"))
n = len(wavs)
for idx, w in enumerate(wavs, 1):
    title = titles[idx-1] if idx-1 < len(titles) else f"Track {idx}"
    out = os.path.join(dest, f"{idx:02d} - {munge(title)}.flac")
    subprocess.run(["flac", "-s", "-f", "-e", "-V", "-8", "-o", out, w])
    tags = ["--remove-tag=ARTIST", "--remove-tag=ALBUM", "--remove-tag=TITLE",
            "--remove-tag=TRACKNUMBER", "--remove-tag=TRACKTOTAL",
            f"--set-tag=ARTIST={artist}", f"--set-tag=ALBUM={album}",
            f"--set-tag=TITLE={title}", f"--set-tag=TRACKNUMBER={idx}",
            f"--set-tag=TRACKTOTAL={n}"]
    if date:
        tags.append(f"--set-tag=DATE={date}")
    subprocess.run(["metaflac", *tags, out])
# cover art (reuse the CD ripper's fetcher: release → release-group → name search).
# Silence its stdout so its chatter can't leak into the tab-separated result line.
subprocess.run(["python3", "/usr/local/bin/fetch_cover.py", dest, mbid],
               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
# history (robust append, same schema the UI reads)
HIST = "/opt/ripper/history.json"
try:
    t = open(HIST).read().strip(); h = json.loads(t) if t else []
except Exception:
    h = []
if not isinstance(h, list):
    h = []
h.append({"artist": artist, "album": album, "tracks": n,
          "date": datetime.now().isoformat(timespec="seconds"), "source": "vinyl"})
open(HIST, "w").write(json.dumps(h, indent=2))
warn = "1" if (titles and n != len(titles)) else "0"
print(f"{artist}\t{album}\t{n}\t{warn}")
PY
)
RES=$(printf '%s' "$RES" | tail -1)   # only the final tab-separated result line
ARTIST=$(printf '%s' "$RES" | cut -f1)
ALBUM=$(printf '%s' "$RES" | cut -f2)
N=$(printf '%s' "$RES" | cut -f3)
WARN=$(printf '%s' "$RES" | cut -f4)
[ -z "$ARTIST$ALBUM" ] && { echo '{"status":"error","message":"Encoding/tagging failed — see /var/log/vinyl.log"}' > "$STATUS"; exit 1; }
log "Finalized: $ARTIST - $ALBUM ($N tracks, warn=$WARN)"

# 3) Plex refresh + phone push + cleanup (both optional — skipped if unconfigured).
[ -n "$PLEX_URL" ] && [ -n "$PLEX_TOKEN" ] && \
    curl -s -m 10 "$PLEX_URL/library/sections/$PLEX_SECTION/refresh?X-Plex-Token=$PLEX_TOKEN" >/dev/null 2>&1
MSG="$ARTIST - $ALBUM has been recorded from vinyl to FLAC and saved to the music library."
[ "$WARN" = "1" ] && MSG="$MSG Heads up: I split it into $N tracks, which differs from the tracklist — worth a check."
[ -n "$NOTIFY_URL" ] && curl -s -m 8 -X POST "$NOTIFY_URL" -H "Content-Type: application/json" \
    -d "{\"event\":\"Vinyl saved\",\"context\":\"${MSG//\"/}\"}" >/dev/null 2>&1

if [ "$WARN" = "1" ]; then
    echo "{\"status\":\"done\",\"message\":\"Saved $N tracks — count differs from the tracklist, check the split.\",\"artist\":\"$ARTIST\",\"album\":\"$ALBUM\"}" > "$STATUS"
else
    echo "{\"status\":\"done\",\"message\":\"Saved $N tracks to the library.\",\"artist\":\"$ARTIST\",\"album\":\"$ALBUM\"}" > "$STATUS"
fi
rm -rf "$WORK"
log "=== Done $SID: $ARTIST - $ALBUM ==="
