#!/bin/sh
# Install frontendshepherd on this login node. Run as root.
set -eu
cd "$(dirname "$0")"

[ -f /sys/fs/cgroup/cgroup.controllers ] || { echo "cgroup v2 is required" >&2; exit 1; }

install -m 755 frontendshepherd.py /usr/local/bin/frontendshepherd
install -m 644 frontendshepherd.service /etc/systemd/system/frontendshepherd.service
if [ -e /etc/frontendshepherd.conf ]; then
    echo "Keeping the existing config file /etc/frontendshepherd.conf"
else
    install -m 644 frontendshepherd.conf /etc/frontendshepherd.conf
fi

systemctl daemon-reload
systemctl enable frontendshepherd
systemctl restart frontendshepherd
echo "frontendshepherd is installed and running."
echo "View its log with: journalctl -fu frontendshepherd"
grep -q '^dry_run *= *true' /etc/frontendshepherd.conf && {
    echo "frontendshepherd is in DRY RUN mode: it only logs and limits nothing."
    echo "To enforce the limits, set dry_run = false in /etc/frontendshepherd.conf"
    echo "and run: systemctl restart frontendshepherd"
}
exit 0
