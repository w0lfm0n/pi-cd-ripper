#!/bin/bash
# Installer for the Pi CD/Vinyl ripper. Run on the Raspberry Pi as a sudo user:
#   git clone https://github.com/w0lfm0n/pi-cd-ripper && cd pi-cd-ripper && sudo ./install.sh
set -e
[ "$(id -u)" -eq 0 ] || { echo "Run with sudo."; exit 1; }

RIPPER_USER="${SUDO_USER:-pi}"
echo "==> Installing for user: $RIPPER_USER"

echo "==> Installing packages (abcde, cdparanoia, flac, sox, cd-discid, eject, python3-flask)…"
apt-get update
apt-get install -y abcde cdparanoia flac sox alsa-utils cd-discid eject \
                   python3 python3-flask curl

echo "==> Placing scripts in /usr/local/bin…"
install -m 755 bin/rip-cd.sh       /usr/local/bin/rip-cd.sh
install -m 755 bin/process-vinyl.sh /usr/local/bin/process-vinyl.sh
install -m 755 bin/record-side.sh  /usr/local/bin/record-side.sh
install -m 755 bin/fetch_cover.py  /usr/local/bin/fetch_cover.py

echo "==> Placing web UI in /opt/ripper…"
install -d -o "$RIPPER_USER" /opt/ripper
install -m 644 -o "$RIPPER_USER" app.py /opt/ripper/app.py
[ -f /opt/ripper/history.json ] || install -m 644 -o "$RIPPER_USER" /dev/null /opt/ripper/history.json

echo "==> abcde config → /home/$RIPPER_USER/.abcde.conf (edit OUTPUTDIR to match MUSIC_DIR)…"
install -m 644 -o "$RIPPER_USER" config/abcde.conf "/home/$RIPPER_USER/.abcde.conf"

echo "==> Config → /etc/cd-ripper.env (edit it after install)…"
[ -f /etc/cd-ripper.env ] || install -m 600 config.env.example /etc/cd-ripper.env

echo "==> systemd units + udev rule…"
install -m 644 config/cd-rip.service   /etc/systemd/system/cd-rip.service
install -m 644 config/ripper-ui.service /etc/systemd/system/ripper-ui.service
install -m 644 config/99-cd-rip.rules  /etc/udev/rules.d/99-cd-rip.rules

echo "==> Log files…"
touch /var/log/rip-cd.log /var/log/vinyl.log
chown "$RIPPER_USER" /var/log/rip-cd.log /var/log/vinyl.log

# The web UI user needs to reach the audio devices for vinyl capture.
usermod -aG audio,cdrom "$RIPPER_USER" || true

systemctl daemon-reload
udevadm control --reload-rules
systemctl enable --now ripper-ui.service

cat <<DONE

==> Done.
   1. Edit /etc/cd-ripper.env   (MUSIC_DIR, Plex, notify webhook)
   2. Edit OUTPUTDIR in /home/$RIPPER_USER/.abcde.conf to match MUSIC_DIR
   3. Make sure MUSIC_DIR is mounted (e.g. your NAS music share)
   4. Web UI:  http://<pi-ip>:8080
   5. Drop a CD in — it rips itself.
DONE
