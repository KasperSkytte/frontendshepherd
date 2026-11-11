#!/bin/sh
# Install frontendshepherd on this login node. Run as root.
set -eu
cd "$(dirname "$0")"

[ -f /sys/fs/cgroup/cgroup.controllers ] || { echo "cgroup v2 is required" >&2; exit 1; }

install -m 755 frontendshepherd.py /usr/local/bin/frontendshepherd
install -m 644 frontendshepherd.service /etc/systemd/system/frontendshepherd.service
if [ -e /etc/frontendshepherd.conf ]; then
    echo "keeping existing /etc/frontendshepherd.conf"
else
    install -m 644 frontendshepherd.conf /etc/frontendshepherd.conf
fi

systemctl daemon-reload
systemctl enable frontendshepherd
systemctl restart frontendshepherd
echo "Installed. Follow it with: journalctl -fu frontendshepherd"
grep -q '^dry_run *= *true' /etc/frontendshepherd.conf &&
    echo "It is in DRY RUN mode: set dry_run = false in /etc/frontendshepherd.conf to enforce."
exit 0
