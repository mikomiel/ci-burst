"""Tests for the builder console reader.

``read_marker`` decides whether a build may create its image, so every test
that expects ``ready`` has a twin that changes one thing and expects anything
but ``ready``. The module reads a string and nothing else: no fake of AWS or of
the state file is needed.

The sentinel stands in for a presigned archive URL. It is assembled at runtime,
so no secret-shaped literal sits in this file.
"""

import hashlib
import unittest

from ci_burst import image_console as console

DIGEST = hashlib.sha256(b"example staged archive").hexdigest()
OTHER = hashlib.sha256(b"another archive").hexdigest()

SENTINEL = "-".join(("not", "a", "real", "signature", "value"))
SENTINEL_URL = f"https://example-bucket.invalid/example-prefix/archive.tar.gz?token={SENTINEL}"

DIGEST_LINE = f"WARM_SOURCE_SHA256 {DIGEST}"
READY_LINE = "IMAGE_READY"


def text(*lines, eol="\n"):
    return eol.join(lines) + eol


def read(*lines, digest=DIGEST, eol="\n"):
    return console.read_marker(text(*lines, eol=eol), digest)


def outcome(*lines, **options):
    return read(*lines, **options)["outcome"]


class ReadyTests(unittest.TestCase):
    def test_the_digest_line_then_the_ready_line_is_ready(self):
        result = read("STEP clean", "CACHE size=1G", DIGEST_LINE, READY_LINE)
        self.assertEqual(result, {"outcome": "ready", "reason": None})

    def test_the_marker_needs_no_final_line_feed(self):
        self.assertEqual(console.read_marker(f"{DIGEST_LINE}\n{READY_LINE}", DIGEST)["outcome"], "ready")

    def test_crlf_line_ends_are_the_same_lines(self):
        self.assertEqual(outcome("STEP clean", DIGEST_LINE, READY_LINE, eol="\r\n"), "ready")

    def test_a_repeated_line_changes_nothing(self):
        # The script tees to the serial console, so each line usually appears twice.
        self.assertEqual(outcome(DIGEST_LINE, DIGEST_LINE, READY_LINE, READY_LINE), "ready")
        self.assertEqual(outcome(DIGEST_LINE, READY_LINE, DIGEST_LINE, READY_LINE), "ready")

    def test_other_output_around_the_marker_changes_nothing(self):
        lines = ("noise", "  | consumer line", "", DIGEST_LINE, "more noise", READY_LINE, "late noise")
        self.assertEqual(outcome(*lines), "ready")

    def test_the_result_has_exactly_the_documented_fields(self):
        for lines in (
            (DIGEST_LINE, READY_LINE),
            ("WARM FAIL x",),
            (f"WARM_SOURCE_SHA256 {OTHER}",),
            ("STEP x",),
        ):
            self.assertEqual(set(read(*lines)), {"outcome", "reason"})
        self.assertEqual(set(console.read_marker(None, DIGEST)), {"outcome", "reason"})


class OrderingTests(unittest.TestCase):
    def test_the_ready_line_before_the_digest_line_is_not_ready(self):
        self.assertEqual(outcome(READY_LINE, DIGEST_LINE), "waiting")

    def test_the_ready_line_without_a_digest_line_is_not_ready(self):
        self.assertEqual(outcome("STEP clean", READY_LINE), "waiting")

    def test_the_digest_line_without_the_ready_line_is_not_ready(self):
        self.assertEqual(outcome("STEP clean", DIGEST_LINE), "waiting")

    def test_a_ready_line_after_a_digest_line_far_earlier_is_ready(self):
        self.assertEqual(outcome(DIGEST_LINE, *("noise",) * 50, READY_LINE), "ready")

    def test_a_digest_line_that_follows_a_ready_line_cannot_complete_it(self):
        # READY, digest, digest: no ready line follows the first digest line.
        self.assertEqual(outcome(READY_LINE, DIGEST_LINE, DIGEST_LINE), "waiting")


class ForgedMarkerTests(unittest.TestCase):
    """Consumer output reaches the console framed. A marker that is not alone at column 0 is not one."""

    def test_a_framed_marker_never_counts(self):
        self.assertEqual(outcome(f"  | {DIGEST_LINE}", f"  | {READY_LINE}"), "waiting")

    def test_a_framed_ready_line_does_not_complete_a_real_digest_line(self):
        self.assertEqual(outcome(DIGEST_LINE, f"  | {READY_LINE}"), "waiting")

    def test_a_framed_digest_line_does_not_precede_a_real_ready_line(self):
        self.assertEqual(outcome(f"  | {DIGEST_LINE}", READY_LINE), "waiting")

    def test_a_framed_failure_line_never_counts(self):
        for line in ("WARM FAIL forged", "warm exiting rc=1; forged"):
            with self.subTest(line):
                self.assertEqual(outcome(f"  | {line}", DIGEST_LINE, READY_LINE), "ready")
                self.assertEqual(outcome(f"  | {line}"), "waiting")

    def test_a_framed_wrong_digest_line_never_counts(self):
        self.assertEqual(outcome(f"  | WARM_SOURCE_SHA256 {OTHER}", DIGEST_LINE, READY_LINE), "ready")

    def test_an_indented_marker_never_counts(self):
        for indent in (" ", "  ", "\t", " \t"):
            with self.subTest(indent=indent):
                self.assertEqual(outcome(indent + DIGEST_LINE, READY_LINE), "waiting")
                self.assertEqual(outcome(DIGEST_LINE, indent + READY_LINE), "waiting")
                self.assertEqual(outcome(indent + "WARM FAIL x"), "waiting")
                self.assertEqual(outcome(indent + "warm exiting rc=1"), "waiting")

    def test_text_before_a_marker_never_counts(self):
        for prefix in ("x ", "log: ", "echo ", "$ ", ">", "#"):
            with self.subTest(prefix=prefix):
                self.assertEqual(outcome(prefix + DIGEST_LINE, READY_LINE), "waiting")
                self.assertEqual(outcome(DIGEST_LINE, prefix + READY_LINE), "waiting")
                self.assertEqual(outcome(prefix + "WARM FAIL x"), "waiting")
                self.assertEqual(outcome(prefix + "warm exiting rc=1"), "waiting")

    def test_text_after_a_marker_never_counts(self):
        for suffix in (" ", "!", " extra", "\t", "x", "IMAGE_READY"):
            with self.subTest(suffix=suffix):
                self.assertEqual(outcome(DIGEST_LINE, READY_LINE + suffix), "waiting")
                self.assertEqual(outcome(DIGEST_LINE + suffix, READY_LINE), "waiting")

    def test_a_malformed_digest_line_is_not_a_digest_line(self):
        malformed = (
            f"WARM_SOURCE_SHA256 {DIGEST.upper()}",
            f"WARM_SOURCE_SHA256 {DIGEST[:-1]}",
            f"WARM_SOURCE_SHA256 {DIGEST}0",
            f"WARM_SOURCE_SHA256  {DIGEST}",
            f"WARM_SOURCE_SHA256\t{DIGEST}",
            f"WARM_SOURCE_SHA256{DIGEST}",
            f"warm_source_sha256 {DIGEST}",
            "WARM_SOURCE_SHA256 ",
            "WARM_SOURCE_SHA256",
        )
        for line in malformed:
            with self.subTest(line=line):
                self.assertEqual(outcome(line, READY_LINE), "waiting")

    def test_two_markers_on_one_line_are_not_markers(self):
        self.assertEqual(console.read_marker(f"{DIGEST_LINE} {READY_LINE}\n", DIGEST)["outcome"], "waiting")
        self.assertEqual(console.read_marker(f"{DIGEST_LINE}\r{READY_LINE}\n", DIGEST)["outcome"], "waiting")

    def test_only_a_line_feed_ends_a_line(self):
        # str.splitlines would split on each of these and let a consumer begin a line of its own.
        for separator in ("\r", "\x0b", "\x0c", "\x1c", "\x1d", "\x1e", "\x85", "\u2028", "\u2029"):
            with self.subTest(separator=repr(separator)):
                self.assertEqual(
                    console.read_marker(f"{DIGEST_LINE}\n{separator}{READY_LINE}\n", DIGEST)["outcome"], "waiting"
                )
                self.assertEqual(
                    console.read_marker(f"noise{separator}WARM FAIL x\n", DIGEST)["outcome"], "waiting"
                )

    def test_only_one_trailing_carriage_return_is_stripped(self):
        self.assertEqual(console.read_marker(f"{DIGEST_LINE}\n{READY_LINE}\r\r\n", DIGEST)["outcome"], "waiting")
        self.assertEqual(console.read_marker(f"{DIGEST_LINE}\r\r\n{READY_LINE}\n", DIGEST)["outcome"], "waiting")
        self.assertEqual(console.read_marker(f"{DIGEST_LINE}\r\n{READY_LINE}\r\n", DIGEST)["outcome"], "ready")

    def test_a_forged_marker_cannot_hide_a_real_failure(self):
        lines = (DIGEST_LINE, READY_LINE, "warm exiting rc=1; no image may be created from this builder")
        self.assertEqual(outcome(*lines), "failed")


class FailureTests(unittest.TestCase):
    def test_a_warm_fail_line_fails_the_build(self):
        result = read("STEP fetch", "WARM FAIL fetch rc=22")
        self.assertEqual(result, {"outcome": "failed", "reason": "WARM_FAILED"})

    def test_a_warm_exiting_line_fails_the_build(self):
        result = read("warm exiting rc=1; no image may be created from this builder")
        self.assertEqual(result, {"outcome": "failed", "reason": "WARM_FAILED"})

    def test_a_failure_line_beats_a_ready_marker_wherever_it_stands(self):
        failure = "WARM FAIL clean-up; last 60 log lines follow, framed"
        for lines in (
            (failure, DIGEST_LINE, READY_LINE),
            (DIGEST_LINE, failure, READY_LINE),
            (DIGEST_LINE, READY_LINE, failure),
        ):
            with self.subTest(lines=lines):
                self.assertEqual(outcome(*lines), "failed")

    def test_a_failure_line_beats_a_wrong_digest_line(self):
        # Both end the build with no image; the builder's own report is the more basic fact.
        self.assertEqual(outcome(f"WARM_SOURCE_SHA256 {OTHER}", "WARM FAIL x"), "failed")
        self.assertEqual(outcome("WARM FAIL x", f"WARM_SOURCE_SHA256 {OTHER}"), "failed")

    def test_the_prefix_must_match_exactly(self):
        for line in (
            "warm FAIL x",
            "Warm Fail x",
            "WARM FAILED x",
            "WARM FAILED",
            "WARM  FAIL x",
            "Warm exiting rc=1",
            "warm exiting rc",
            "warm  exiting rc=1",
            "warm exit rc=1",
        ):
            with self.subTest(line=line):
                self.assertEqual(outcome(line), "waiting")

    def test_a_failure_line_alone_is_a_failure_with_crlf_too(self):
        self.assertEqual(outcome("WARM FAIL x", eol="\r\n"), "failed")

    def test_a_bare_warm_fail_line_fails_the_build(self):
        failed = {"outcome": "failed", "reason": "WARM_FAILED"}
        for lines in (
            ("WARM FAIL",),
            ("STEP fetch", "WARM FAIL"),
            ("WARM FAIL", "STEP clean"),
            (DIGEST_LINE, READY_LINE, "WARM FAIL"),
            ("WARM FAIL", DIGEST_LINE, READY_LINE),
            (f"WARM_SOURCE_SHA256 {OTHER}", "WARM FAIL"),
        ):
            with self.subTest(lines=lines):
                self.assertEqual(read(*lines), failed)
        self.assertEqual(outcome("WARM FAIL", eol="\r\n"), "failed")
        self.assertEqual(console.read_marker("WARM FAIL", DIGEST), failed)  # no line end after it

    def test_a_console_cut_inside_the_failure_line_fails_once_the_marker_is_whole(self):
        # The console is a window of the builder's output, so it can end anywhere in the last line.
        consoled = text("STEP fetch", "WARM FAIL fetch rc=22")
        marker_start = consoled.index("WARM FAIL")
        marker_end = marker_start + len("WARM FAIL")
        for end in range(marker_start, marker_end):
            with self.subTest(end=end, shows=consoled[marker_start:end]):
                self.assertEqual(console.read_marker(consoled[:end], DIGEST)["outcome"], "waiting")
        for end in range(marker_end, len(consoled) + 1):
            with self.subTest(end=end, shows=consoled[marker_start:end]):
                self.assertEqual(console.read_marker(consoled[:end], DIGEST)["outcome"], "failed")

    def test_a_console_cut_at_64_kib_that_ends_in_the_bare_marker_fails_the_build(self):
        limit = 64 * 1024
        cut = ("STEP noise\n" * 7000 + "WARM FAIL")[-limit:]
        self.assertEqual(len(cut), limit)
        self.assertEqual(console.read_marker(cut, DIGEST), {"outcome": "failed", "reason": "WARM_FAILED"})
        # The same window without the marker shows noise only.
        self.assertEqual(console.read_marker(cut[: -len("WARM FAIL")], DIGEST)["outcome"], "waiting")

    def test_a_bare_warm_fail_line_counts_only_alone_at_column_0(self):
        for line in (
            "  | WARM FAIL",
            " WARM FAIL",
            "\tWARM FAIL",
            "x WARM FAIL",
            "# WARM FAIL",
            "WARM FAIL\t",
            "WARM  FAIL",
            "warm fail",
            "WARM FAILED",
        ):
            with self.subTest(line=line):
                self.assertEqual(outcome(line), "waiting")
                self.assertEqual(outcome(line, DIGEST_LINE, READY_LINE), "ready")
        # Only one trailing carriage return is part of the line end.
        self.assertEqual(console.read_marker("WARM FAIL\r\r\n", DIGEST)["outcome"], "waiting")
        # Another separator does not start a line of its own.
        self.assertEqual(console.read_marker("noise\x0bWARM FAIL", DIGEST)["outcome"], "waiting")


class MismatchTests(unittest.TestCase):
    def test_a_different_digest_is_a_mismatch_even_when_ready_follows(self):
        result = read(f"WARM_SOURCE_SHA256 {OTHER}", READY_LINE)
        self.assertEqual(result, {"outcome": "mismatch", "reason": "IMAGE_MARKER_DIGEST_MISMATCH"})

    def test_a_different_digest_alone_is_a_mismatch(self):
        self.assertEqual(outcome(f"WARM_SOURCE_SHA256 {OTHER}"), "mismatch")

    def test_a_different_digest_is_a_mismatch_wherever_it_stands(self):
        wrong = f"WARM_SOURCE_SHA256 {OTHER}"
        for lines in (
            (wrong, DIGEST_LINE, READY_LINE),
            (DIGEST_LINE, wrong, READY_LINE),
            (DIGEST_LINE, READY_LINE, wrong),
            (wrong, READY_LINE, DIGEST_LINE, READY_LINE),
        ):
            with self.subTest(lines=lines):
                self.assertEqual(outcome(*lines), "mismatch")

    def test_a_repeated_wrong_digest_line_is_still_a_mismatch(self):
        wrong = f"WARM_SOURCE_SHA256 {OTHER}"
        self.assertEqual(outcome(wrong, wrong, READY_LINE, READY_LINE), "mismatch")

    def test_the_expected_digest_decides_not_the_first_one_seen(self):
        # The same console is ready for the digest it shows and a mismatch for any other.
        lines = (DIGEST_LINE, READY_LINE)
        self.assertEqual(outcome(*lines), "ready")
        self.assertEqual(outcome(*lines, digest=OTHER), "mismatch")


class BlindAndWaitingTests(unittest.TestCase):
    def test_no_console_text_is_blind(self):
        for value in (None, "", "\n \n", " ", "\t", "\r\n", "\n\n\n", " \t\r\n \n"):
            with self.subTest(value=value):
                self.assertEqual(console.read_marker(value, DIGEST), {"outcome": "blind", "reason": None})

    def test_any_other_text_is_readable(self):
        for value in ("x", "STEP clean\n", "boot log\n", "\x00"):
            with self.subTest(value=value):
                self.assertEqual(console.read_marker(value, DIGEST)["outcome"], "waiting")

    def test_a_readable_console_without_a_verdict_is_waiting(self):
        result = read("Booting", "STEP fetch", "STEP unpack", "  | consumer output")
        self.assertEqual(result, {"outcome": "waiting", "reason": None})

    def test_a_very_long_line_is_read_in_one_pass(self):
        self.assertEqual(console.read_marker("x" * 1_000_000 + "\n" + DIGEST_LINE, DIGEST)["outcome"], "waiting")


class DeadlineReasonTests(unittest.TestCase):
    def test_a_blind_console_at_the_deadline_is_console_blind(self):
        self.assertEqual(console.deadline_reason(console.read_marker(None, DIGEST)["outcome"]), "CONSOLE_BLIND")
        self.assertEqual(console.deadline_reason("blind"), "CONSOLE_BLIND")

    def test_a_readable_console_at_the_deadline_is_a_marker_timeout(self):
        outcome_at_deadline = read("Booting", "STEP fetch")["outcome"]
        self.assertEqual(console.deadline_reason(outcome_at_deadline), "MARKER_TIMEOUT")
        self.assertEqual(console.deadline_reason("waiting"), "MARKER_TIMEOUT")

    def test_a_verdict_is_not_a_deadline_outcome(self):
        for value in ("ready", "failed", "mismatch", "", None, "unknown"):
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    console.deadline_reason(value)

    def test_the_reason_codes_are_the_documented_strings(self):
        self.assertEqual(console.WARM_FAILED, "WARM_FAILED")
        self.assertEqual(console.IMAGE_MARKER_DIGEST_MISMATCH, "IMAGE_MARKER_DIGEST_MISMATCH")
        self.assertEqual(console.CONSOLE_BLIND, "CONSOLE_BLIND")
        self.assertEqual(console.MARKER_TIMEOUT, "MARKER_TIMEOUT")


class InputTests(unittest.TestCase):
    def test_the_expected_digest_must_be_64_lowercase_hex_characters(self):
        for value in (None, "", DIGEST.upper(), DIGEST[:-1], DIGEST + "0", " " + DIGEST[1:], 7, b"a" * 64):
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    console.read_marker(text(DIGEST_LINE, READY_LINE), value)

    def test_a_bad_digest_is_refused_even_for_a_blind_console(self):
        with self.assertRaises(ValueError):
            console.read_marker(None, "not a digest")

    def test_console_text_that_is_not_text_is_a_caller_bug(self):
        for value in (b"IMAGE_READY\n", 7, ["IMAGE_READY"]):
            with self.subTest(value=value):
                with self.assertRaises(TypeError):
                    console.read_marker(value, DIGEST)
                with self.assertRaises(TypeError):
                    console.framed_tail(value)


class FramedTailTests(unittest.TestCase):
    def test_only_framed_lines_are_returned(self):
        consoled = text(
            "STEP fetch",
            f"curl {SENTINEL_URL}",
            "  | first consumer line",
            DIGEST_LINE,
            READY_LINE,
            "WARM FAIL step rc=1",
            "warm exiting rc=1; no image may be created from this builder",
            "  | second consumer line",
        )
        self.assertEqual(console.framed_tail(consoled), "  | first consumer line\n  | second consumer line")

    def test_a_url_in_an_unframed_line_never_appears(self):
        for line in (
            SENTINEL_URL,
            f"fetching {SENTINEL_URL}",
            f" | {SENTINEL_URL}",
            f"  |{SENTINEL_URL}",
            f"x  | {SENTINEL_URL}",
            f"{DIGEST_LINE} {SENTINEL_URL}",
            f"\t  | {SENTINEL_URL}",
        ):
            with self.subTest(line=line):
                tail = console.framed_tail(text("  | kept", line, "  | also kept"))
                self.assertEqual(tail, "  | kept\n  | also kept")
                self.assertNotIn(SENTINEL, tail)
                self.assertNotIn("https://", tail)

    def test_lookalike_frames_are_not_framed(self):
        for line in (" | x", "  |x", "   | x", "\t| x", "x  | y", "|  | x"):
            with self.subTest(line=line):
                self.assertEqual(console.framed_tail(text(line)), "")

    def test_a_line_with_only_the_frame_is_kept(self):
        self.assertEqual(console.framed_tail(text("  | ", "  | x")), "  | \n  | x")

    def test_every_byte_outside_printable_ascii_and_tab_becomes_a_question_mark(self):
        raw = "  | ok\ttab \x1b[31mred\x1b[0m nul\x00 del\x7f bell\x07 eé snow☃ mid\rcr"
        expected = "  | ok\ttab ?[31mred?[0m nul? del? bell? e?? snow??? mid?cr"
        self.assertEqual(console.framed_tail(text(raw)), expected)

    def test_the_result_is_plain_ascii_whatever_the_input(self):
        wild = "".join(chr(code) for code in range(0, 600)).replace("\n", "")
        tail = console.framed_tail(text("  | " + wild), max_lines=5, max_bytes=100_000)
        self.assertTrue(tail.isascii())
        self.assertTrue(all(ch == "\t" or " " <= ch <= "~" for ch in tail), tail)

    def test_a_lone_surrogate_becomes_a_question_mark(self):
        self.assertEqual(console.framed_tail(text("  | a\ud800b")), "  | a?b")

    def test_crlf_framed_lines_lose_their_line_end(self):
        self.assertEqual(console.framed_tail("  | one\r\n  | two\r\n"), "  | one\n  | two")

    def test_only_the_last_lines_are_kept(self):
        consoled = text(*(f"  | line {number}" for number in range(10)))
        self.assertEqual(console.framed_tail(consoled, max_lines=3), "  | line 7\n  | line 8\n  | line 9")
        self.assertEqual(console.framed_tail(consoled, max_lines=1), "  | line 9")
        self.assertEqual(len(console.framed_tail(consoled, max_lines=100).split("\n")), 10)

    def test_whole_lines_leave_from_the_front_to_fit_the_byte_bound(self):
        consoled = text("  | aaaa", "  | bbbb", "  | cccc")  # three lines of 8 bytes, 26 with the line feeds
        self.assertEqual(console.framed_tail(consoled, max_bytes=26), "  | aaaa\n  | bbbb\n  | cccc")
        self.assertEqual(console.framed_tail(consoled, max_bytes=25), "  | bbbb\n  | cccc")
        self.assertEqual(console.framed_tail(consoled, max_bytes=17), "  | bbbb\n  | cccc")
        self.assertEqual(console.framed_tail(consoled, max_bytes=16), "  | cccc")

    def test_a_single_line_that_is_too_long_is_cut_short_and_keeps_its_frame(self):
        tail = console.framed_tail(text("  | " + "x" * 100), max_bytes=20)
        self.assertEqual(tail, "  | " + "x" * 16)
        self.assertEqual(len(tail), 20)

    def test_the_result_never_exceeds_either_bound(self):
        consoled = text(*(f"  | line {number} " + "y" * number for number in range(30)))
        for max_lines in (1, 2, 5, 40):
            for max_bytes in (1, 2, 4, 5, 9, 20, 64, 200, 4096):
                with self.subTest(max_lines=max_lines, max_bytes=max_bytes):
                    tail = console.framed_tail(consoled, max_lines=max_lines, max_bytes=max_bytes)
                    self.assertLessEqual(len(tail.encode("ascii")), max_bytes)
                    self.assertLessEqual(len(tail.split("\n")), max_lines)

    def test_every_returned_line_is_framed_whatever_the_bounds(self):
        # A cut that falls inside the frame would leave a fragment, and a fragment is not a framed line.
        consoled = text(
            *(f"  | line {number} " + "y" * number for number in range(30)), "unframed", " | lookalike", "  |"
        )
        for max_lines in (1, 2, 5, 40):
            for max_bytes in range(1, 90):
                with self.subTest(max_lines=max_lines, max_bytes=max_bytes):
                    tail = console.framed_tail(consoled, max_lines=max_lines, max_bytes=max_bytes)
                    for line in tail.split("\n") if tail else ():
                        self.assertTrue(line.startswith(console.FRAME), repr(line))

    def test_a_bound_smaller_than_the_frame_returns_nothing_instead_of_a_fragment(self):
        for consoled in (text("  | x"), text("  | " + "x" * 50), text("  | one", "  | two")):
            for max_bytes in range(1, len(console.FRAME)):
                with self.subTest(consoled=consoled, max_bytes=max_bytes):
                    self.assertEqual(console.framed_tail(consoled, max_bytes=max_bytes), "")

    def test_a_bound_of_exactly_the_frame_keeps_the_bare_frame(self):
        tail = console.framed_tail(text("  | " + "x" * 50), max_bytes=len(console.FRAME))
        self.assertEqual(tail, console.FRAME)

    def test_the_defaults_bound_a_flood(self):
        flood = text(*("  | " + "z" * 300 for _ in range(500)))
        tail = console.framed_tail(flood)
        self.assertLessEqual(len(tail), console.DEFAULT_TAIL_BYTES)
        self.assertLessEqual(len(tail.split("\n")), console.DEFAULT_TAIL_LINES)

    def test_repeated_lines_are_not_collapsed(self):
        self.assertEqual(console.framed_tail(text("  | same", "  | same")), "  | same\n  | same")

    def test_no_framed_line_gives_an_empty_string(self):
        for value in (None, "", "\n", "STEP x\nIMAGE_READY\n"):
            with self.subTest(value=value):
                self.assertEqual(console.framed_tail(value), "")

    def test_a_bound_without_room_is_refused(self):
        for options in ({"max_lines": 0}, {"max_bytes": 0}, {"max_lines": -1}, {"max_bytes": -5}):
            with self.subTest(options=options):
                with self.assertRaises(ValueError):
                    console.framed_tail(text("  | x"), **options)


if __name__ == "__main__":
    unittest.main()
