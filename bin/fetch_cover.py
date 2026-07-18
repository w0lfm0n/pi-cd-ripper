#!/usr/bin/env python3
# Fetch front cover art into an album folder as cover.jpg.
# Usage: fetch_cover.py <album_dir> [release_mbid]
import json, os, sys, urllib.request, urllib.parse

UA = {"User-Agent": "cd-ripper/1.0 ( pi@home )"}


def get(url):
    try:
        return urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=15).read()
    except Exception:
        return None


def main():
    dest = sys.argv[1]
    mbid = sys.argv[2] if len(sys.argv) > 2 else ""
    if not dest or not os.path.isdir(dest):
        print("no dest dir:", dest)
        return
    if os.path.exists(os.path.join(dest, "cover.jpg")):
        print("cover.jpg already present")
        return

    data = None
    # 1) direct release cover
    if mbid:
        data = get(f"https://coverartarchive.org/release/{mbid}/front-500")
        # 2) fall back to the release-group cover
        if not data:
            rg = json.loads(get(f"https://musicbrainz.org/ws/2/release/{mbid}?inc=release-groups&fmt=json") or b"{}")
            rgid = (rg.get("release-group") or {}).get("id", "")
            if rgid:
                data = get(f"https://coverartarchive.org/release-group/{rgid}/front-500")

    # 3) no mbid (or nothing found) → search MusicBrainz by the folder name "Artist - Album"
    if not data:
        base = os.path.basename(dest.rstrip("/"))
        if " - " in base:
            artist, album = base.split(" - ", 1)
            q = urllib.parse.quote(f'releasegroup:"{album}" AND artist:"{artist}"')
            d = json.loads(get(f"https://musicbrainz.org/ws/2/release-group/?query={q}&fmt=json") or b"{}")
            rgs = d.get("release-groups", [])
            if rgs:
                data = get(f"https://coverartarchive.org/release-group/{rgs[0]['id']}/front-500")

    if data:
        with open(os.path.join(dest, "cover.jpg"), "wb") as f:
            f.write(data)
        print("SAVED cover.jpg:", len(data), "bytes")
    else:
        print("no cover art found")


if __name__ == "__main__":
    main()
