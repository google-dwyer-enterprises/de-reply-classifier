"""Unit tests for followup_features deterministic logic: extract_new_text
(quoted-thread stripping — the BLOCKING prereq for the whole follow-up analysis),
length_bucket, and deterministic_features. Pure functions, no DB.
Run: python -m unittest discover -s tests
"""
import sys
import unittest
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from followup_features import extract_new_text, deterministic_features, length_bucket


class TestExtractNewText(unittest.TestCase):
    def test_gmail_on_wrote_boundary(self):
        body = ("Hi Bob, are you free Tuesday? "
                "On Mon, Jun 15, 2026 at 2:00 PM John Doe <j@x.com> wrote: old quoted stuff")
        nt, found = extract_new_text(None, body)
        self.assertTrue(found)
        self.assertEqual(nt, "Hi Bob, are you free Tuesday?")

    def test_no_boundary(self):
        nt, found = extract_new_text(None, "Just following up — any thoughts?")
        self.assertFalse(found)
        self.assertEqual(nt, "Just following up — any thoughts?")

    def test_empty_and_none_body(self):
        self.assertEqual(extract_new_text(None, ""), ("", False))
        self.assertEqual(extract_new_text(None, None), ("", False))

    def test_multiple_boundaries_cut_at_earliest(self):
        body = ("New text here -----Original Message----- blah "
                "On Mon Jun 1, 2026 a@b.com wrote: more")
        nt, found = extract_new_text(None, body)
        self.assertTrue(found)
        self.assertEqual(nt, "New text here")

    def test_sent_from_my_phone_boundary(self):
        nt, found = extract_new_text(None, "Sounds good. Sent from my iPhone")
        self.assertTrue(found)
        self.assertEqual(nt, "Sounds good.")

    def test_forwarded_header_with_address(self):
        body = "See below. From: jane@x.com Sent: today To: me Subject: Re: deal old"
        nt, found = extract_new_text(None, body)
        self.assertTrue(found)
        self.assertEqual(nt, "See below.")

    def test_no_over_truncation_on_inline_wrote_without_digits(self):
        # 'On ... wrote:' prose with NO date/time digits must NOT be treated as a boundary.
        body = "Following up on the proposal. On our last call your team wrote: we would review."
        nt, found = extract_new_text(None, body)
        self.assertFalse(found)
        self.assertEqual(nt, body)

    def test_no_over_truncation_on_inline_from_subject_without_address(self):
        body = "Please fill in the From: field and the Subject: field before sending. Thanks!"
        nt, found = extract_new_text(None, body)
        self.assertFalse(found)
        self.assertEqual(nt, body)

    def test_subject_echo_stripped(self):
        nt, found = extract_new_text("Quick question", "Re: Quick question Hi there, any update?")
        self.assertEqual(nt, "Hi there, any update?")

    def test_subject_echo_stripped_when_subject_already_has_re(self):
        # Stored subject already carries 'Re:' — must still strip the body echo.
        nt, found = extract_new_text("Re: Quick question", "Re: Quick question Hi there.")
        self.assertEqual(nt, "Hi there.")

    def test_subject_echo_stripped_with_whitespace_mismatch(self):
        # Multi-space subject vs single-space body echo.
        nt, found = extract_new_text("Quick   question", "Re: Quick question Hello.")
        self.assertEqual(nt, "Hello.")

    def test_subject_with_regex_special_chars(self):
        nt, found = extract_new_text("(special) [chars]?", "Re: (special) [chars]? Hi there.")
        self.assertEqual(nt, "Hi there.")

    def test_pure_quote_yields_no_usable_extraction(self):
        # Boundary at the very start leaves no new text -> not counted as a clean extraction.
        nt, found = extract_new_text(None, "On Mon, Jun 1, 2026 at 9am a@b.com wrote: everything here")
        self.assertEqual(nt, "")
        self.assertFalse(found)


class TestLengthBucket(unittest.TestCase):
    def test_boundaries(self):
        cases = [(0, "very_short"), (15, "very_short"), (16, "short"), (40, "short"),
                 (41, "medium"), (90, "medium"), (91, "long")]
        for words, expected in cases:
            with self.subTest(words=words):
                self.assertEqual(length_bucket(words), expected)


class TestDeterministicFeatures(unittest.TestCase):
    def test_empty_text(self):
        f = deterministic_features("", None)
        self.assertEqual(f["word_count"], 0)
        self.assertEqual(f["length_bucket"], "very_short")

    def test_none_timestamp(self):
        f = deterministic_features("hi", None)
        self.assertIsNone(f["send_dow"])
        self.assertIsNone(f["send_hour_utc"])

    def test_naive_datetime_assumed_utc(self):
        # 2026-06-15 is a Monday; naive datetime must be treated as UTC (not host-shifted).
        f = deterministic_features("hi", datetime(2026, 6, 15, 14, 0, 0))
        self.assertEqual(f["send_hour_utc"], 14)
        self.assertEqual(f["send_dow"], 0)

    def test_tzaware_converted_to_utc_consistently(self):
        # Mon 02:00 +05:00 == Sun 21:00 UTC: dow and hour must share the UTC zone.
        tz = timezone(__import__("datetime").timedelta(hours=5))
        f = deterministic_features("hi", datetime(2026, 6, 15, 2, 0, 0, tzinfo=tz))
        self.assertEqual(f["send_hour_utc"], 21)
        self.assertEqual(f["send_dow"], 6)  # Sunday

    def test_opens_with_question(self):
        self.assertTrue(deterministic_features("Hi Bob, are you free Tuesday? Thanks", None)["opens_with_question"])
        self.assertFalse(deterministic_features("Hi Bob. Are you free?", None)["opens_with_question"])
        self.assertTrue(deterministic_features("Are you free?", None)["opens_with_question"])

    def test_has_url_excludes_unsub(self):
        self.assertFalse(deterministic_features("opt out here https://example.com/unsubscribe?id=1", None)["has_url"])
        self.assertTrue(deterministic_features("grab a slot https://calendly.com/me", None)["has_url"])

    def test_has_calendar_link(self):
        self.assertTrue(deterministic_features("book a time: https://calendly.com/me", None)["has_calendar_link"])
        self.assertFalse(deterministic_features("here is our website", None)["has_calendar_link"])

    def test_has_ps_and_emoji_and_caps(self):
        self.assertTrue(deterministic_features("Thanks. P.S. one more thing", None)["has_ps"])
        self.assertFalse(deterministic_features("Thanks for your time", None)["has_ps"])
        self.assertTrue(deterministic_features("great news 🎉", None)["has_emoji"])
        self.assertEqual(deterministic_features("THIS IS URGENT now", None)["all_caps_word_count"], 2)


if __name__ == "__main__":
    unittest.main()
