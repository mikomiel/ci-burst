"""What the builder's console says about a warm build, and nothing more.

The warm script prints its verdict at column 0: ``WARM_SOURCE_SHA256 <digest>``
and then ``IMAGE_READY`` on success, ``WARM FAIL ...`` and ``warm exiting rc=...``
on failure. Everything a consumer command prints reaches the console only as
lines framed with ``"  | "`` (the script sanitizes and frames them), so a
marker can be trusted only where it stands alone at column 0.

``read_marker`` is deliberately stricter than the worker console parser in
``export``. It splits on a line feed and nothing else (``str.splitlines`` also
splits on a lone carriage return, form feed, NEL and the Unicode separators, and
a consumer could use any of them to start a line of its own), strips one
trailing carriage return, and accepts only a whole line that equals a marker (a
failure marker may carry detail after it). A framed, indented, prefixed or
mid-line marker never counts. The script tees its output to the serial console,
so a line usually appears twice; a repeated line changes nothing.

The console keeps only its last 64 KiB and a read can end inside the last line,
so a failure line can arrive without its detail. ``WARM FAIL`` alone on a line is
therefore a failure marker, like ``WARM FAIL`` followed by a space and detail.

Nothing here logs, and nothing here reads or writes state: the caller passes in
the console text and the staged archive digest and acts on the outcome. No
console text leaves this module except through ``framed_tail``, and only as
bounded, sanitized, framed lines.
"""

import re

READY = "ready"
FAILED = "failed"
MISMATCH = "mismatch"
WAITING = "waiting"
BLIND = "blind"

#: The stable reason codes of this module. ``IMAGE_MARKER_DIGEST_MISMATCH`` and
#: ``WARM_FAILED`` come with an outcome from ``read_marker``; the other two come
#: from ``deadline_reason`` and only once the build deadline has passed.
WARM_FAILED = "WARM_FAILED"
IMAGE_MARKER_DIGEST_MISMATCH = "IMAGE_MARKER_DIGEST_MISMATCH"
CONSOLE_BLIND = "CONSOLE_BLIND"
MARKER_TIMEOUT = "MARKER_TIMEOUT"

#: The prefix with which ``warm-cache.sh`` frames every line of consumer output.
FRAME = "  | "

#: The defaults of ``framed_tail``. The script itself prints 60 framed lines on a
#: failure, and the console repeats each, so these keep an event file small and
#: still hold the end of a failure.
DEFAULT_TAIL_LINES = 40
DEFAULT_TAIL_BYTES = 4096

_DIGEST = re.compile(r"[0-9a-f]{64}")
_DIGEST_LINE = re.compile(r"WARM_SOURCE_SHA256 ([0-9a-f]{64})")
_READY_LINE = "IMAGE_READY"
_FAILURE_LINE = "WARM FAIL"
_FAILURE_PREFIXES = (_FAILURE_LINE + " ", "warm exiting rc=")

_REASONS = {FAILED: WARM_FAILED, MISMATCH: IMAGE_MARKER_DIGEST_MISMATCH}


def _text(console_text):
    """The console as text. ``None`` is no console. Any other type is a caller bug."""
    if console_text is None:
        return ""
    if not isinstance(console_text, str):
        raise TypeError("console text must be a string or None")
    return console_text


def _lines(text):
    """Split on a line feed only and strip at most one carriage return from each line."""
    for line in text.split("\n"):
        yield line[:-1] if line.endswith("\r") else line


def _is_failure(line):
    """Whether the line is ``WARM FAIL`` alone, or starts with ``WARM FAIL `` or ``warm exiting rc=``."""
    return line == _FAILURE_LINE or line.startswith(_FAILURE_PREFIXES)


def read_marker(console_text, expected_archive_sha256):
    """Read the console and say what the build has shown so far.

    ``expected_archive_sha256`` is the hex sha256 of the staged archive (64
    lowercase hex characters, else ``ValueError``). The result is
    ``{"outcome", "reason"}``. The outcome is one of:

    * ``blind``: no console text at all (``None``, empty, or only whitespace).
    * ``failed`` (reason ``WARM_FAILED``): a column-0 line is ``WARM FAIL``
      alone, or starts with ``WARM FAIL `` or ``warm exiting rc=``, anywhere in
      the console.
    * ``mismatch`` (reason ``IMAGE_MARKER_DIGEST_MISMATCH``): a column-0
      ``WARM_SOURCE_SHA256`` line carries a digest other than the expected one,
      wherever it stands and even when a matching line stands beside it.
    * ``ready``: a matching digest line and, after it, the whole line
      ``IMAGE_READY``.
    * ``waiting``: readable text without any of the above.

    When several apply, ``failed`` wins over ``mismatch`` and ``mismatch`` over
    ``ready``. The builder reporting its own failure is the more basic fact, and
    in every case no image may be created, so the order only chooses the code.
    ``IMAGE_READY`` without a digest line before it is not ready, so a console
    that shows only ``IMAGE_READY`` stays ``waiting``. ``reason`` is ``None``
    for the other outcomes.
    """
    if not isinstance(expected_archive_sha256, str) or not _DIGEST.fullmatch(expected_archive_sha256):
        raise ValueError("the expected archive digest must be 64 lowercase hex characters")
    text = _text(console_text)
    if not text.strip():
        return _result(BLIND)
    failed = wrong_digest = digest_seen = ready = False
    for line in _lines(text):
        if _is_failure(line):
            failed = True
            continue
        found = _DIGEST_LINE.fullmatch(line)
        if found is not None:
            if found.group(1) == expected_archive_sha256:
                digest_seen = True
            else:
                wrong_digest = True
        elif line == _READY_LINE and digest_seen:
            ready = True
    if failed:
        return _result(FAILED)
    if wrong_digest:
        return _result(MISMATCH)
    return _result(READY if ready else WAITING)


def _result(outcome):
    return {"outcome": outcome, "reason": _REASONS.get(outcome)}


def deadline_reason(outcome):
    """The reason code of a build whose deadline passed without a verdict.

    ``blind`` gives ``CONSOLE_BLIND`` and ``waiting`` gives ``MARKER_TIMEOUT``.
    Any other outcome is a verdict of its own and ``ValueError``.
    """
    if outcome == BLIND:
        return CONSOLE_BLIND
    if outcome == WAITING:
        return MARKER_TIMEOUT
    raise ValueError("only a blind or waiting console ends at the deadline")


def _printable(line):
    """The line with every byte outside tab and printable ASCII replaced by ``?``."""
    raw = line.encode("utf-8", "replace")
    return bytes(byte if byte == 9 or 32 <= byte <= 126 else 63 for byte in raw).decode("ascii")


def framed_tail(console_text, max_lines=DEFAULT_TAIL_LINES, max_bytes=DEFAULT_TAIL_BYTES):
    """The end of the consumer output the console shows, bounded and sanitized.

    Only lines that start with the frame (after one trailing carriage return is
    stripped) are considered; every other line, however it looks, is dropped.
    Every byte of a kept line outside tab and printable ASCII becomes ``?``, so
    the result is plain ASCII. The result is at most ``max_lines`` lines and
    ``max_bytes`` bytes, counting a line feed between lines. It keeps the end:
    whole lines leave from the front first, and a single line that is still too
    long is cut short (its start stays, so its frame stays). The lines are
    joined with a line feed; the result is ``""`` when no framed line exists.

    Every returned line is framed. A bound smaller than the frame leaves no room
    for even the start of a line, and the result is then ``""`` instead of a
    fragment of the frame.

    A framed line is consumer output. A consumer that prints a secret it can see
    puts it into this tail, so the tail goes only into the failed-build event
    that the design allows it in.
    """
    if max_lines < 1 or max_bytes < 1:
        raise ValueError("a tail needs room for at least one line and one byte")
    framed = [_printable(line) for line in _lines(_text(console_text)) if line.startswith(FRAME)]
    kept = framed[-max_lines:]
    while len(kept) > 1 and sum(len(line) for line in kept) + len(kept) - 1 > max_bytes:
        kept.pop(0)
    tail = "\n".join(kept)[:max_bytes]
    # Whole lines fit by the loop above, so only the cut of a single line can end
    # inside the frame, and what is left then is not a framed line.
    return tail if tail.startswith(FRAME) else ""
