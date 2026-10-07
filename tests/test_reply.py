import unittest

from slate.voice.reply import Sentences


class ReplyTests(unittest.TestCase):
    def test_preserves_words_across_arbitrary_delta_boundaries(self):
        text = "This is the first sentence. This is the second sentence! A short tail"
        for width in (1, 4, 19, 200):
            with self.subTest(width=width):
                sentences = Sentences()
                chunks = []
                for start in range(0, len(text), width):
                    chunks.extend(sentences.feed(text[start : start + width]))
                chunks.extend(sentences.feed("", final=True))
                self.assertEqual(" ".join(chunks), text)
                self.assertEqual(sentences.feed("", final=True), [])

    def test_bounds_long_utterances_and_holds_incomplete_words(self):
        sentences = Sentences(limit=40)
        self.assertEqual(sentences.feed("The beginning of an unfinished"), [])
        chunks = sentences.feed(" sentence " + "many words " * 20, final=True)
        self.assertTrue(all(0 < len(piece) <= 40 for piece in chunks))
        self.assertTrue(
            " ".join(chunks).startswith("The beginning of an unfinished sentence")
        )
