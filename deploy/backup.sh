#!/usr/bin/env bash
# Daily backup of the database + uploaded lists. Keeps 14 days. Installed to /etc/cron.daily by install.sh.
set -euo pipefail
DATA=/var/lib/driver-outreach
DEST=$DATA/backups
mkdir -p "$DEST"
STAMP=$(date +%Y-%m-%d)
/opt/driver-outreach/venv/bin/python - "$DATA/outreach.db" "$DEST/outreach-$STAMP.db" <<'EOF'
import sqlite3, sys
src = sqlite3.connect(sys.argv[1]); dst = sqlite3.connect(sys.argv[2])
src.backup(dst); dst.close(); src.close()
EOF
tar -czf "$DEST/files-$STAMP.tar.gz" -C "$DATA" files 2>/dev/null || true
chown -R outreach:outreach "$DEST"
chmod 600 "$DEST"/* 2>/dev/null || true
find "$DEST" -type f -mtime +14 -delete
