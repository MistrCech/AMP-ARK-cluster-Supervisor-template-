"""Testy ChatRestorer: ceska pismena z logu mapy do zprav z GetChat.

  python3 -m unittest discover -s tests

Simulace GetChat vychazi z overeni 9. 10. 2026 (ASA, Windows): hrac napsal 'čus',
log mapy ma 'čus' (UTF-8, C4 8D), GetChat vratil '?us'. Log obsahuje i tribe chat,
GetChat jen globalni - zadna zprava z logu se nesmi dostat tam, kam nepatri.
"""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "supervisor"))
import arkclustersupervisor as sup  # noqa: E402


def getchat(text, keep_cp1252=False):
    """Co z textu udela RCON: znak mimo ASCII (nebo mimo cp1252) -> '?', emoji -> '??'."""
    out = []
    for ch in text:
        if ch.isascii():
            out.append(ch)
            continue
        if keep_cp1252:
            try:
                ch.encode("cp1252")
                out.append(ch)
                continue
            except UnicodeEncodeError:
                pass
        out.append("??" if ord(ch) > 0xFFFF else "?")
    return "".join(out)


class Clock:
    def __init__(self, t=1000.0):
        self.t = t

    def __call__(self):
        return self.t

    def sleep(self, s):
        self.t += s


def restorer(clock):
    return sup.ChatRestorer(clock=clock, sleep=clock.sleep)


class DegradedTest(unittest.TestCase):
    def test_real_case(self):
        self.assertTrue(sup.rcon_degraded("čus", "?us"))

    def test_both_code_page_behaviours(self):
        text = "Příliš žluťoučký kůň úpěl ďábelské ódy"
        self.assertTrue(sup.rcon_degraded(text, getchat(text)))
        self.assertTrue(sup.rcon_degraded(text, getchat(text, keep_cp1252=True)))

    def test_replacement_char_yes_best_fit_no(self):
        self.assertTrue(sup.rcon_degraded("čus", "�us"))
        self.assertFalse(sup.rcon_degraded("čus", "cus"))       # best-fit nebyl videt - nepripoustet
        self.assertFalse(sup.rcon_degraded("kde jsí?", "kde jsi?"))

    def test_emoji_is_two_question_marks(self):
        self.assertTrue(sup.rcon_degraded("gg 😀", "gg ??"))
        self.assertFalse(sup.rcon_degraded("gg 😀", "gg ?"))

    def test_mismatch(self):
        self.assertFalse(sup.rcon_degraded("čus", "?as"))
        self.assertFalse(sup.rcon_degraded("čus", "?uss"))
        self.assertFalse(sup.rcon_degraded("cus", "?us"))     # ASCII pismeno se na '?' nemeni

    def test_same_player(self):
        self.assertTrue(sup._same_player("Mistr Čech", "Mistr ?ech"))
        self.assertTrue(sup._same_player("Šimon", "imon"))      # parse_chat odrizl '?' z kraje
        self.assertFalse(sup._same_player("Čech", "Cech"))      # dva ruzni hraci
        self.assertFalse(sup._same_player("Ведьма", "Anna"))


class RestorerTest(unittest.TestCase):
    def test_restores_message(self):
        c = Clock()
        r = restorer(c)
        r.note("DeNNy", "čus")
        c.t += 3
        self.assertEqual(r.restore("DeNNy", "?us"), ("DeNNy", "čus"))

    def test_each_log_line_only_once(self):
        c = Clock()
        r = restorer(c)
        r.note("DeNNy", "čus")
        self.assertEqual(r.restore("DeNNy", "?us", wait=0), ("DeNNy", "čus"))
        self.assertEqual(r.restore("DeNNy", "?us", wait=0), ("DeNNy", "?us"))

    def test_window(self):
        c = Clock()
        r = restorer(c)
        r.note("DeNNy", "čus")
        c.t += sup.ChatRestorer.WINDOW + 1
        self.assertEqual(r.restore("DeNNy", "?us", wait=0), ("DeNNy", "?us"))

    def test_waits_for_late_log_line(self):
        c = Clock()
        r = restorer(c)
        r.note("Nekdo", "x")                    # log mapy chat obsahuje
        late = {"done": False}

        def sleep(s):
            c.t += s
            if not late["done"]:
                r.note("DeNNy", "čus")
                late["done"] = True
        r._sleep = sleep
        self.assertEqual(r.restore("DeNNy", "?us"), ("DeNNy", "čus"))

    def test_no_chat_in_log_means_no_waiting(self):
        c = Clock()
        r = restorer(c)
        start = c.t
        self.assertEqual(r.restore("DeNNy", "?us"), ("DeNNy", "?us"))
        self.assertEqual(c.t, start)

    def test_gives_up_after_budget(self):
        c = Clock()
        r = restorer(c)
        r.note("Nekdo", "x")
        start = c.t
        self.assertEqual(r.restore("DeNNy", "?us"), ("DeNNy", "?us"))
        self.assertLessEqual(c.t - start, sup.ChatRestorer.WAIT + 0.3)

    def test_not_degraded_returns_at_once_and_consumes_nothing(self):
        c = Clock()
        r = restorer(c)
        r.note("DeNNy", "zdar")
        self.assertEqual(r.restore("DeNNy", "zdar", degraded=False), ("DeNNy", "zdar"))
        self.assertFalse(r._recent[0][3])

    def test_prefers_same_player(self):
        c = Clock()
        r = restorer(c)
        r.note("Anna", "čus")
        r.note("Bara", "ťus")
        self.assertEqual(r.restore("Bara", "?us", wait=0), ("Bara", "ťus"))
        self.assertEqual(r.restore("Anna", "?us", wait=0), ("Anna", "čus"))

    def test_foreign_lines_are_never_taken(self):
        c = Clock()
        r = restorer(c)
        r.note("Bara", "ťus")
        r.note("Ведьма", "čus")                 # jmeno bez latinky (drive pres vyjimku)
        self.assertEqual(r.restore("Anna", "?us", wait=0), ("Anna", "?us"))

    def test_tribe_then_global_same_shape_is_left_alone(self):
        # Review 9. 10.: tribe 'ťus' a hned globalni 'čus' - nelze poznat, ktery je globalni.
        c = Clock()
        r = restorer(c)
        r.note("Anna", "ťus")       # tribe (GetChat ho nikdy nevrati)
        r.note("Anna", "čus")       # globalni
        self.assertEqual(r.restore("Anna", "?us", wait=0), ("Anna", "?us"))

    def test_exact_line_beats_similar_tribe_line(self):
        c = Clock()
        r = restorer(c)
        r.note("Anna", "kde jsí?")  # tribe
        r.note("Anna", "kde jsi?")  # globalni
        self.assertEqual(r.restore("Anna", "kde jsi?", wait=0), ("Anna", "kde jsi?"))
        self.assertFalse(r._recent[0][3])        # tribe radek zustal nepouzity

    def test_literal_question_mark_not_replaced_by_tribe(self):
        c = Clock()
        r = restorer(c)
        r.note("Anna", "čuš")       # tribe
        r.note("Anna", "cu?")       # globalni, otaznik napsany rucne
        self.assertEqual(r.restore("Anna", "cu?", wait=0), ("Anna", "cu?"))

    def test_second_candidate_during_grace_makes_it_ambiguous(self):
        c = Clock()
        r = restorer(c)
        r.note("Anna", "ťus")       # tribe, zatim jediny kandidat
        late = {"done": False}

        def sleep(s):
            c.t += s
            if not late["done"]:
                r.note("Anna", "čus")   # globalni radek docten behem cekani
                late["done"] = True
        r._sleep = sleep
        self.assertEqual(r.restore("Anna", "?us"), ("Anna", "?us"))

    def test_late_line_of_unrestored_message_is_discarded(self):
        # Review 9. 10.: radek prisel az po odeslani '?us' - nesmi pripadnout dalsi zprave.
        c = Clock()
        r = restorer(c)
        r.note("Nekdo", "x")
        self.assertEqual(r.restore("Anna", "?us", wait=0), ("Anna", "?us"))
        r.note("Anna", "čus")       # pozdni radek prvni zpravy
        r.note("Anna", "ťus")       # druha zprava
        self.assertEqual(r.restore("Anna", "?us", wait=0), ("Anna", "ťus"))

    def test_name_with_diacritics(self):
        c = Clock()
        r = restorer(c)
        r.note("Mistr Čech", "ahoj")
        self.assertEqual(r.restore("Mistr ?ech", "ahoj", wait=0), ("Mistr Čech", "ahoj"))

    def test_name_starting_with_diacritics(self):
        # GetChat: '?imon (?imon): ?au' -> parse_chat odrizne '?' na kraji -> 'imon'.
        c = Clock()
        r = restorer(c)
        r.note(*sup.parse_chat("2026.10.09_16.00.57: Šimon (Šimon): čau", sup.LOG_CHAT_RE))
        self.assertEqual(r.restore(*sup.parse_chat("?imon (?imon): ?au"), wait=0), ("Šimon", "čau"))

    def test_platform_icon_dropped_from_name(self):
        c = Clock()
        r = restorer(c)
        r.note("DeNNy", "čus")
        self.assertEqual(r.restore("DeNNy", "?us", wait=0), ("DeNNy", "čus"))

    def test_parse_chat_round_trip(self):
        # Radek logu a radek GetChat projdou stejnym parse_chat jako v supervisoru.
        c = Clock()
        r = restorer(c)
        r.note(*sup.parse_chat("2026.10.09_16.00.57: DeNNy (DeNNy): čus", sup.LOG_CHAT_RE))
        self.assertEqual(r.restore(*sup.parse_chat("DeNNy (DeNNy): ?us"), wait=0), ("DeNNy", "čus"))


if __name__ == "__main__":
    unittest.main()
