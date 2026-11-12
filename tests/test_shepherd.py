"""Runs the daemon logic against a fake cgroup tree. No root needed.

    python3 tests/test_shepherd.py
"""
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import frontendshepherd as fs

UID = "54321"  # no such user needed; falls back to the number


class Fake:
    """A cgroup tree with one user slice whose counters we control."""

    def __init__(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.slice = os.path.join(self.tmp.name, "cg", "user.slice", "user-%s.slice" % UID)
        os.makedirs(self.slice)
        cp = fs.load_config("/nonexistent")
        cp["general"]["cgroup_root"] = os.path.join(self.tmp.name, "cg")
        cp["general"]["state_dir"] = self.tmp.name
        cp["cpu"].update(max_cpus="16", min_cpus="2", refill_hours="4")
        cp["memory"].update(user_max_gb="32", reserved_gb="1")
        self.cp = cp
        self.s = fs.Shepherd(cp, dry_run=True)
        self.mails = []
        self.s.send = lambda to, subject, body, cc=None: self.mails.append((to, subject, cc))
        self.now = 1_000_000.0
        self.cpu = 0.0
        self.set()

    def set(self):
        with open(os.path.join(self.slice, "cpu.stat"), "w") as f:
            f.write("usage_usec %d\nuser_usec 0\n" % (self.cpu * 1e6))

    def run(self, cores, seconds, step=10):
        """Advance time with the user burning `cores` cores."""
        for _ in range(int(seconds / step)):
            self.now += step
            self.cpu += cores * step
            self.set()
            self.s.tick(self.now)
        return self.s.state["users"].get(UID)


class TestBudget(unittest.TestCase):
    def test_light_use_is_free(self):
        f = Fake()
        f.s.tick(f.now)
        st = f.run(cores=2, seconds=24 * 3600, step=60)
        self.assertEqual(st["level"], f.s.capacity)
        self.assertFalse(st["throttled"])
        self.assertEqual(f.mails, [])

    def test_burst_then_throttle_then_release(self):
        f = Fake()
        f.s.tick(f.now)
        # 8 CPU-hours at 16 CPUs last exactly 30 minutes
        st = f.run(cores=16, seconds=29 * 60)
        self.assertFalse(st["throttled"])
        st = f.run(cores=16, seconds=60)
        self.assertTrue(st["throttled"])
        self.assertEqual(len(f.mails), 1)
        self.assertIn("CPU usage", f.mails[0][1])
        # the job keeps running, pinned at the throttled 2 CPUs: no refill
        st = f.run(cores=2, seconds=12 * 3600, step=60)
        self.assertTrue(st["throttled"])
        self.assertEqual(st["level"], 0)
        # the job ends; light use below min_cpus refills. Released only
        # when completely full, after refill_hours = 4
        st = f.run(cores=1, seconds=4 * 3600 - 60)
        self.assertTrue(st["throttled"])
        st = f.run(cores=1, seconds=60)
        self.assertFalse(st["throttled"])
        self.assertEqual(st["level"], f.s.capacity)

    def test_admin_reset(self):
        f = Fake()
        f.s.tick(f.now)
        st = f.run(cores=16, seconds=30 * 60)
        self.assertTrue(st["throttled"])
        open(os.path.join(f.tmp.name, "reset-" + UID), "w").close()
        st = f.run(cores=1, seconds=10)
        self.assertFalse(st["throttled"])
        self.assertEqual(st["level"], f.s.capacity)
        self.assertFalse(os.path.exists(os.path.join(f.tmp.name, "reset-" + UID)))

    def test_each_user_gets_a_private_copy_of_their_own_record(self):
        import json, stat
        f = Fake()
        f.run(1, 20)
        own = os.path.join(f.tmp.name, "user-%s.json" % UID)
        for path in (own, os.path.join(f.tmp.name, "state.json")):
            self.assertEqual(stat.S_IMODE(os.stat(path).st_mode), 0o600)
        with open(own) as fh:
            self.assertEqual(json.load(fh), f.s.state["users"][UID])
        # gone again when the user is forgotten
        del f.s.state["users"][UID]
        f.s.save_state()
        self.assertFalse(os.path.exists(own))

    def test_budget_arithmetic(self):
        for cores, hours in ((8, 1), (4, 2)):
            f = Fake()
            f.s.tick(f.now)
            st = f.run(cores=cores, seconds=hours * 3600 - 60)
            self.assertFalse(st["throttled"])
            st = f.run(cores=cores, seconds=60)
            self.assertTrue(st["throttled"])

    def test_logout_keeps_the_record_and_new_slice_is_charged(self):
        f = Fake()
        f.s.tick(f.now)
        f.run(cores=16, seconds=10 * 60)
        level = f.s.state["users"][UID]["level"]
        os.rename(f.slice, f.slice + ".gone")
        f.now += 60
        f.s.tick(f.now)
        st = f.s.state["users"][UID]
        self.assertAlmostEqual(st["level"], level + 60 * f.s.refill)
        # log in again: the fresh slice already burned 100 core-seconds
        os.rename(f.slice + ".gone", f.slice)
        f.cpu = 100.0
        f.set()
        f.now += 10
        f.s.tick(f.now)
        self.assertAlmostEqual(st["level"], level + 60 * f.s.refill - 100)

    def test_idle_user_is_forgotten(self):
        f = Fake()
        f.s.tick(f.now)
        os.rename(f.slice, f.slice + ".gone")
        f.s.tick(f.now + 10)
        self.assertEqual(f.s.state["users"], {})

    def oom(self, f, memcg=None, process="python3"):
        """Let the next check see one kernel OOM kill."""
        own = "/user.slice/user-%s.slice" % UID
        f.s.oom_events = lambda: [(memcg or own, own + "/session-1.scope", process)]
        f.run(cores=0, seconds=10)
        f.s.oom_events = lambda: []

    def test_admin_is_not_copied_by_default(self):
        f = Fake()
        f.s.tick(f.now)
        for _ in range(5):
            self.oom(f)
        self.assertEqual([m[2] for m in f.mails], [None] * 5)

    def test_oom_mail_and_repeat_offender_cc(self):
        f = Fake()
        f.s.repeat_events = 3
        f.s.tick(f.now)
        self.oom(f)
        self.oom(f)
        self.assertEqual([m[2] for m in f.mails], [None, None])
        self.oom(f)
        self.assertEqual(f.mails[-1][2], f.s.admin)
        self.assertIn("were killed", f.mails[-1][1])

    def test_max_mails_per_day(self):
        f = Fake()
        f.s.max_mails = 3
        f.s.tick(f.now)
        for _ in range(5):
            self.oom(f)
        self.assertEqual(len(f.mails), 3)
        f.now += 86400          # a day later the user can be mailed again
        self.oom(f)
        self.assertEqual(len(f.mails), 4)

    def test_oom_is_reported_after_the_users_slice_is_gone(self):
        f = Fake()
        f.s.tick(f.now)
        os.rename(f.slice, f.slice + ".gone")   # everything of the user was killed
        own = "/user.slice/user-%s.slice" % UID
        f.s.oom_events = lambda: [(own, own + "/session-1.scope", "python3")]
        f.s.tick(f.now + 10)
        self.assertEqual(len(f.mails), 1)
        self.assertEqual(len(f.s.state["users"][UID]["events"]), 1)

    def test_total_limit_mails_the_admin_and_the_killed_user(self):
        f = Fake()
        f.s.tick(f.now)
        self.oom(f, memcg="/user.slice")
        self.assertEqual([m[0] for m in f.mails], [f.s.admin, UID])
        self.assertEqual(len(f.s.state["users"][UID]["events"]), 1)

    def test_kernel_log_line_is_parsed(self):
        line = ("6,2101,7791234,-;oom-kill:constraint=CONSTRAINT_MEMCG,nodemask=(null),"
                "cpuset=user.slice,mems_allowed=0,oom_memcg=/user.slice/user-1000.slice,"
                "task_memcg=/user.slice/user-1000.slice/session-12.scope,task=python3,"
                "pid=10635,uid=1000")
        self.assertEqual(fs.OOM_RE.search(line).groups(),
                         ("/user.slice/user-1000.slice",
                          "/user.slice/user-1000.slice/session-12.scope", "python3"))

    def test_dropins(self):
        f = Fake()
        d = f.s.dropins()
        self.assertIn("MemoryMax=32768M", d["user-.slice.d"])
        self.assertIn("CPUQuota=1600%", d["user-.slice.d"])
        self.assertIn("MemorySwapMax=0\n", d["user-.slice.d"])
        self.assertIn("MemoryMax=infinity", d["user-0.slice.d"])
        self.assertRegex(d["user.slice.d"], r"MemoryMax=\d+M")


class TestSliceOnly(unittest.TestCase):
    """The sheepdog and the mail's process list only look inside user.slice."""

    def setUp(self):
        import subprocess
        self.f = Fake()
        self.inside = subprocess.Popen(["sleep", "60"])
        self.outside = subprocess.Popen(["sleep", "60"])
        scope = os.path.join(self.f.slice, "session-1.scope")
        os.makedirs(scope)
        with open(os.path.join(scope, "cgroup.procs"), "w") as fh:
            fh.write("%d\n" % self.inside.pid)
        self.f.s.exempt = lambda uid: False

    def tearDown(self):
        for p in (self.inside, self.outside):
            p.kill()
            p.wait()

    def test_sheepdog_leaves_processes_outside_the_slice_alone(self):
        s = self.f.s
        s.dry_run = False
        s.sheepdog_patterns = [fs.re.compile("sleep 60")]
        s.sheepdog_max_age = 0
        s.sheepdog(fs.time.time() + 10)
        self.assertEqual(self.inside.wait(timeout=5), -fs.signal.SIGTERM)
        self.assertIsNone(self.outside.poll())

    def test_process_list_only_has_the_slice(self):
        out = fs.top_processes(self.f.slice)
        self.assertIn(str(self.inside.pid), out)
        self.assertNotIn(str(self.outside.pid), out)


class TestConfig(unittest.TestCase):
    def test_defaults(self):
        s = fs.Shepherd(fs.load_config("/nonexistent"))
        self.assertEqual((s.min_cpus, s.capacity, s.refill_hours), (2, 8 * 3600, 4))
        self.assertEqual((s.user_max_gb, s.reserved_gb), (16, 8))
        self.assertLessEqual(s.burst, 8)

    def test_unknown_settings_are_found(self):
        cp = fs.load_config("/nonexistent")
        self.assertEqual(fs.unknown_settings(cp), [])
        cp.read_string("[cpu]\nmin_cpu = 1\n[reaper]\nenabled = true\n")
        self.assertEqual(fs.unknown_settings(cp), ["[cpu] min_cpu", "[reaper]"])

    def test_shipped_config_has_no_unknown_settings(self):
        conf = os.path.join(os.path.dirname(__file__), "..", "frontendshepherd.conf")
        self.assertEqual(fs.unknown_settings(fs.load_config(conf)), [])


class TestMailTexts(unittest.TestCase):
    def sent(self, f):
        out = []
        f.s.send = lambda to, subject, body, cc=None: out.append((subject, body))
        f.s.notify_user(UID, "oom", f.s.new_user(0), 1e9, "python3")
        return out[0]

    def test_shipped_config_matches_the_built_in_texts(self):
        conf = os.path.join(os.path.dirname(__file__), "..", "frontendshepherd.conf")
        s = fs.Shepherd(fs.load_config(conf))
        self.assertEqual(s.messages, fs.MESSAGES)

    def test_custom_text(self):
        f = Fake()
        f.s.messages["oom_subject"] = "Hej {user}"
        f.s.messages["oom_body"] = "Max {user_max_gb} GB paa {host}."
        subject, body = self.sent(f)
        self.assertEqual(subject, "Hej " + UID)
        self.assertEqual(body, "Max 32 GB paa %s.\n" % f.s.host)

    def test_broken_text_falls_back_to_the_default(self):
        f = Fake()
        f.s.messages["oom_body"] = "Hi {nosuchthing}"
        self.assertIn("The largest process was `python3`", self.sent(f)[1])


class TestSmtp(unittest.TestCase):
    def test_mail_goes_through_the_smtp_server(self):
        import socketserver
        import threading
        got = []

        class Handler(socketserver.StreamRequestHandler):
            def handle(self):
                self.wfile.write(b"220 fake\r\n")
                for line in self.rfile:
                    cmd = line[:4].upper()
                    if cmd == b"DATA":
                        self.wfile.write(b"354 go\r\n")
                        data = b""
                        for l in self.rfile:
                            if l == b".\r\n":
                                break
                            data += l
                        got.append(data.decode())
                    elif cmd == b"QUIT":
                        self.wfile.write(b"221 bye\r\n")
                        return
                    self.wfile.write(b"250 ok\r\n")

        with socketserver.TCPServer(("127.0.0.1", 0), Handler) as srv:
            threading.Thread(target=srv.serve_forever, daemon=True).start()
            f = Fake()
            f.cp["mail"].update(smtp_host="127.0.0.1", smtp_security="none",
                                smtp_port=str(srv.server_address[1]),
                                address="{user}@example.org")
            s = fs.Shepherd(f.cp)
            s.send("someone@example.org", "hello", "body text\n", cc="admin@example.org")
            srv.shutdown()
        self.assertEqual(len(got), 1)
        self.assertIn("Subject: hello", got[0])
        self.assertIn("body text", got[0])


if __name__ == "__main__":
    unittest.main()
