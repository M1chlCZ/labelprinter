#!/bin/sh
# Install the label print bridge on a CUPS host. Run as root.
set -eu

[ "$(id -u)" = 0 ] || { echo "install.sh must run as root" >&2; exit 1; }
here=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)

install -d -m 0755 /usr/local/lib/labelprinter
install -m 0644 "$here/labelprinter.py" /usr/local/lib/labelprinter/labelprinter.py
install -m 0755 "$here/pc43-filter" /usr/local/lib/labelprinter/pc43-filter
cc -O2 -o /usr/local/lib/labelprinter/pbmtodp "$here/pbmtodp.c"

install -d -m 0755 /etc/labelprinter
if [ ! -f /etc/labelprinter/environment ]; then
  install -m 0644 "$here/environment.example" /etc/labelprinter/environment
fi
install -m 0644 "$here/labelprinter.service" /etc/systemd/system/labelprinter.service

getent group labelprinter >/dev/null || groupadd --system labelprinter
if ! getent passwd labelprinter >/dev/null; then
  useradd --system --gid labelprinter --no-create-home \
    --home-dir /var/lib/labelprinter --shell /usr/sbin/nologin labelprinter
fi
usermod -a -G lpadmin labelprinter 2>/dev/null || true

systemctl daemon-reload

cat <<'EOF'
Installed. Next steps:
  1. Create a token of at least 32 characters in /etc/labelprinter/token
     (chmod 600, owner labelprinter).
  2. Create a TLS certificate and key in /etc/labelprinter/server.crt and
     /etc/labelprinter/server.key (for example with openssl req -x509).
  3. Optional: install the included PPD for a PC43t queue, for example
     lpadmin -p PC43t -P ./pc43.ppd -v usb://... -E
  4. systemctl enable --now labelprinter
EOF
