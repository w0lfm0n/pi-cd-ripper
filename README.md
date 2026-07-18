# Pi CD + Vinyl Ripper

Turn a Raspberry Pi and a cheap USB optical drive into a **hands-off CD ripper**: drop
a disc in and it rips itself to tagged lossless FLAC on your NAS, fetches cover art,
refreshes Plex, and pings your phone when it's done. **Zero clicks.**

It also has a **Vinyl mode** — record from a USB turntable, auto-split into tracks on
the silences, tag from MusicBrainz, and run the same FLAC-to-library pipeline.

![status: works on my Pi](https://img.shields.io/badge/status-works%20on%20my%20Pi-brightgreen)
![license: MIT](https://img.shields.io/badge/license-MIT-blue)

---

## What it does

- **Insert a CD → walk away.** A `udev` rule fires a `systemd` service that rips the
  whole disc to verified FLAC (`abcde` + `cdparanoia`), tags it, and files it as
  `Artist - Album/NN - Track.flac`.
- **Skip-if-owned.** Looks the disc up on MusicBrainz *before* ripping and checks your
  library — if you already own the album, it ejects and skips.
- **Cover art** pulled from the Cover Art Archive (release → release-group → name search).
- **Plex refresh + phone push** on completion (both optional).
- **Tiny web dashboard** on `:8080` — live rip progress (track X of Y), recently added,
  rip history, and an activity log.
- **Vinyl mode** — record a record, auto-split, tag, save. Same library, same UI.

## Hardware

| Part | Notes |
|---|---|
| **Raspberry Pi** | A Pi 3 is plenty — ripping is I/O-bound. |
| **USB CD/DVD drive** | **Power it properly** — a desktop drive with its own PSU, or a powered USB hub. A bus-powered drive on the Pi's USB will drop out mid-rip. This is the #1 cause of failed rips. |
| **NAS / storage** | Anything the Pi can mount (NFS/SMB) where Plex already looks. |
| **USB turntable** *(optional)* | Any USB audio-out turntable (e.g. Audio-Technica LP60-USB) for vinyl mode. |

## How it works

```
 CD inserted
     │  udev (99-cd-rip.rules)  ── "systemctl start cd-rip.service"
     ▼
 cd-rip.service ── runs rip-cd.sh to completion (NOT from udev — see gotchas)
     │
     ├─ MusicBrainz TOC lookup ──► already in library?  ──► eject + skip
     │
     ├─ abcde -N  ── rip → encode FLAC → tag → move to  $MUSIC_DIR/Artist - Album/
     ├─ fetch_cover.py ── cover.jpg into the album folder
     ├─ Plex  /library/sections/N/refresh        (optional)
     └─ POST $NOTIFY_URL  {event, context}        (optional phone push)

 Web UI (ripper-ui.service → app.py, Flask on :8080)
     └─ live status, recently added, history, activity log, + Vinyl mode
```

## Install

On the Pi:

```bash
git clone https://github.com/w0lfm0n/pi-cd-ripper
cd pi-cd-ripper
sudo ./install.sh
```

Then:

1. **Mount your music library** on the Pi (the NAS share Plex reads). The installer
   doesn't do this — set it up in `/etc/fstab` so it survives reboots.
2. **Edit `/etc/cd-ripper.env`** — `MUSIC_DIR`, Plex URL/token/section, notify webhook.
   (Template: [`config.env.example`](config.env.example).)
3. **Edit `OUTPUTDIR`** in `~/.abcde.conf` to the **same path** as `MUSIC_DIR`.
4. Browse to `http://<pi-ip>:8080` and drop a CD in.

### Configuration

Everything deployment-specific lives in `/etc/cd-ripper.env` — nothing is hard-coded:

| Var | What |
|---|---|
| `MUSIC_DIR` | Where albums are written (your mounted Plex music library). |
| `PLEX_URL` / `PLEX_TOKEN` / `PLEX_SECTION` | Trigger a Plex scan after each rip. Leave `PLEX_URL` blank to disable. |
| `NOTIFY_URL` | Webhook fired on rip complete / already-owned / vinyl saved. Receives `POST {"event","context"}`. Point it at ntfy, Gotify, Home Assistant, Discord relay… Leave blank to disable. |

## Vinyl mode

Open the web UI → **🎵 Vinyl** tab:

1. Search MusicBrainz for the album (gets the tracklist + cover).
2. **Record Side 1**, play the record, **Stop** at the end. Repeat for each side.
3. **Finish & Save** — it splits each side on the inter-track silences, encodes to FLAC,
   tags from the tracklist, saves to your library, and (optionally) refreshes Plex.

If the auto-split track count differs from the MusicBrainz tracklist, it flags it so you
can check the split.

## Gotchas I hit (so you don't)

- **udev kills long-running tasks (~3 min).** Never rip directly from the udev rule —
  the rule just does `systemctl start cd-rip.service`, and the service runs the rip to
  completion with no time limit.
- **`abcde.conf` must use single quotes.** It's sourced as a shell script, so
  double-quoted `${ARTIST}`/`${ALBUM}` expand to *empty* at load time and every track
  collides into one `" - "` folder. Also use `${ARTISTFILE}`/`${ALBUMFILE}` (the
  filesystem-safe, non-empty ones) in `OUTPUTFORMAT`.
- **Run `abcde -N`** (non-interactive, auto-pick first match). Headless under systemd,
  stdin is `/dev/null`, so without `-N` abcde picks "no metadata" → blank tags.
- **Power the optical drive.** Under-powered USB drives drop out mid-rip.

## Files

```
app.py                     Flask web UI (CD status + Vinyl mode), served on :8080
bin/rip-cd.sh              CD rip orchestrator (MusicBrainz, dedup, abcde, cover, notify)
bin/fetch_cover.py         Cover Art Archive fetcher → cover.jpg
bin/record-side.sh         Vinyl: record one side via arecord + live level meter
bin/process-vinyl.sh       Vinyl: silence-split → FLAC → tag → save
config/abcde.conf          abcde ripper config (→ ~/.abcde.conf)
config/cd-rip.service      systemd unit that runs the rip (→ /etc/systemd/system)
config/ripper-ui.service   systemd unit for the web UI (→ /etc/systemd/system)
config/99-cd-rip.rules     udev rule: disc inserted → start cd-rip.service
config.env.example         Config template (→ /etc/cd-ripper.env)
install.sh                 One-shot installer
```

## License

MIT — see [LICENSE](LICENSE).
