#!/usr/bin/env python3
"""One-shot: find album folders with no local/embedded art and write cover.jpg
from iTunes (<=1200px, token-overlap confidence guard). Reads artist/album from
FLAC tags, not folder names. Safe to re-run (skips folders that now have art)."""
import os, glob, json, subprocess, urllib.request, urllib.parse, re, time

ROOT = "/mnt/music/Music"
CAP = 1200
COVERS = ["cover.jpg", "folder.jpg", "front.jpg", "Cover.jpg", "cover.png", "Folder.jpg"]
DISC = re.compile(r"^(cd|disc)\s*\d", re.I)

def has_cover_file(d):
    if any(os.path.exists(os.path.join(d, c)) for c in COVERS):
        return True
    return bool(glob.glob(os.path.join(d, "*.jpg")) + glob.glob(os.path.join(d, "*.png")))

def has_embedded(fl):
    if not fl:
        return False
    r = subprocess.run(["metaflac", "--list", "--block-type=PICTURE", fl[0]],
                       capture_output=True, text=True)
    return "PICTURE" in r.stdout

def tag(f, name):
    r = subprocess.run(["metaflac", "--show-tag=" + name, f], capture_output=True, text=True)
    out = r.stdout.strip()
    return out.split("=", 1)[1] if "=" in out else ""

def toks(s):
    return set(re.sub(r"[^a-z0-9 ]", " ", s.lower()).split())

def _clean(s):
    # drop parenthetical/bracket qualifiers + "remastered/deluxe/edition" noise
    s = re.sub(r"[\(\[].*?[\)\]]", " ", s)
    s = re.sub(r"\b(remaster(ed)?|deluxe|anniversary|edition|reissue|expanded|bonus|disc|cd\d*|japan(ese)?)\b",
               " ", s, flags=re.I)
    return re.sub(r"\s+", " ", s).strip()

def itunes(artist, album, retries=3):
    term = urllib.parse.quote(f"{artist} {album}")
    url = f"https://itunes.apple.com/search?term={term}&entity=album&limit=8"
    data = None
    for i in range(retries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "cover-fetch/1.0"})
            with urllib.request.urlopen(req, timeout=15) as r:
                data = json.load(r)
            break
        except Exception:
            time.sleep(3 * (i + 1))  # back off on throttle
    if data is None:
        return None, -1.0  # -1 signals request failure, not a mismatch
    want = toks(_clean(artist) + " " + _clean(album))
    best, bestscore = None, 0.0
    for res in data.get("results", []):
        cand = toks(_clean(res.get("artistName", "")) + " " + _clean(res.get("collectionName", "")))
        if not cand:
            continue
        score = len(want & cand) / max(1, len(want))
        if score > bestscore:
            bestscore, best = score, res
    if best and bestscore >= 0.4:
        art = best.get("artworkUrl100", "")
        if art:
            return art.replace("100x100bb", f"{CAP}x{CAP}bb"), bestscore
    return None, bestscore

def caa(artist, album):
    """MusicBrainz release-group search → Cover Art Archive front cover."""
    q = urllib.parse.quote(f'artist:"{artist}" AND releasegroup:"{_clean(album)}"')
    url = f"https://musicbrainz.org/ws/2/release-group/?query={q}&fmt=json&limit=3"
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "cover-fetch/1.0 (homelab)"})
        with urllib.request.urlopen(req, timeout=15) as r:
            data = json.load(r)
    except Exception:
        return None
    for rg in data.get("release-groups", []):
        rgid = rg.get("id")
        if not rgid:
            continue
        for variant in ("front-1200", "front-500", "front"):
            try:
                cu = f"https://coverartarchive.org/release-group/{rgid}/{variant}"
                req = urllib.request.Request(cu, headers={"User-Agent": "cover-fetch/1.0 (homelab)"})
                with urllib.request.urlopen(req, timeout=20) as r:
                    img = r.read()
                if img and len(img) > 5000:
                    return img
            except Exception:
                continue
        time.sleep(1)  # MB/CAA politeness
    return None

def fetch(url):
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "cover-fetch/1.0"})
        with urllib.request.urlopen(req, timeout=20) as r:
            return r.read()
    except Exception:
        return None

# --- discover album folders with flacs but no art ---
missing = []
for a in sorted(os.listdir(ROOT)):
    ap = os.path.join(ROOT, a)
    if not os.path.isdir(ap):
        continue
    cands = []
    if glob.glob(os.path.join(ap, "*.flac")):
        cands.append(ap)
    for sub in sorted(glob.glob(os.path.join(ap, "*"))):
        if os.path.isdir(sub) and glob.glob(os.path.join(sub, "*.flac")):
            cands.append(sub)
        # one more level for Artist/Album/Disc layouts
        for sub2 in sorted(glob.glob(os.path.join(sub, "*"))):
            if os.path.isdir(sub2) and glob.glob(os.path.join(sub2, "*.flac")):
                cands.append(sub2)
    for d in cands:
        fl = sorted(glob.glob(os.path.join(d, "*.flac")))
        if not has_cover_file(d) and not has_embedded(fl):
            missing.append(d)

print(f"art-less album folders: {len(missing)}", flush=True)
done = 0
skip = []
for d in missing:
    rel = d.replace(ROOT + "/", "")
    fl = sorted(glob.glob(os.path.join(d, "*.flac")))
    artist = tag(fl[0], "ALBUMARTIST") or tag(fl[0], "ARTIST")
    album = tag(fl[0], "ALBUM")
    if not artist or not album:
        skip.append((rel, "no tags")); continue
    src = "itunes"
    url, sc = itunes(artist, album)
    img = fetch(url) if url else None
    if not img or len(img) < 5000:                 # iTunes miss/throttle → CAA fallback
        img = caa(artist, album)
        src = "caa"
    if not img or len(img) < 5000:
        why = "request failed (throttled?)" if sc < 0 else f"no match score={sc:.2f}"
        skip.append((rel, f"{why} ({artist} - {album})")); continue
    time.sleep(1.5)  # politeness between albums
    targets = [d]
    if DISC.match(os.path.basename(d)):
        targets.append(os.path.dirname(d))
    for t in targets:
        try:
            with open(os.path.join(t, "cover.jpg"), "wb") as w:
                w.write(img)
        except Exception as e:
            print(f"  WRITEFAIL {t}: {e}", flush=True)
    done += 1
    print(f"OK  [{src}] {artist} - {album}  {len(img)//1024}KB -> {rel}", flush=True)

print(f"\nWROTE {done}, SKIPPED {len(skip)}", flush=True)
for rel, why in skip:
    print(f"  SKIP  {rel}  [{why}]", flush=True)
