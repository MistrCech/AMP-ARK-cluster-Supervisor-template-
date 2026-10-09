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


NEG = float("-inf")


def ask(r, c, player, msg, since=None, eof=True, wait=1.5, degraded=True):
    """Jako chat_loop: predchozi otazka pred 10 s, odpoved prisla ted, vlakno logu (eof=True) hned docte soubor."""
    since = c() - 10 if since is None else since
    replied = c()
    c.t += 0.01
    if eof:
        r.mark_eof()
    return r.restore(player, msg, degraded=degraded, since=since, replied=replied, wait=wait)


class RestorerTest(unittest.TestCase):
    def test_restores_message(self):
        c = Clock()
        r = restorer(c)
        r.note("DeNNy", "čus")
        self.assertEqual(ask(r, c, "DeNNy", "?us"), ("DeNNy", "čus"))

    def test_each_log_line_only_once(self):
        c = Clock()
        r = restorer(c)
        r.note("DeNNy", "čus")
        self.assertEqual(ask(r, c, "DeNNy", "?us"), ("DeNNy", "čus"))
        self.assertEqual(ask(r, c, "DeNNy", "?us"), ("DeNNy", "?us"))

    def test_lines_before_previous_poll_are_not_candidates(self):
        c = Clock(100)
        r = restorer(c)
        r.note("Anna", "čus")       # pred predchozi otazkou GetChat - uz ji vratila
        c.t = 110
        r.note("Anna", "ťus")
        self.assertEqual(ask(r, c, "Anna", "?us", since=105), ("Anna", "ťus"))

    def test_waits_until_log_is_read_after_reply(self):
        c = Clock()
        r = restorer(c)
        r.note("Nekdo", "x")
        replied = c()

        def sleep(s):
            c.t += s
            if r.last_eof < replied:
                r.note("DeNNy", "čus")      # vlakno logu docte radek ...
                r.mark_eof()                # ... a dojde na konec souboru
        r._sleep = sleep
        self.assertEqual(r.restore("DeNNy", "?us", since=c() - 10, replied=replied), ("DeNNy", "čus"))

    def test_log_not_read_in_time_fails_closed(self):
        c = Clock()
        r = restorer(c)
        r.note("Anna", "ťus")       # tribe; globalni radek jeste neni docteny
        start = c.t
        self.assertEqual(ask(r, c, "Anna", "?us", eof=False, wait=1.0), ("Anna", "?us"))
        self.assertLessEqual(c.t - start, 1.2)

    def test_no_chat_in_log_means_no_waiting(self):
        c = Clock()
        r = restorer(c)
        start = c.t
        self.assertEqual(ask(r, c, "DeNNy", "?us", eof=False), ("DeNNy", "?us"))
        self.assertLessEqual(c.t - start, 0.02)

    def test_not_degraded_returns_at_once_and_consumes_nothing(self):
        c = Clock()
        r = restorer(c)
        r.note("DeNNy", "zdar")
        self.assertEqual(ask(r, c, "DeNNy", "zdar", degraded=False), ("DeNNy", "zdar"))
        self.assertFalse(r._recent[0][3])

    def test_prefers_same_player(self):
        c = Clock()
        r = restorer(c)
        r.note("Anna", "čus")
        r.note("Bara", "ťus")
        self.assertEqual(ask(r, c, "Bara", "?us"), ("Bara", "ťus"))
        self.assertEqual(ask(r, c, "Anna", "?us"), ("Anna", "čus"))

    def test_foreign_lines_are_never_taken(self):
        c = Clock()
        r = restorer(c)
        r.note("Bara", "ťus")
        r.note("Ведьма", "čus")
        self.assertEqual(ask(r, c, "Anna", "?us"), ("Anna", "?us"))

    def test_tribe_then_global_same_shape_is_left_alone(self):
        c = Clock()
        r = restorer(c)
        r.note("Anna", "ťus")       # tribe (GetChat ho nikdy nevrati)
        r.note("Anna", "čus")       # globalni
        self.assertEqual(ask(r, c, "Anna", "?us"), ("Anna", "?us"))

    def test_exact_line_beats_similar_tribe_line(self):
        c = Clock()
        r = restorer(c)
        r.note("Anna", "kde jsí?")  # tribe
        r.note("Anna", "kde jsi?")  # globalni
        self.assertEqual(ask(r, c, "Anna", "kde jsi?"), ("Anna", "kde jsi?"))
        self.assertFalse(r._recent[0][3])        # tribe radek zustal nepouzity

    def test_literal_question_mark_not_replaced_by_tribe(self):
        c = Clock()
        r = restorer(c)
        r.note("Anna", "čuš")
        r.note("Anna", "cu?")
        self.assertEqual(ask(r, c, "Anna", "cu?"), ("Anna", "cu?"))

    def test_names_that_collapse_to_the_same_are_left_alone(self):
        # Review 9. 10. (2. kolo): 'Čech' i 'Ťech' prijdou z GetChat jako 'ech'.
        c = Clock()
        r = restorer(c)
        r.note("Čech", "čus")
        r.note("Ťech", "ťus")
        self.assertEqual(ask(r, c, "ech", "?us"), ("ech", "?us"))
        r2 = restorer(c)
        r2.note("Čech", "ahoj")
        r2.note("Ťech", "ahoj")
        self.assertEqual(ask(r2, c, "ech", "ahoj"), ("ech", "ahoj"))

    def test_late_line_of_unrestored_message_is_discarded(self):
        c = Clock()
        r = restorer(c)
        r.note("Nekdo", "x")
        self.assertEqual(ask(r, c, "Anna", "?us"), ("Anna", "?us"))
        r.note("Anna", "čus")       # pozdni radek prvni zpravy
        r.note("Anna", "ťus")       # druha zprava
        self.assertEqual(ask(r, c, "Anna", "?us"), ("Anna", "ťus"))

    def test_eviction_fails_closed(self):
        c = Clock()
        r = sup.ChatRestorer(clock=c, sleep=c.sleep, maxlen=3)
        r.note("Anna", "čus")
        for i in range(3):
            c.t += 0.1
            r.note("Bara", f"spam {i}")         # tribe spam vytlaci globalni radek
        self.assertEqual(ask(r, c, "Anna", "?us"), ("Anna", "?us"))

    def test_interval_not_fully_in_memory_fails_closed(self):
        # Review 9. 10. (3. kolo): mapa dlouho bez GetChat - globalni radek vypadl z WINDOW.
        c = Clock(1000)
        r = restorer(c)
        r.note("Anna", "ťus")       # globalni, starsi nez WINDOW
        c.t = 1150
        r.note("Anna", "čus")       # tribe
        c.t = 1160
        self.assertEqual(ask(r, c, "Anna", "?us", since=900), ("Anna", "?us"))
        self.assertEqual(ask(r, c, "Anna", "?us", since=NEG), ("Anna", "?us"))   # prvni otazka po startu

    def test_name_with_diacritics(self):
        c = Clock()
        r = restorer(c)
        r.note("Mistr Čech", "ahoj")
        self.assertEqual(ask(r, c, "Mistr ?ech", "ahoj"), ("Mistr Čech", "ahoj"))

    def test_name_starting_with_diacritics(self):
        c = Clock()
        r = restorer(c)
        r.note(*sup.parse_chat("2026.10.09_16.00.57: Šimon (Šimon): čau", sup.LOG_CHAT_RE))
        self.assertEqual(ask(r, c, *sup.parse_chat("?imon (?imon): ?au")), ("Šimon", "čau"))

    def test_platform_icon_dropped_from_name(self):
        c = Clock()
        r = restorer(c)
        r.note("\ue000DeNNy", "čus")
        self.assertEqual(ask(r, c, "DeNNy", "?us"), ("DeNNy", "čus"))

    def test_parse_chat_round_trip(self):
        c = Clock()
        r = restorer(c)
        r.note(*sup.parse_chat("2026.10.09_16.00.57: DeNNy (DeNNy): čus", sup.LOG_CHAT_RE))
        self.assertEqual(ask(r, c, *sup.parse_chat("DeNNy (DeNNy): ?us")), ("DeNNy", "čus"))


if __name__ == "__main__":
    unittest.main()
