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
        self.set(0)

    def set(self, ooms):
        with open(os.path.join(self.slice, "cpu.stat"), "w") as f:
            f.write("usage_usec %d\nuser_usec 0\n" % (self.cpu * 1e6))
        with open(os.path.join(self.slice, "memory.events"), "w") as f:
            f.write("low 0\noom_kill %d\n" % ooms)

    def run(self, cores, seconds, ooms=0, step=10):
        """Advance time with the user burning `cores` cores."""
        for _ in range(int(seconds / step)):
            self.now += step
            self.cpu += cores * step
            self.set(ooms)
            self.s.tick(self.now)
        return self.s.state["users"].get(UID)


class TestBucket(unittest.TestCase):
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

    def test_bucket_arithmetic(self):
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
        f.set(0)
        f.now += 10
        f.s.tick(f.now)
        self.assertAlmostEqual(st["level"], level + 60 * f.s.refill - 100)

    def test_idle_user_is_forgotten(self):
        f = Fake()
        f.s.tick(f.now)
        os.rename(f.slice, f.slice + ".gone")
        f.s.tick(f.now + 10)
        self.assertEqual(f.s.state["users"], {})

    def test_admin_is_not_copied_by_default(self):
        f = Fake()
        f.s.cooldown = 0
        f.s.tick(f.now)
        for n in range(1, 6):
            f.run(cores=0, seconds=10, ooms=n)
        self.assertEqual([m[2] for m in f.mails], [None] * 5)

    def test_oom_mail_and_repeat_offender_cc(self):
        f = Fake()
        f.s.cooldown = 0
        f.s.repeat_events = 3
        f.s.tick(f.now)
        f.run(cores=0, seconds=10, ooms=1)
        f.run(cores=0, seconds=10, ooms=2)
        self.assertEqual([m[2] for m in f.mails], [None, None])
        f.run(cores=0, seconds=10, ooms=3)
        self.assertEqual(f.mails[-1][2], f.s.admin)
        self.assertIn("out of memory", f.mails[-1][1])

    def test_mail_cooldown(self):
        f = Fake()
        f.s.tick(f.now)
        f.run(cores=0, seconds=10, ooms=1)
        f.run(cores=0, seconds=10, ooms=2)
        self.assertEqual(len(f.mails), 1)

    def test_dropins(self):
        f = Fake()
        d = f.s.dropins()
        self.assertIn("MemoryMax=32768M", d["user-.slice.d"])
        self.assertIn("CPUQuota=1600%", d["user-.slice.d"])
        self.assertIn("MemorySwapMax=0\n", d["user-.slice.d"])
        self.assertIn("MemoryMax=infinity", d["user-0.slice.d"])
        self.assertRegex(d["user.slice.d"], r"MemoryMax=\d+M")


class TestConfig(unittest.TestCase):
    def test_defaults(self):
        s = fs.Shepherd(fs.load_config("/nonexistent"))
        self.assertEqual((s.min_cpus, s.capacity, s.refill_hours), (2, 8 * 3600, 4))
        self.assertEqual((s.user_max_gb, s.reserved_gb), (16, 8))
        self.assertLessEqual(s.burst, 8)


class TestMailTexts(unittest.TestCase):
    def sent(self, f):
        out = []
        f.s.send = lambda to, subject, body, cc=None: out.append((subject, body))
        f.s.notify_user(UID, "oom", f.s.new_user(0), 1e9)
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
        self.assertIn("ran out of memory", self.sent(f)[1])


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
