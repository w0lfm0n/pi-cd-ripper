#!/bin/bash
# Record ONE vinyl side to WAV via arecord, streaming a live level meter to
# /tmp/vinyl-status.json. Launched (detached) by the Flask UI /api/vinyl/start.
# Stop by sending SIGINT to arecord (Flask /api/vinyl/stop) — arecord finalises
# the WAV cleanly, this wrapper then marks the side done.
SID="$1"; SIDE="$2"
[ -z "$SID" ] || [ -z "$SIDE" ] && { echo "usage: record-side.sh <sid> <side>"; exit 2; }
WORK="/home/pi/vinyl-work/$SID"
STATUS=/tmp/vinyl-status.json
LOG=/var/log/vinyl.log
mkdir -p "$WORK"
OUT="$WORK/side$(printf '%02d' "$SIDE").wav"
log(){ echo "[$(date)] $*" >> "$LOG"; }

# Resolve the USB capture card from `arecord -l`.
CARD=$(arecord -l 2>/dev/null | grep -iE 'card [0-9]+:.*(USB|CODEC|LP60|Audio)' | head -1 | sed -E 's/^card ([0-9]+):.*/\1/')
if [ -z "$CARD" ]; then
    log "No USB capture device found (side $SIDE)"
    echo '{"status":"error","message":"No USB capture device. Is the turntable powered on and plugged into the Pi?"}' > "$STATUS"
    exit 1
fi

log "Recording side $SIDE on card $CARD -> $OUT"
echo "{\"status\":\"recording\",\"level\":0,\"elapsed\":0,\"side\":$SIDE}" > "$STATUS"

START=$(date +%s)
# arecord writes the WAV to $OUT; -V stereo prints a live VU meter to stderr.
# Merge stderr, split the \r-updated meter into lines, parse the peak %, and
# rewrite the status file (level + elapsed) on every meter tick.
stdbuf -oL -eL arecord -D "plughw:${CARD},0" -f S16_LE -r 48000 -c 2 -V stereo -t wav "$OUT" 2>&1 \
  | stdbuf -oL tr '\r' '\n' \
  | while IFS= read -r line; do
        pct=$(printf '%s' "$line" | grep -oE '[0-9]+%' | tail -1 | tr -d '%')
        [ -n "$pct" ] && LAST=$pct
        now=$(date +%s); el=$((now-START))
        printf '{"status":"recording","level":%s,"elapsed":%s,"side":%s}\n' "${LAST:-0}" "$el" "$SIDE" > "$STATUS"
    done

# arecord has exited (SIGINT from Stop, or device removed).
SZ=$(du -h "$OUT" 2>/dev/null | cut -f1)
log "Side $SIDE finished (${SZ:-unknown})"
echo '{"status":"ready"}' > "$STATUS"
