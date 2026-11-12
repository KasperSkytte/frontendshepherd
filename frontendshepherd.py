#!/usr/bin/env python3
"""frontendshepherd - keeps shared login nodes responsive.

Two layers, both built on the systemd/cgroup v2 slices that every login
already gets (user.slice/user-UID.slice):

1. Static limits, written as systemd drop-ins at startup: a memory cap per
   user, a memory cap for all users together, a CPU ceiling per user.
   The kernel enforces these; they hold even if this daemon is not running.

2. A CPU bucket per user, run by this daemon. Using up to `min_cpus` is
   always free. Any higher usage is taken from the user's bucket of
   `bucket_cpu_hours`. When it is empty the user is throttled to `min_cpus`
   until the bucket has refilled completely, which takes `refill_hours`.
   The bucket only refills while usage is below `min_cpus`, so a job that
   keeps running flat out at the throttled limit stays throttled.

A "CPU" here is a logical CPU as counted by nproc and top: one hardware
thread. With hyper-threading, 2 CPUs are one physical core.

Python 3.6+, standard library only. Needs cgroup v2 and systemd.
"""

import argparse
import configparser
import errno
import glob
import json
import logging
import os
import pwd
import re
import signal
import smtplib
import socket
import ssl
import subprocess
import sys
import threading
import time
from email.message import EmailMessage

DEFAULT_CONFIG = "/etc/frontendshepherd.conf"
SYSTEMD_DIR = "/etc/systemd/system"
DROPIN = "90-frontendshepherd.conf"

# Default mail texts. All can be replaced in the [mail] section of the config.
MESSAGES = {
    "throttle_subject": "[{cluster}] Your CPU usage on {host} has been limited",
    "throttle_body": """\
Hi {user},

You have just used up your max allowed burst CPU usage of {bucket_cpu_hours} CPU-hours
on the login node {host}, so all your processes together are now limited
to max {min_cpus} CPUs. Nothing has been killed.

Login nodes are shared by everyone. Short burst workloads (e.g. compiling,
installing software, building containers, developing code etc.) are allowed
with up to max {max_cpus} CPUs at once, but not for extended periods of time.
To prevent impacting the responsiveness of the login nodes, please submit
long and heavy work as batch jobs, so that it runs on the dedicated compute
nodes.

The limit is lifted automatically once your processes have used less than
{min_cpus} CPUs for a total of {refill_hours} hours. Run `frontendshepherd status` on the
login node to check the status.

Your busiest processes right now:

{processes}""",
    "oom_subject": "[{cluster}] Your processes on {host} were killed (too much memory usage)",
    "oom_body": """\
Hi {user},

All your processes on the login node {host} were killed, and you were
logged out, because together they used more memory than allowed (max
{user_max_gb} GB per user, less if the login node is running out of memory).
The largest process was `{process}`.

Login nodes are shared by everyone. To prevent impacting the responsiveness
of the login nodes, please submit long and memory-heavy work as batch jobs,
so that it runs on the dedicated compute nodes.""",
    "repeat_note": "This has happened {events} times within {days} days, so the cluster administrators have been copied on this message.",
}

DEFAULTS = {
    "general": {
        "interval": "10",
        "min_uid": "1000",
        "exempt_users": "",
        "dry_run": "false",
        "state_dir": "/var/lib/frontendshepherd",
        "cgroup_root": "/sys/fs/cgroup",
        "user_tasks_max": "8192",
    },
    "cpu": {
        "min_cpus": "2",
        "max_cpus": "8",
        "bucket_cpu_hours": "8",
        "refill_hours": "4",
        "reserved_cpus": "2",
    },
    "memory": {
        "user_max_gb": "16",
        "reserved_gb": "8",
    },
    "mail": {
        "enabled": "true",
        "smtp_host": "",
        "smtp_port": "0",
        "smtp_security": "starttls",
        "smtp_user": "",
        "smtp_password_file": "",
        "sendmail": "/usr/sbin/sendmail",
        "from": "root",
        "admin": "root",
        "address": "{user}",
        "max_mails_per_day": "100",
        "repeat_events": "0",
        "repeat_days": "7",
        "cluster_name": "",
        **MESSAGES,
    },
    "sheepdog": {
        "enabled": "true",
        "patterns": r"\.vscode-server/",
        "max_age_days": "7",
        "interval_minutes": "30",
    },
}

log = logging.getLogger("frontendshepherd")

# The kernel's log line for an out-of-memory kill in a cgroup
OOM_RE = re.compile(r"oom-kill:.*oom_memcg=(/[^,]*),task_memcg=(/[^,]*),task=([^,]*),pid=")


# --------------------------------------------------------------------------
# small helpers
# --------------------------------------------------------------------------

def load_config(path):
    cp = configparser.ConfigParser(interpolation=None)
    cp.read_dict(DEFAULTS)
    if not cp.read(path):
        log.warning("config %s not found, using built-in defaults", path)
    return cp


def unknown_settings(cp):
    """Sections and settings in the config that the tool does not know, most
    likely misspelled. They have no effect."""
    out = []
    for section in cp.sections():
        if section not in DEFAULTS:
            out.append("[%s]" % section)
            continue
        out += ["[%s] %s" % (section, k) for k in cp[section] if k not in DEFAULTS[section]]
    return out


def read_file(path):
    try:
        with open(path) as f:
            return f.read()
    except OSError:
        return None


def read_kv(path):
    """Parse 'key value' files such as cpu.stat and memory.events."""
    out = {}
    for line in (read_file(path) or "").splitlines():
        parts = line.split()
        if len(parts) == 2 and parts[1].isdigit():
            out[parts[0]] = int(parts[1])
    return out


def cpu_seconds(slice_path):
    """Total CPU seconds consumed by a cgroup, or None if it is gone."""
    usec = read_kv(os.path.join(slice_path, "cpu.stat")).get("usage_usec")
    return None if usec is None else usec / 1e6


def username(uid):
    try:
        return pwd.getpwuid(int(uid)).pw_name
    except KeyError:
        return str(uid)


def update_bucket(st, used, dt, min_cpus, capacity, refill):
    """Advance one user's bucket by dt seconds in which `used` CPU-seconds
    were consumed. Returns "throttle", "release" or None.

    `used / dt` is the average number of CPUs over the interval.
    - above min_cpus: the usage is taken from the bucket in full, so
      8 CPUs for 1 hour is exactly 8 CPU-hours
    - below min_cpus: the bucket refills by `refill` CPU-seconds per second
    - right at min_cpus (within 5 %): neither. This is where a throttled
      job that wants more CPU sits, pinned at its limit, so it gets no
      refill until it ends or is stopped."""
    if used > min_cpus * dt * 1.05:
        st["level"] = max(0.0, st["level"] - used)
    elif used < min_cpus * dt * 0.95:
        st["level"] = min(capacity, st["level"] + refill * dt)
    if not st["throttled"] and st["level"] <= 0:
        st["throttled"] = True
        return "throttle"
    if st["throttled"] and st["level"] >= capacity:
        st["throttled"] = False
        return "release"
    return None


# --------------------------------------------------------------------------
# the daemon
# --------------------------------------------------------------------------

class Shepherd:
    def __init__(self, cp, dry_run=False):
        g, c, m, mail, r = (cp[s] for s in ("general", "cpu", "memory", "mail", "sheepdog"))
        self.interval = g.getfloat("interval")
        self.min_uid = g.getint("min_uid")
        self.exempt_users = set(g.get("exempt_users").replace(",", " ").split())
        self.dry_run = dry_run or g.getboolean("dry_run")
        self.state_dir = g.get("state_dir")
        self.state_file = os.path.join(self.state_dir, "state.json")
        self.user_slice = os.path.join(g.get("cgroup_root"), "user.slice")
        self.tasks_max = g.getint("user_tasks_max")

        # CPU amounts are logical CPUs (hardware threads), as in nproc/top.
        self.ncpu = os.cpu_count() or 1
        self.min_cpus = c.getfloat("min_cpus")
        self.reserved_cores = c.getfloat("reserved_cpus")
        self.burst = min(self.ncpu, c.getfloat("max_cpus"))
        if self.burst <= self.min_cpus:  # node smaller than the config assumes
            self.min_cpus = self.burst / 2
            log.warning("max_cpus is not above min_cpus on this node; "
                        "using min_cpus = %.3g", self.min_cpus)
        self.capacity = c.getfloat("bucket_cpu_hours") * 3600  # CPU-seconds
        self.refill_hours = c.getfloat("refill_hours")
        self.refill = self.capacity / (self.refill_hours * 3600)  # CPU-seconds per second

        self.total_gb = meminfo_total_kb() / 2**20
        self.user_max_gb = m.getfloat("user_max_gb")
        self.reserved_gb = m.getfloat("reserved_gb")

        self.mail_enabled = mail.getboolean("enabled")
        self.smtp_host = mail.get("smtp_host").strip()
        self.smtp_port = mail.getint("smtp_port")
        self.smtp_security = mail.get("smtp_security").strip().lower()
        self.smtp_user = mail.get("smtp_user").strip()
        self.smtp_password_file = mail.get("smtp_password_file").strip()
        self.messages = {k: mail.get(k).strip() for k in MESSAGES}
        self.sendmail = mail.get("sendmail")
        self.mail_from = mail.get("from")
        self.admin = mail.get("admin")
        self.address = mail.get("address")
        self.max_mails = mail.getint("max_mails_per_day")
        self.repeat_events = mail.getint("repeat_events")
        self.repeat_window = mail.getfloat("repeat_days") * 86400
        self.host = socket.gethostname().split(".")[0]
        self.cluster = mail.get("cluster_name") or self.host

        self.sheepdog_enabled = r.getboolean("enabled")
        self.sheepdog_patterns = [re.compile(p) for p in r.get("patterns").split()]
        self.sheepdog_max_age = r.getfloat("max_age_days") * 86400
        self.sheepdog_interval = r.getfloat("interval_minutes") * 60
        self.sheepdog_last = 0.0

        self.state = {"users": {}, "admin_mails": []}
        self.kmsg = None  # kernel log, opened in run()
        self.stop = threading.Event()

    # ---- state ----------------------------------------------------------

    def load_state(self):
        try:
            with open(self.state_file) as f:
                self.state.update(json.load(f))
        except FileNotFoundError:
            pass
        except (OSError, ValueError) as e:
            log.warning("ignoring unreadable state file: %s", e)

    def save_state(self):
        tmp = self.state_file + ".tmp"
        try:
            with open(tmp, "w") as f:
                json.dump(self.state, f)
            os.chmod(tmp, 0o644)  # world readable so users can run `status`
            os.replace(tmp, self.state_file)
        except OSError as e:
            log.warning("cannot save state: %s", e)

    def new_user(self, now):
        return {"level": self.capacity, "throttled": False, "usage": None,
                "ts": now, "cores": 0.0, "events": [], "mails": []}

    def exempt(self, uid):
        return uid < self.min_uid or username(uid) in self.exempt_users

    # ---- static limits (systemd drop-ins) -------------------------------

    def dropins(self):
        """Return {directory name: drop-in content} for the current config."""
        total = ["[Slice]", "MemoryMax=%dM" % ((self.total_gb - self.reserved_gb) * 1024)]
        if 0 < self.reserved_cores < self.ncpu:
            total.append("CPUQuota=%d%%" % round((self.ncpu - self.reserved_cores) * 100))

        user = ["[Slice]",
                "MemoryMax=%dM" % (self.user_max_gb * 1024),
                "CPUQuota=%d%%" % round(self.burst * 100),
                "TasksMax=%d" % self.tasks_max,
                # No swapping past the limit: over the limit means killed.
                "MemorySwapMax=0"]

        unlimited = ["[Slice]", "MemoryMax=infinity", "MemorySwapMax=infinity",
                     "CPUQuota=", "TasksMax=infinity"]

        out = {"user.slice.d": total, "user-.slice.d": user,
               "user-0.slice.d": unlimited}
        for name in self.exempt_users:
            try:
                out["user-%d.slice.d" % pwd.getpwnam(name).pw_uid] = unlimited
            except KeyError:
                log.warning("exempt user %s does not exist", name)
        header = "# Written by frontendshepherd from its config. Do not edit.\n"
        return {d: header + "\n".join(lines) + "\n" for d, lines in out.items()}

    def write_dropins(self):
        wanted = self.dropins()
        changed = False
        for path in glob.glob(os.path.join(SYSTEMD_DIR, "user*.slice.d", DROPIN)):
            if os.path.basename(os.path.dirname(path)) not in wanted:
                os.remove(path)
                changed = True
        for d, content in wanted.items():
            path = os.path.join(SYSTEMD_DIR, d, DROPIN)
            if read_file(path) != content:
                os.makedirs(os.path.dirname(path), exist_ok=True)
                with open(path, "w") as f:
                    f.write(content)
                changed = True
        if changed:
            log.info("static limits changed, reloading systemd")
            subprocess.run(["systemctl", "daemon-reload"], check=False)

    # ---- CPU quota ------------------------------------------------------

    def ensure_quota(self, uid, path, cores):
        """Make the slice's CPU ceiling equal `cores`; no-op if it already is."""
        if self.dry_run:
            return
        cur = (read_file(os.path.join(path, "cpu.max")) or "").split()
        if len(cur) == 2 and cur[0] != "max" and abs(int(cur[0]) / int(cur[1]) - cores) < 0.01:
            return
        r = subprocess.run(
            ["systemctl", "set-property", "--runtime", "user-%s.slice" % uid,
             "CPUQuota=%d%%" % round(cores * 100)],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, universal_newlines=True)
        if r.returncode:
            log.warning("could not set CPU quota for uid %s: %s", uid, r.stdout.strip())

    # ---- mail -----------------------------------------------------------

    def send(self, to, subject, body, cc=None):
        if self.dry_run or not self.mail_enabled:
            log.info("mail not sent (%s) to %s: %s",
                     "dry run" if self.dry_run else "mail disabled", to, subject)
            return
        try:
            self.deliver(to, subject, body, cc)
        except (OSError, subprocess.SubprocessError, smtplib.SMTPException) as e:
            log.warning("sending mail to %s failed: %s", to, e)

    def deliver(self, to, subject, body, cc=None):
        """Send one mail through the SMTP server if one is configured,
        otherwise through the local sendmail. Raises on failure."""
        msg = EmailMessage()
        msg["From"], msg["To"], msg["Subject"] = self.mail_from, to, subject
        if cc:
            msg["Cc"] = cc
        msg.set_content(body)
        if not self.smtp_host:
            subprocess.run([self.sendmail, "-t", "-oi"], input=msg.as_bytes(),
                           timeout=30, check=True)
            return
        use_ssl = self.smtp_security == "ssl"
        port = self.smtp_port or {"ssl": 465, "starttls": 587}.get(self.smtp_security, 25)
        tls = ssl.create_default_context()  # verifies the server's certificate
        smtp = (smtplib.SMTP_SSL(self.smtp_host, port, timeout=30, context=tls) if use_ssl
                else smtplib.SMTP(self.smtp_host, port, timeout=30))
        with smtp:
            if self.smtp_security == "starttls":
                smtp.starttls(context=tls)
            if self.smtp_user:
                with open(self.smtp_password_file) as f:
                    smtp.login(self.smtp_user, f.read().strip())
            smtp.send_message(msg)

    def may_mail(self, sent, now):
        """`sent` holds the times of earlier mails to one recipient. Record
        one more, unless max_mails_per_day were sent in the last 24 hours."""
        sent[:] = [t for t in sent if now - t < 86400]
        if len(sent) >= self.max_mails:
            return False
        sent.append(now)
        return True

    def notify_user(self, uid, kind, st, now, process=""):
        if not self.may_mail(st.setdefault("mails", []), now):
            log.warning("no mail to %s: already %d mails in the last 24 hours",
                        username(uid), self.max_mails)
            return
        user = username(uid)
        # repeat_events = 0 (the default): the admin is never copied
        repeat = 0 < self.repeat_events <= len(st["events"])
        values = {
            "user": user, "host": self.host, "cluster": self.cluster,
            "min_cpus": "%.3g" % self.min_cpus, "max_cpus": "%.3g" % self.burst,
            "bucket_cpu_hours": "%.3g" % (self.capacity / 3600),
            "refill_hours": "%.3g" % self.refill_hours,
            "user_max_gb": "%.3g" % self.user_max_gb,
            "events": len(st["events"]), "days": "%.3g" % (self.repeat_window / 86400),
            "processes": (top_processes(os.path.join(self.user_slice, "user-%s.slice" % uid))
                          if kind == "throttle" else ""),
            "process": process,
        }
        body = self.render(kind + "_body", values) + "\n"
        if repeat:
            body += "\n" + self.render("repeat_note", values) + "\n"
        self.send(self.address.format(user=user), self.render(kind + "_subject", values),
                  body, cc=self.admin if repeat else None)

    def render(self, key, values):
        """Fill in a mail text from the config; fall back to the built-in
        text if the configured one has a broken {placeholder}."""
        try:
            return self.messages[key].format(**values)
        except (KeyError, IndexError, ValueError) as e:
            log.warning("mail text %s in the config is broken (%r), using the default", key, e)
            return MESSAGES[key].format(**values)

    # ---- main loop ------------------------------------------------------

    def apply_resets(self):
        """`frontendshepherd reset USER` leaves a file reset-UID in the state
        directory. Fill that user's bucket and lift the throttle."""
        for path in glob.glob(os.path.join(self.state_dir, "reset-*")):
            uid = os.path.basename(path)[len("reset-"):]
            try:
                os.remove(path)
            except OSError:
                continue
            st = self.state["users"].get(uid)
            if st:
                st["level"], st["throttled"] = self.capacity, False
            log.info("RESET %s (uid %s): bucket filled by an admin", username(uid), uid)

    def tick(self, now=None):
        now = now or time.time()
        users = self.state["users"]
        self.apply_resets()
        present = {}
        for path in glob.glob(os.path.join(self.user_slice, "user-*.slice")):
            m = re.search(r"user-(\d+)\.slice$", path)
            if m and not self.exempt(int(m.group(1))):
                present[m.group(1)] = path

        for uid in set(users) | set(present):
            st = users.setdefault(uid, self.new_user(now))
            dt = max(0.0, now - st["ts"])
            st["ts"] = now
            path = present.get(uid)
            usage = cpu_seconds(path) if path else None

            if usage is None:  # no slice: user has nothing running here
                used = 0.0
                st["usage"] = 0.0
            else:
                prev = st["usage"]
                if prev is None:          # first time we see this user
                    used = 0.0
                elif usage < prev:        # slice was recreated, counter restarted
                    used = usage
                else:
                    used = usage - prev
                st["usage"] = usage
            st["cores"] = round(used / dt, 2) if dt > 0 else 0.0

            event = update_bucket(st, used, dt, self.min_cpus, self.capacity, self.refill)
            st["events"] = [t for t in st["events"] if now - t < self.repeat_window]
            name = username(uid)
            if event == "throttle":
                st["events"].append(now)
                log.info("THROTTLE %s (uid %s): bucket empty, limiting to %.3g CPUs "
                         "(%d events in window)%s", name, uid, self.min_cpus,
                         len(st["events"]), " [dry run]" if self.dry_run else "")
                self.notify_user(uid, "throttle", st, now)
            elif event == "release":
                log.info("RELEASE %s (uid %s): bucket is full again", name, uid)

            if usage is not None:
                self.ensure_quota(uid, path,
                                  self.min_cpus if st["throttled"] else self.burst)
                self.ensure_group_kill(path)
            elif st["level"] >= self.capacity and not st["events"]:
                del users[uid]  # logged out, bucket full, clean record: forget

        self.check_oom(now)
        if self.sheepdog_enabled and now - self.sheepdog_last >= self.sheepdog_interval:
            self.sheepdog_last = now
            self.sheepdog(now)
        self.save_state()

    def ensure_group_kill(self, path):
        """Set memory.oom.group on a user's slice: when the user runs out of
        memory the kernel then kills all of that user's processes at once.
        Without it the kernel kills one process, and while that memory is
        being freed it may go on to kill arbitrary other ones of the user."""
        f = os.path.join(path, "memory.oom.group")
        if self.dry_run or (read_file(f) or "").strip() != "0":
            return
        try:
            with open(f, "w") as fh:
                fh.write("1")
        except OSError as e:
            log.warning("cannot set %s: %s", f, e)

    def open_kmsg(self):
        try:
            self.kmsg = os.open("/dev/kmsg", os.O_RDONLY | os.O_NONBLOCK)
            os.lseek(self.kmsg, 0, os.SEEK_END)  # only what happens from now on
        except OSError as e:
            log.warning("cannot read the kernel log (%s): out-of-memory kills "
                        "will not be logged or mailed", e)

    def oom_events(self):
        """Out-of-memory kills the kernel has logged since the last call, as
        (cgroup that hit its limit, cgroup of the process chosen, its name). The
        kernel log is used because it still names the user after all their
        processes, and with them their cgroup, are gone."""
        out = []
        while self.kmsg is not None:
            try:
                record = os.read(self.kmsg, 8192).decode("utf-8", "replace")
            except BlockingIOError:
                break  # nothing new
            except OSError as e:
                if e.errno == errno.EPIPE:
                    continue  # older records were overwritten; carry on
                break
            m = OOM_RE.search(record)
            if m:
                out.append(m.groups())
        return out

    def check_oom(self, now):
        seen = {}
        for memcg, victim_cg, process in self.oom_events():
            seen.setdefault(memcg, (victim_cg, process))  # one report per cgroup and check
        for memcg, (victim_cg, process) in seen.items():
            total = memcg == "/user.slice"  # the limit for all users together
            m = re.match(r"/user\.slice/user-(\d+)\.slice", victim_cg if total else memcg + "/")
            if not (total or m):
                continue  # not one of ours
            uid = m.group(1) if m else None
            name = username(uid) if uid else "unknown"
            if total:
                log.warning("OOM all users together hit the total memory limit; all "
                            "processes of %s were killed (largest process on the node: %s)",
                            name, process)
                if self.may_mail(self.state.setdefault("admin_mails", []), now):
                    self.send(self.admin,
                              "[%s] %s: total user memory limit reached" % (self.cluster, self.host),
                              "All users together hit the memory limit of user.slice on %s.\n"
                              "The kernel killed all processes of %s, who had the largest\n"
                              "process (%s). See `journalctl -k` for details and\n"
                              "`frontendshepherd status --all` for current usage.\n"
                              % (self.host, name, process))
            else:
                log.info("OOM %s (uid %s): over the memory limit, all processes killed "
                         "(largest: %s)", name, uid, process)
            if uid and not self.exempt(int(uid)):
                st = self.state["users"].setdefault(uid, self.new_user(now))
                st["events"].append(now)
                self.notify_user(uid, "oom", st, now, process)

    def sheepdog(self, now):
        """SIGTERM old processes whose command line matches a sheepdog pattern."""
        boot = next((int(l.split()[1]) for l in (read_file("/proc/stat") or "").splitlines()
                     if l.startswith("btime")), None)
        if boot is None:
            return
        hz = os.sysconf("SC_CLK_TCK")
        # Only processes in the users' login slices: a user's batch jobs run
        # elsewhere (for example under slurmstepd) and are left alone.
        candidates = []
        for path in glob.glob(os.path.join(self.user_slice, "user-*.slice")):
            m = re.search(r"user-(\d+)\.slice$", path)
            if m and not self.exempt(int(m.group(1))):
                candidates += [(int(m.group(1)), pid) for pid in slice_pids(path)]
        for uid, pid in candidates:
            d = "/proc/%d" % pid
            try:
                cmd = (read_file(d + "/cmdline") or "").replace("\0", " ").strip()
                if not cmd or not any(p.search(cmd) for p in self.sheepdog_patterns):
                    continue
                # field 22 of /proc/PID/stat: start time in ticks since boot
                start = int(read_file(d + "/stat").rsplit(")", 1)[1].split()[19])
                age = now - (boot + start / hz)
                if age < self.sheepdog_max_age:
                    continue
                log.info("SHEEPDOG terminated pid %d of %s, %.0f days old: %s%s", pid, username(uid),
                         age / 86400, cmd[:120], " [dry run]" if self.dry_run else "")
                if not self.dry_run:
                    os.kill(pid, signal.SIGTERM)
            except (OSError, AttributeError, IndexError, ValueError):
                continue  # process went away while we looked at it

    def policy(self):
        return ("%.3g CPUs always free, burst up to %.3g CPUs, bucket of %.3g "
                "CPU-hours refilling in %.3g hours, %.3g GB memory per user"
                % (self.min_cpus, self.burst, self.capacity / 3600,
                   self.refill_hours, self.user_max_gb))

    def run(self):
        if read_file(os.path.join(os.path.dirname(self.user_slice), "cgroup.controllers")) is None:
            sys.exit("cgroup v2 (unified hierarchy) is required")
        self.load_state()
        if self.dry_run:
            log.info("DRY RUN: nothing is limited, no mail is sent, only logging")
        else:
            self.write_dropins()
        self.open_kmsg()
        for sig in (signal.SIGTERM, signal.SIGINT):
            signal.signal(sig, lambda *_: self.stop.set())
        log.info("started: %s", self.policy())
        while not self.stop.is_set():
            try:
                self.tick()
            except Exception:
                log.exception("tick failed")
            self.stop.wait(self.interval)
        # Don't leave anyone throttled with nobody around to release them.
        # The throttled flag stays in the state file and is re-applied on start.
        for uid, st in self.state["users"].items():
            path = os.path.join(self.user_slice, "user-%s.slice" % uid)
            if st["throttled"] and os.path.isdir(path):
                self.ensure_quota(uid, path, self.burst)
        self.save_state()
        log.info("stopped")


def meminfo_total_kb():
    """MemTotal in kB."""
    for line in (read_file("/proc/meminfo") or "").splitlines():
        if line.startswith("MemTotal:"):
            return int(line.split()[1])
    return 0


def slice_pids(path):
    """All process ids in a cgroup and the cgroups below it."""
    pids = []
    for root, _dirs, files in os.walk(path):
        if "cgroup.procs" in files:
            pids += [int(p) for p in (read_file(os.path.join(root, "cgroup.procs")) or "").split()]
    return pids


def top_processes(slice_path, n=8):
    """The busiest processes in a user's login slice (not their batch jobs)."""
    pids = slice_pids(slice_path)
    if not pids:
        return "  (none)"
    try:
        out = subprocess.run(
            ["ps", "-p", ",".join(map(str, pids)), "-o", "pid,pcpu,pmem,etime,args",
             "--sort=-pcpu"],
            stdout=subprocess.PIPE, universal_newlines=True, timeout=10).stdout
        return "\n".join("  " + l[:110] for l in out.splitlines()[:n + 1])
    except (OSError, subprocess.SubprocessError):
        return "  (process list unavailable)"


# --------------------------------------------------------------------------
# status command
# --------------------------------------------------------------------------

def memory_gb(s, uid):
    """Memory a user's processes hold, without file cache (the kernel drops
    cache before it kills anything). None if the user has no slice."""
    path = os.path.join(s.user_slice, "user-%s.slice" % uid)
    cur = read_file(os.path.join(path, "memory.current"))
    if cur is None:
        return None
    stat = read_kv(os.path.join(path, "memory.stat"))
    cache = stat.get("active_file", 0) + stat.get("inactive_file", 0)
    return max(0, int(cur) - cache) / 2**30


def status(s, user=None, show_all=False):
    s.load_state()
    if not os.path.exists(s.state_file):
        sys.exit("No state file at %s - is the daemon running?" % s.state_file)
    if show_all or (user is None and os.geteuid() == 0):
        status_all(s)
        return
    try:
        uid = pwd.getpwnam(user).pw_uid if user else os.getuid()
    except KeyError:
        sys.exit("no such user: %s" % user)
    st = s.state["users"].get(str(uid)) or s.new_user(0)
    hours = s.capacity / 3600
    level = min(s.capacity, st["level"])
    print("%s on %s%s" % (username(uid), s.host, "  [DRY RUN: nothing is enforced]" if s.dry_run else ""))
    if s.exempt(uid):
        print("  This account is not limited.")
        return
    if st["throttled"]:
        if st["cores"] < s.min_cpus * 0.95:
            when = "in about %d min" % ((s.capacity - level) / s.refill / 60)
        else:
            when = "NOT refilling while you use %.3g CPUs or more" % s.min_cpus
        state = "THROTTLED to %.3g CPU(s) until the bucket is full again, %s" % (s.min_cpus, when)
    elif st["cores"] > s.min_cpus * 1.05:
        state = "ok; at this rate the bucket is empty in about %d min" % (
            level / st["cores"] / 60)
    else:
        state = "ok"
    mem = memory_gb(s, uid)
    rows = [
        ("State", state),
        ("CPU in use", "%.1f CPUs (up to %.3g allowed, %.3g are free of charge)"
         % (st["cores"], s.burst, s.min_cpus)),
        ("CPU bucket", "%d%% full (%.1f of %.3g CPU-hours)"
         % (100 * level / s.capacity, level / 3600, hours)),
        ("Memory", "%.1f of %.3g GB" % (mem or 0, s.user_max_gb)),
        ("Warnings", "%d in the last %.3g days" % (len(st["events"]), s.repeat_window / 86400)),
    ]
    for k, v in rows:
        print("  %-11s %s" % (k, v))


def status_all(s):
    users = s.state["users"]
    print("Policy on %s: %s%s\n" % (s.host, s.policy(), "  [DRY RUN]" if s.dry_run else ""))
    fmt = "%-16s %7s %8s %9s %10s  %s"
    print(fmt % ("USER", "CPUS", "BUCKET", "MEM (GB)", "WARNINGS", "STATE"))
    for uid, st in sorted(users.items(), key=lambda kv: kv[1]["level"]):
        mem = memory_gb(s, uid)
        print(fmt % (username(uid)[:16], "%.1f" % st["cores"],
                     "%d%%" % min(100, 100 * st["level"] / s.capacity),
                     "-" if mem is None else "%.1f" % mem,
                     len(st["events"]),
                     "THROTTLED" if st["throttled"] else "ok"))
    if not users:
        print("(no users logged in)")


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("-c", "--config", default=DEFAULT_CONFIG)
    sub = ap.add_subparsers(dest="cmd")
    run = sub.add_parser("run", help="run the daemon (as root)")
    run.add_argument("--dry-run", action="store_true",
                     help="log what would happen, limit nothing, send no mail")
    st = sub.add_parser("status", help="show your own usage and limits")
    st.add_argument("-a", "--all", action="store_true",
                    help="show all users (the default for root)")
    st.add_argument("user", nargs="?", help="show this user instead of yourself")
    rs = sub.add_parser("reset", help="fill a user's bucket and lift the throttle (as root)")
    rs.add_argument("user", nargs="+")
    tm = sub.add_parser("testmail", help="send a test mail to check the [mail] settings (as root)")
    tm.add_argument("address")
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    if args.cmd in ("reset", "testmail") and os.geteuid() != 0:
        sys.exit("frontendshepherd %s can only be run by root" % args.cmd)
    if args.cmd == "run":
        cp = load_config(args.config)
        for name in unknown_settings(cp):
            log.warning("unknown setting in %s, ignored: %s", args.config, name)
        Shepherd(cp, dry_run=args.dry_run).run()
    elif args.cmd == "status":
        logging.disable(logging.WARNING)
        status(Shepherd(load_config(args.config)), args.user, args.all)
    elif args.cmd == "reset":
        s = Shepherd(load_config(args.config))
        for name in args.user:
            try:
                uid = pwd.getpwnam(name).pw_uid
                open(os.path.join(s.state_dir, "reset-%d" % uid), "w").close()
            except KeyError:
                sys.exit("no such user: %s" % name)
            except OSError as e:
                sys.exit("cannot request a reset: %s" % e)
            print("%s: bucket will be reset within %g seconds" % (name, s.interval))
    elif args.cmd == "testmail":
        s = Shepherd(load_config(args.config))
        try:
            s.deliver(args.address, "[%s] frontendshepherd test mail" % s.cluster,
                      "Mail from frontendshepherd on %s works.\n" % s.host)
        except (OSError, subprocess.SubprocessError, smtplib.SMTPException) as e:
            sys.exit("sending failed: %s" % e)
        print("sent to %s via %s" % (args.address, s.smtp_host or s.sendmail))
    else:
        ap.print_help()


if __name__ == "__main__":
    main()
