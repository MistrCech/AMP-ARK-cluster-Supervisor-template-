"""Vyber eventu (resolve_event, event_mods) - python3 -m unittest discover -s tests"""
import datetime
import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "supervisor"))
import arkclustersupervisor as sup  # noqa: E402

EVENTS = {
    "events": {"asa": {"fear": {"name": "Fear Ascended", "mods": ["877752"], "args": ["-HalloweenColors"]},
                       "winter": {"name": "Winter Wonderland", "mods": ["927090"]},
                       "anniversary": {"name": "Anniversary", "mods": [], "args": ["-ActiveEvent=Birthday"]},
                       "nomod": {"name": "Bez modu", "mods": []}}},
    "calendar": [{"event": "fear", "start": "2026-10-20", "end": "2026-11-03"}],
}
D = datetime.date


class ResolveTest(unittest.TestCase):
    def test_off(self):
        for choice in ("off", "", None, "OFF"):
            self.assertEqual(sup.resolve_event(choice, EVENTS, D(2026, 10, 25), "asa")[0], None)

    def test_manual(self):
        key, entry = sup.resolve_event("fear", EVENTS, D(2026, 1, 1), "asa")
        self.assertEqual((key, entry["mods"]), ("fear", ["877752"]))

    def test_official_inside_and_on_both_edges(self):
        for day in (D(2026, 10, 20), D(2026, 10, 25), D(2026, 11, 3)):
            self.assertEqual(sup.resolve_event("official", EVENTS, day, "asa")[0], "fear")

    def test_official_outside(self):
        for day in (D(2026, 10, 19), D(2026, 11, 4)):
            self.assertEqual(sup.resolve_event("official", EVENTS, day, "asa")[0], None)

    def test_unknown_and_missing_mod(self):
        self.assertIsNone(sup.resolve_event("easter", EVENTS, D(2026, 1, 1), "asa")[0])
        self.assertIsNone(sup.resolve_event("nomod", EVENTS, D(2026, 1, 1), "asa")[0])

    def test_other_game_has_no_events(self):
        self.assertIsNone(sup.resolve_event("fear", EVENTS, D(2026, 10, 25), "ase")[0])


class PlanTest(unittest.TestCase):
    BASE = ["947033", "942339"]

    def plan(self, choice, day=D(2026, 10, 25), seen=()):
        return sup.plan_event(choice, EVENTS, day, list(seen), self.BASE, "asa")

    def test_off_and_nothing_seen_changes_nothing(self):
        p = self.plan("off")
        self.assertEqual((p["mods"], p["passive"], p["args"], p["active"]), (self.BASE, [], [], None))

    def test_active_event_adds_mod_and_args(self):
        p = self.plan("fear")
        self.assertEqual(p["mods"], self.BASE + ["877752"])
        self.assertEqual((p["passive"], p["args"], p["seen"]), ([], ["-HalloweenColors"], ["fear"]))

    def test_after_event_mod_stays_passive_and_first(self):
        # Wildcard 33.15: -mods=927090,ModID -passivemods=927090 (pasivni prvni).
        p = self.plan("off", seen=["fear"])
        self.assertEqual(p["mods"], ["877752"] + self.BASE)
        self.assertEqual((p["passive"], p["args"]), (["877752"], []))

    def test_next_event_while_previous_is_passive(self):
        p = self.plan("winter", seen=["fear"])
        self.assertEqual(p["mods"], ["877752"] + self.BASE + ["927090"])
        self.assertEqual((p["passive"], p["seen"]), (["877752"], ["fear", "winter"]))

    def test_event_again_is_not_passive(self):
        p = self.plan("fear", seen=["fear", "winter"])
        self.assertEqual(p["passive"], ["927090"])
        self.assertNotIn("877752", p["passive"])
        self.assertEqual(p["mods"].count("877752"), 1)

    def test_args_only_event(self):
        p = self.plan("anniversary")
        self.assertEqual((p["mods"], p["args"], p["active"]), (self.BASE, ["-ActiveEvent=Birthday"], "anniversary"))

    def test_mod_already_in_base_list(self):
        p = sup.plan_event("off", EVENTS, D(2026, 1, 1), ["fear"], ["877752", "947033"], "asa")
        self.assertEqual(p["mods"], ["877752", "947033"])
        self.assertEqual(p["passive"], ["877752"])


class RepoDataTest(unittest.TestCase):
    """events.json v repu: platny JSON, ASCII (Windows ho cte v kodove strance),
    kazdy event v kalendari existuje a ma mod nebo prepinac, data jsou platna a neprekryvaji se."""

    def setUp(self):
        raw = (ROOT / "supervisor/events.json").read_bytes()
        raw.decode("ascii")
        self.data = json.loads(raw)

    def test_calendar_consistent(self):
        cal = sorted(self.data["calendar"], key=lambda c: (c.get("game", "asa"), c["start"]))
        for c in cal:
            game = c.get("game", "asa")
            entry = self.data["events"][game][c["event"]]
            self.assertTrue(sup._event_mod_ids(entry) or entry.get("args"), c)
            start, end = D.fromisoformat(c["start"]), D.fromisoformat(c["end"])
            self.assertLessEqual(start, end, c)
        for a, b in zip(cal, cal[1:]):
            if a.get("game", "asa") == b.get("game", "asa"):
                self.assertLess(a["end"], b["start"], (a, b))

    def test_template_enum_matches_events(self):
        cfg = json.loads((ROOT / "arkasaclusterconfig.json").read_text(encoding="utf-8"))
        field = next(f for f in cfg if f["FieldName"] == "Event")
        keys = set(field["EnumValues"]) - {"off", "official"}
        self.assertEqual(keys, set(self.data["events"]["asa"]))


if __name__ == "__main__":
    unittest.main()
