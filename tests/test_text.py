import unittest

from speakeasy.text import apply_replacements, clean, remove_fillers

FILLERS = {"cleanup": {"fillers": ["um", "uh", "hmm", "ah", "mm"]}}


class RemoveFillers(unittest.TestCase):
    def check(self, before, after):
        self.assertEqual(remove_fillers(FILLERS, before), after)

    def test_sentence_start_is_recapitalized(self):
        self.check("Uh really liking this. Um so hmm I'm thinking.", "Really liking this. So I'm thinking.")

    def test_mid_sentence_commas(self):
        self.check("So, um, the thing is, uh, working.", "So, the thing is, working.")

    def test_trailing_filler(self):
        self.check("Is it running? Hmm.", "Is it running?")

    def test_words_containing_fillers_are_kept(self):
        self.check("The umbrella in Humber, uh-huh, ahead of the hummus.",
                   "The umbrella in Humber, uh-huh, ahead of the hummus.")

    def test_no_fillers_configured(self):
        self.assertEqual(remove_fillers({}, "um, hi"), "um, hi")


class Replacements(unittest.TestCase):
    cfg = {"replacements": {"kubectl": ["cube CTL"], "opencode": ["open code"]}}

    def test_separators_are_interchangeable(self):
        for heard in ("cube CTL", "Cube-CTL", "cube/ctl", "cube.ctl"):
            self.assertEqual(apply_replacements(self.cfg, f"run {heard} now"), "run kubectl now")

    def test_whole_words_only(self):
        self.assertEqual(apply_replacements(self.cfg, "reopen codes"), "reopen codes")

    def test_clean_runs_both(self):
        cfg = {**self.cfg, **FILLERS}
        self.assertEqual(clean(cfg, "Um, open code is up."), "Opencode is up.")


if __name__ == "__main__":
    unittest.main()
