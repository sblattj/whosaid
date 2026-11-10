"""Synthetic tests for lib/voice_lint.py (invented phrases only)."""
import io, json, os, sys, tempfile, unittest
from contextlib import redirect_stdout
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "lib"))
import voice_lint as V

P = {"max_words": 12, "max_paragraphs": 1, "greeting": r"(hi|hey)\s+\w+",
     "closers": ["see the notes", "cheers!"], "ask_phrases": ["shall i"],
     "banned_words": ["zorp"], "banned_chars": ["~"],
     "groups": {"max_words": "length", "max_paragraphs": "length", "greeting": "shape"}}


class VoiceLint(unittest.TestCase):
    def test_empty_profile_hits_nothing(self):
        r = V.lint("Hey Quill, shall I zorp the widget?\n\nCheers!", {})
        self.assertEqual(r["score"], 0)
        self.assertEqual(r["paragraphs"], 2)

    def test_plain_short_reply_is_clean(self):
        r = V.lint("ok the widget build is green now, I'll ship it after lunch", P)
        self.assertEqual(r["score"], 0, r)

    def test_each_rule_fires(self):
        text = ("Hey Quill, the widget build is green and the gadget build is red today.\n\n"
                "Shall I zorp it ~ now? See the notes. Cheers!")
        r = V.lint(text, P)
        self.assertEqual(set(r["hits"]), set(V.RULES), r)
        self.assertEqual(r["score"], 7)
        self.assertEqual(r["by_group"], {"length": 2, "shape": 1})

    def test_links_count_as_their_label(self):
        long = "https://example.invalid/" + "a/" * 50
        r = V.lint(f"see [#12]({long}) and {long} ok", {"max_words": 4})
        self.assertEqual(r["words"], 4)
        self.assertNotIn("max_words", r["hits"])

    def test_whole_word_and_phrase_only(self):
        r = V.lint("the zorpless shallish item", P)
        self.assertNotIn("banned_words", r["hits"])
        self.assertNotIn("ask_phrases", r["hits"])
        r = V.lint("ZORP, shall\n i", P)
        self.assertIn("banned_words", r["hits"])
        self.assertIn("ask_phrases", r["hits"])

    def test_closer_only_in_last_paragraph(self):
        r = V.lint("cheers! first\n\nthen the end", {"closers": ["cheers!"]})
        self.assertNotIn("closers", r["hits"])

    def test_greeting_only_at_start(self):
        r = V.lint("the gadget says hey Quill", P)
        self.assertNotIn("greeting", r["hits"])

    def test_default_profile_flags_a_heads_up_opener(self):
        for t in ("heads up, the build is red on main", "Heads-up: the build is red", "headsup the build is red"):
            self.assertIn("greeting", V.lint(t, V.DEFAULT_PROFILE)["hits"], t)
        for t in ("the build is red, just a heads up", "headsets up front are fine"):
            self.assertNotIn("greeting", V.lint(t, V.DEFAULT_PROFILE)["hits"], t)

    def test_cli_fail_over_and_unknown_key(self):
        d = tempfile.mkdtemp()
        prof, msg = os.path.join(d, "p.json"), os.path.join(d, "m.txt")
        json.dump({"banned_words": ["zorp"]}, open(prof, "w"))
        open(msg, "w").write("zorp it")
        with redirect_stdout(io.StringIO()) as out:
            self.assertEqual(V.main(["--profile", prof, "--fail-over", "0", msg]), 1)
        self.assertEqual(json.loads(out.getvalue())["score"], 1)
        with redirect_stdout(io.StringIO()):
            self.assertEqual(V.main(["--profile", prof, msg]), 0)
        json.dump({"max_wordz": 3}, open(prof, "w"))
        with redirect_stdout(io.StringIO()):
            self.assertEqual(V.main(["--profile", prof, msg]), 2)


if __name__ == "__main__":
    unittest.main()
