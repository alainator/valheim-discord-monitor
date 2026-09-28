import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from admin_bot import StatusChannel  # noqa: E402


class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


class StatusChannelTest(unittest.TestCase):
    def setUp(self):
        self.clock = Clock()
        self.st = StatusChannel({"channel_id": "1"}, "Alheim", clock=self.clock)
        self.st.current = "old name"

    def test_names(self):
        self.st.set_state(False, 3)
        self.assertEqual(self.st.wanted, "🟢 Valheim: 3 online")
        self.st.set_state(False, 0)
        self.assertEqual(self.st.wanted, "🟢 Valheim: empty")
        self.st.set_state(True, 5)
        self.assertEqual(self.st.wanted, "🔴 Valheim: offline")

    def test_unknown_count_keeps_the_current_name(self):
        self.st.set_state(False, None)
        self.assertIsNone(self.st.due())

    def test_custom_template_and_length_cap(self):
        st = StatusChannel({"channel_id": "1", "online": "⚔️ {server}: {count}/10" + "x" * 200}, "Alheim")
        st.set_state(False, 2)
        self.assertTrue(st.wanted.startswith("⚔️ Alheim: 2/10"))
        self.assertEqual(len(st.wanted), 100)

    def test_renames_are_spaced_and_only_the_latest_applies(self):
        self.st.set_state(False, 1)
        self.assertEqual(self.st.due(), "🟢 Valheim: 1 online")
        self.st.renamed(self.st.due())
        for n in (2, 3, 4, 2):                   # a burst of joins/leaves
            self.st.set_state(False, n)
            self.clock.t += 30
            self.assertIsNone(self.st.due())     # too soon after the last rename
        self.clock.t += 300
        self.assertEqual(self.st.due(), "🟢 Valheim: 2 online")

    def test_no_rename_when_name_unchanged(self):
        self.st.set_state(False, 1)
        self.st.renamed(self.st.due())
        self.clock.t += 1000
        self.st.set_state(False, 1)
        self.assertIsNone(self.st.due())

    def test_interval_cannot_go_below_discords_limit(self):
        st = StatusChannel({"channel_id": "1", "min_interval_seconds": 10}, "S")
        self.assertEqual(st.min_interval, 300)


if __name__ == "__main__":
    unittest.main()
