"""Promotion: the one edit that moves the pin, and its way back.

``promote`` is pure local. It reads only local files: the image state file, the
deployment TOML and the backup that a journal row names. It writes only the TOML
(its backup and one temporary file), the state file, one lock file next to the
TOML and, once a write is done, one line of the event file in the state
directory. It makes no AWS or forge call, builds no client and holds no
credential: the module imports neither adapter, and a test pins that. Its
verification comes from the record that a pass or ``image status --refresh``
left in the state file.

Without ``--write`` ``promote`` only reads and reports. With ``--write`` it
follows the design's eight steps, in this order:

1. nothing is written until the checks that need only the TOML and the
   directory pass (``PROMOTE_ZONE_PIN``, ``PROMOTE_DIRECTORY_NOT_DEDICATED``);
2. the lock file of the TOML, then the writer lock of the state file (always in
   this order, and never waited for);
3. a pending journal row is resolved from the file hash;
4. the candidate (or the rollback target) is verified against the record;
5. the one ``launch_template_version`` assignment of ``[aws]`` is replaced in
   memory, and the new text must parse again through the strict loader to a
   result that differs from the old one in that key only;
6. a temporary file and a timestamped backup are written, and the target is read
   again: it must still have the hash of the bytes that were read;
7. the ``pending`` journal row is committed, then the temporary file replaces the
   target and the directory is flushed;
8. the row is marked ``complete``.

Then, and only then, one ``image_promoted`` event is appended (``promote`` says
what it holds and what a failure of that write does). The event is a notice and
the journal row is the record: a crash after the rename loses the event and
never the promotion.

A crash between two of these steps leaves a state that the next write form
(``--write``, ``--rollback --write``) or ``image run --apply`` resolves from the
file hash (``resolve_pending``). Without ``--write`` ``image status`` only
reports the resolution that the hash implies, and ``promote`` also plans as the
write would after it, against a copy of the state in memory. Nothing here
deletes a backup that a journal row names.
"""

import errno
import fcntl
import hashlib
import os
import re
import stat
import tomllib
from contextlib import contextmanager, suppress
import dataclasses
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from . import config as configuration
from .errors import ConfigRefusal, ImageRefusal, Refusal, StateRefusal
from .image_template import CLEAN

PROMOTE_NO_CANDIDATE = "PROMOTE_NO_CANDIDATE"
PROMOTE_VERSION_NOT_CANDIDATE = "PROMOTE_VERSION_NOT_CANDIDATE"
PROMOTE_IMAGE_NOT_AVAILABLE = "PROMOTE_IMAGE_NOT_AVAILABLE"
PROMOTE_VERSION_NOT_RECORDED = "PROMOTE_VERSION_NOT_RECORDED"
PROMOTE_DRIFT_NOT_CLEAN = "PROMOTE_DRIFT_NOT_CLEAN"
PROMOTE_GATE_NOT_PASSED = "PROMOTE_GATE_NOT_PASSED"
IMAGE_VERIFICATION_STALE = "IMAGE_VERIFICATION_STALE"
PIN_MOVED = "PIN_MOVED"
PROMOTE_ZONE_PIN = "PROMOTE_ZONE_PIN"
PROMOTE_PIN_LINE_AMBIGUOUS = "PROMOTE_PIN_LINE_AMBIGUOUS"
PROMOTE_REPARSE_FAILED = "PROMOTE_REPARSE_FAILED"
PROMOTE_FILE_CHANGED = "PROMOTE_FILE_CHANGED"
PROMOTE_FILE_STALE = "PROMOTE_FILE_STALE"
PROMOTE_STATE_CHANGED = "PROMOTE_STATE_CHANGED"
PROMOTE_DIRECTORY_NOT_DEDICATED = "PROMOTE_DIRECTORY_NOT_DEDICATED"
PROMOTE_BUSY = "PROMOTE_BUSY"
PROMOTE_WRITE_FAILED = "PROMOTE_WRITE_FAILED"
PROMOTION_JOURNAL_DIVERGED = "PROMOTION_JOURNAL_DIVERGED"
PIN_RECORD_DIVERGED = "PIN_RECORD_DIVERGED"
ROLLBACK_NO_PROMOTION = "ROLLBACK_NO_PROMOTION"
ROLLBACK_PREVIOUS_NOT_RECORDED = "ROLLBACK_PREVIOUS_NOT_RECORDED"
ROLLBACK_PREVIOUS_MISSING = "ROLLBACK_PREVIOUS_MISSING"

#: The recorded state of a usable image. ``image status --refresh`` records
#: ``MISSING_IMAGE`` when the provider no longer knows the image.
AVAILABLE = "available"
MISSING_IMAGE = "missing"

_AWS_HEADER = re.compile(r"[ \t]*\[[ \t]*aws[ \t]*\][ \t]*(?:#[^\n]*)?\r?")
_ANY_HEADER = re.compile(r"[ \t]*\[")
_PIN_LINE = re.compile(r"([ \t]*launch_template_version[ \t]*=[ \t]*)([0-9]+)([ \t]*(?:#[^\n]*)?\r?)")
_TEMPORARY_SUFFIX = ".promote-tmp"
_LOCK_SUFFIX = ".lock"
_BACKUP_INFIX = ".bak."
_BACKUP_STAMP = "%Y%m%dT%H%M%SZ"
_FOREIGN_NAMES_SHOWN = 5


@dataclass(frozen=True)
class Snapshot:
    """The deployment file as it was read once: its bytes, hash, parse and metadata."""

    target: Path
    data: bytes
    sha256: str
    text: str
    raw: dict
    config: object
    mode: int
    uid: int
    gid: int
    identity: tuple  # what tells the file the bytes were read from from any other: see ``_identity``
    held: int | None = None  # with ``hold``, the descriptor the bytes were read through, still open

    @property
    def pin(self):
        return self.config.aws.launch_template_version


# -- reading --------------------------------------------------------------------------


def read_snapshot(config, *, hold=False):
    """Read the deployment file once, through the strict loader, and keep its bytes.

    The path is followed to the file it names, so the directory that is checked,
    locked and written is the directory the bytes live in. Every pin fact of one
    command comes from these bytes.

    With ``hold`` the descriptor stays open in ``held`` and the caller closes it: while it is open the file's inode
    cannot be given to another file, so a later check of the path tells this file from any replacement.
    """
    target = Path(config.path).resolve()
    try:
        descriptor = os.open(target, os.O_RDONLY | os.O_CLOEXEC)
    except FileNotFoundError:
        raise ConfigRefusal("CONFIG_MISSING", str(config.path)) from None
    except OSError as error:
        raise ConfigRefusal("CONFIG_UNREADABLE", f"{config.path}: {error}") from None
    try:
        snapshot = _read_open_file(config, target, descriptor)
    except BaseException:
        os.close(descriptor)
        raise
    if hold:
        return dataclasses.replace(snapshot, held=descriptor)
    os.close(descriptor)
    return snapshot


def _read_open_file(config, target, descriptor):
    """The snapshot of the file open at ``descriptor``: its metadata and its bytes come from that one open file."""
    try:
        metadata = os.fstat(descriptor)
    except OSError as error:
        raise ConfigRefusal("CONFIG_UNREADABLE", f"{config.path}: {error}") from None
    try:
        chunks = []
        while chunk := os.read(descriptor, 1 << 16):
            chunks.append(chunk)
        linked = os.fstat(descriptor).st_nlink > 0
        data = b"".join(chunks)
        text = data.decode("utf-8")
    except (OSError, UnicodeDecodeError) as error:
        raise ConfigRefusal("CONFIG_UNREADABLE", f"{config.path}: {error}") from None
    if not linked:
        # A bind mount of the file itself keeps the inode that was mounted. Once a write elsewhere replaced the file
        # by a rename, that inode has no name left, and every fact read from it is old: a hash that resolves a
        # pending row the wrong way, a pin that is no longer live. The check runs after the read, on the descriptor
        # that supplied the bytes, so a replacement during the read is seen too.
        raise ImageRefusal(
            PROMOTE_FILE_STALE,
            "this view of the deployment file was replaced; mount its directory, not the file, and run again",
        )
    try:
        raw = tomllib.loads(text)
    except tomllib.TOMLDecodeError as error:
        raise ConfigRefusal("CONFIG_INVALID", f"TOML: {error}") from None
    return Snapshot(
        target=target,
        data=data,
        sha256=hashlib.sha256(data).hexdigest(),
        text=text,
        raw=raw,
        config=configuration.loads(text, config.path),
        mode=stat.S_IMODE(metadata.st_mode),
        uid=metadata.st_uid,
        gid=metadata.st_gid,
        identity=_identity(metadata),
    )


def _identity(metadata):
    """The device and inode, with the change and modification times and the size.

    A file that replaced another can reuse its inode, but not its change time, which no write of the content or
    of the times can set back: the pair tells a file that was replaced, even by the same bytes, from one that was not.
    """
    return (metadata.st_dev, metadata.st_ino, metadata.st_ctime_ns, metadata.st_mtime_ns, metadata.st_size)


def _is_unchanged(snapshot):
    """Whether the deployment path still leads to the same file with the same bytes. It parses nothing.

    The path is compared before anything is opened, the open neither follows a link nor waits for a writer, and the
    identity is compared before the bytes are read: a replacement by a FIFO, a link or another file is a change, and
    never a hang. Any error is a change.
    """
    try:
        target = Path(snapshot.config.path).resolve()
        if target != snapshot.target:
            return False
        descriptor = os.open(target, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC)
        try:
            metadata = os.fstat(descriptor)
            if not stat.S_ISREG(metadata.st_mode) or _identity(metadata) != snapshot.identity:
                return False
            if snapshot.held is not None and _identity(os.fstat(snapshot.held)) != _identity(metadata):
                return False  # the held file is no longer the one the path names, or it changed
            chunks = []
            while chunk := os.read(descriptor, 1 << 16):
                chunks.append(chunk)
        finally:
            os.close(descriptor)
    except (OSError, RuntimeError):  # ``resolve`` raises ``RuntimeError`` for a loop of links
        return False
    return sha256_of(b"".join(chunks)) == snapshot.sha256


def sha256_of(data):
    return hashlib.sha256(data).hexdigest()


# -- checks that need only the file and its directory --------------------------------


def check_zone_pins(raw):
    """Refuse when a zone entry carries its own ``launch_template_version``.

    The single-key edit moves the ``[aws]`` pin and would leave that zone on
    its own version, so the operator edits by hand.
    """
    zones = raw.get("aws", {}).get("availability_zones", [])
    pinned = [index for index, zone in enumerate(zones) if isinstance(zone, dict) and "launch_template_version" in zone]
    if pinned:
        raise ImageRefusal(
            PROMOTE_ZONE_PIN,
            "[[aws.availability_zones]] entries "
            + ", ".join(str(index) for index in pinned)
            + " carry their own launch_template_version: edit the file by hand",
        )


def check_directory(target):
    """Refuse unless the directory holds only the file, its backups and its lock.

    The file's own temporary file counts as well, because a crashed promotion
    can leave one. A subdirectory or a symbolic link never counts. The check
    reads the directory and writes nothing.
    """
    name = target.name
    try:
        entries = list(os.scandir(target.parent))
    except OSError:
        raise ImageRefusal(
            PROMOTE_DIRECTORY_NOT_DEDICATED, "the directory of the deployment file cannot be listed"
        ) from None
    foreign = []
    for entry in entries:
        own = (
            entry.name == name
            or entry.name.startswith(name + _BACKUP_INFIX)
            or entry.name in (name + _LOCK_SUFFIX, name + _TEMPORARY_SUFFIX)
        )
        if not own or entry.is_symlink() or entry.is_dir(follow_symlinks=False):
            foreign.append(entry.name)
    if foreign:
        shown = ", ".join(sorted(foreign)[:_FOREIGN_NAMES_SHOWN])
        more = f" and {len(foreign) - _FOREIGN_NAMES_SHOWN} more" if len(foreign) > _FOREIGN_NAMES_SHOWN else ""
        raise ImageRefusal(
            PROMOTE_DIRECTORY_NOT_DEDICATED,
            f"the directory holds more than the file, its backups and its lock: {shown}{more}",
        )


@contextmanager
def file_lock(target):
    """Hold the lock file of the deployment file, or refuse without waiting."""
    path = target.parent / (target.name + _LOCK_SUFFIX)
    try:
        descriptor = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
    except OSError as error:
        raise ImageRefusal(PROMOTE_WRITE_FAILED, f"cannot open the lock file: {_errno_name(error)}") from None
    try:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            raise ImageRefusal(PROMOTE_BUSY, "another promotion holds the lock file") from None
        try:
            yield
        finally:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
    finally:
        os.close(descriptor)


# -- the journal ---------------------------------------------------------------------


def implied_resolution(row, file_sha256):
    """What the hash of the file says about a pending row: ``aborted``, ``complete`` or ``diverged``."""
    if file_sha256 == row.old_file_sha256:
        return "aborted"
    if file_sha256 == row.new_file_sha256:
        return "complete"
    return "diverged"


def pending_report(state, file_sha256, *, readable_only=False):
    """Every pending row with the resolution that the file hash implies. Reads only.

    With ``readable_only`` a damaged row anywhere in the journal does not refuse: the report then lists the
    well-formed pending rows, for a preview whose whole-journal check refuses next.
    """
    rows = state.pending_journal_readable() if readable_only else state.pending_journal()
    return [
        {
            "journal_id": row.id,
            "kind": row.kind,
            "old_pin": row.old_pin,
            "new_pin": row.new_pin,
            "implied_resolution": implied_resolution(row, file_sha256),
        }
        for row in rows
    ]


def resolve_pending(state, file_sha256):
    """Resolve every pending row from the hash of the file, or refuse and write nothing.

    The old hash means the rename did not happen: the row is ``aborted``. The new
    hash means it did: the row is ``complete``, and a promotion marks its
    candidate ``promoted``. Any other hash refuses with ``PROMOTION_JOURNAL_DIVERGED``
    and leaves every row pending, so an operator resolves it by hand. The
    caller holds the writer lock. The result lists what was resolved.
    """
    rows = state.pending_journal()
    if not rows:
        return []
    verdicts = [(row, implied_resolution(row, file_sha256)) for row in rows]
    diverged = [row.id for row, verdict in verdicts if verdict == "diverged"]
    if diverged:
        raise StateRefusal(
            PROMOTION_JOURNAL_DIVERGED,
            "the file matches neither hash of pending journal row "
            + ", ".join(str(identifier) for identifier in diverged)
            + ": restore the file from its backup, then run a resolving command again",
        )
    resolved = []
    with state.transaction():
        for row, verdict in verdicts:
            if verdict == "complete":
                state.journal_complete(row.id)
                _mark_promoted(state, row)
            else:
                state.journal_abort(row.id)
            resolved.append({"journal_id": row.id, "kind": row.kind, "resolution": verdict})
    return resolved


def _mark_promoted(state, row):
    """A completed promotion ends its candidate as ``promoted``. A rollback changes no build."""
    if row.kind != "promotion" or row.candidate_id is None:
        return
    build = state.build(row.candidate_id)
    if build is not None and build.state == "candidate":
        state.advance_build(build.id, state="promoted")


# -- selecting and verifying ---------------------------------------------------------


def select_candidate(state, deployment, version=None):
    """The newest verified candidate, or the candidate that owns ``version``.

    A version that no candidate row owns is refused: that is a pin an operator set
    by hand, and nothing checked its drift.
    """
    if version is None:
        build = state.verified_candidate(deployment)
        if build is None:
            raise ImageRefusal(PROMOTE_NO_CANDIDATE, "no verified candidate is recorded")
        return build
    owned = [build for build in state.builds(deployment) if build.template_version == version]
    candidates = [build for build in owned if build.state == "candidate"]
    if not candidates:
        shown = ", ".join(sorted({build.state for build in owned})) or "no record"
        raise ImageRefusal(
            PROMOTE_VERSION_NOT_CANDIDATE, f"no candidate record owns launch-template version {version} ({shown})"
        )
    return candidates[-1]


def verify_candidate(build, image_config, now, toml_pin):
    """Refuse unless the record shows every item that a promotion needs.

    The order is the design's: the image, the version, the drift, the gate, the
    age of the record, then the pin. A refusal names the item and the recorded
    value, never a secret.
    """
    if build.image_state != AVAILABLE:
        raise ImageRefusal(PROMOTE_IMAGE_NOT_AVAILABLE, f"the record shows image state {build.image_state or 'none'}")
    if build.template_version_exists != 1 or build.template_version is None:
        raise ImageRefusal(
            PROMOTE_VERSION_NOT_RECORDED,
            f"the record does not show launch-template version {build.template_version} as existing",
        )
    if build.drift_result != CLEAN:
        raise ImageRefusal(PROMOTE_DRIFT_NOT_CLEAN, f"the record shows drift result {build.drift_result or 'none'}")
    passed = build.gate_configured == 1 and build.gate_passed == 1
    allowed_without = image_config.warm.allow_no_gate and build.gate_configured == 0
    if not (passed or allowed_without):
        raise ImageRefusal(
            PROMOTE_GATE_NOT_PASSED,
            f"the record shows gate configured {build.gate_configured} and passed {build.gate_passed}",
        )
    check_fresh(build.verified_at, now, image_config.promote_max_age_seconds)
    if build.source_pin != toml_pin:
        raise ImageRefusal(
            PIN_MOVED,
            f"the file pins version {toml_pin}; the record was derived from version {build.source_pin}",
        )


def check_fresh(verified_at, now, limit):
    """Refuse a record that was verified too long ago, never, or in the future."""
    if verified_at is None:
        raise ImageRefusal(IMAGE_VERIFICATION_STALE, "the record has no verification time: run image status --refresh")
    age = now - verified_at
    if age < 0:
        raise ImageRefusal(
            IMAGE_VERIFICATION_STALE, "the record was verified in the future: run image status --refresh"
        )
    if age > limit:
        raise ImageRefusal(
            IMAGE_VERIFICATION_STALE,
            f"verified {int(age)} seconds ago, and the limit is {limit}: run image status --refresh",
        )


def evidence(build, now):
    """The evidence block of a candidate. Digests are shortened; nothing here is a secret."""
    duration = None
    if build.state == "candidate" and build.verified_at is not None:
        # Nothing writes a candidate row after the build ends (a refresh leaves
        # updated_at alone), so its updated_at is the end of the build.
        duration = max(0.0, build.updated_at - build.created_at)
    return {
        "build_id": build.id,
        "source_sha": _short(build.source_sha),
        "basis_digest": _short(build.basis_digest),
        "script_sha256": build.script_sha256,
        "gate": {"configured": _flag(build.gate_configured), "passed": _flag(build.gate_passed)},
        "build_duration_seconds": duration,
        "verified_at": build.verified_at,
        "verified_age_seconds": None if build.verified_at is None else int(now - build.verified_at),
        "image_id": build.image_id,
        "image_state": build.image_state,
        "template_version": build.template_version,
        "template_version_exists": _flag(build.template_version_exists),
        "drift_result": build.drift_result,
        "source_pin": build.source_pin,
    }


def _short(value):
    return value[:12] if value else None


def _flag(value):
    return None if value is None else bool(value)


# -- the edit ------------------------------------------------------------------------


def plan_edit(text, new_pin):
    """Replace the one ``launch_template_version`` assignment of ``[aws]`` and nothing else.

    Returns the new text and the change (the line number and the assignment
    before and after). The change shows the key, the indentation and the value
    only: a trailing comment stays in the file but is not reported, because it can
    hold text that should not reach logs or tickets. The text is split at line
    feeds only, so carriage returns and every other byte stay as they are, and
    only the digits of the value are replaced.
    An assignment in a sub-table, a quoted or dotted key, an inline table or
    anything that is not a plain decimal value is not found, and the edit
    refuses: it is better to refuse than to guess. A scanner can be fooled by a
    multi-line string, so the caller re-parses the result (``check_reparse``).
    """
    lines = text.split("\n")
    in_aws, found = False, []
    for number, line in enumerate(lines):
        if _AWS_HEADER.fullmatch(line):
            in_aws = True
        elif _ANY_HEADER.match(line):
            in_aws = False
        elif in_aws:
            match = _PIN_LINE.fullmatch(line)
            if match:
                found.append((number, match))
    if len(found) != 1:
        raise ImageRefusal(
            PROMOTE_PIN_LINE_AMBIGUOUS,
            f"found {len(found)} plain launch_template_version assignments in the [aws] table; exactly one is needed",
        )
    number, match = found[0]
    key, old_value, rest = match.groups()
    lines[number] = f"{key}{new_pin}{rest}"
    return "\n".join(lines), {"line": number + 1, "from": f"{key}{old_value}", "to": f"{key}{new_pin}"}


def check_reparse(snapshot, new_text, new_pin):
    """Refuse unless the strict loader accepts the new text and it differs from the old only in the pin."""
    try:
        reloaded = configuration.loads(new_text, snapshot.config.path)
    except ConfigRefusal as refusal:
        raise ImageRefusal(
            PROMOTE_REPARSE_FAILED, f"the strict loader refused the new text: {refusal.reason}"
        ) from None
    new_raw = tomllib.loads(new_text)  # the loader accepted this text, so it parses
    expected = {key: (dict(value) if isinstance(value, dict) else value) for key, value in snapshot.raw.items()}
    expected["aws"] = {**snapshot.raw["aws"], "launch_template_version": new_pin}
    if new_raw != expected or reloaded.aws.launch_template_version != new_pin:
        raise ImageRefusal(PROMOTE_REPARSE_FAILED, "the new text differs from the old in more than the pin")


# -- the files -----------------------------------------------------------------------


def _create(path, data, snapshot):
    """Write ``data`` to a new file with the mode of the target, flushed. Never replaces a file."""
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        os.fchmod(descriptor, snapshot.mode)
        try:
            os.fchown(descriptor, snapshot.uid, snapshot.gid)
        except OSError:
            pass  # the owner stays the caller's when the caller may not hand a file over
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
    except BaseException:
        _remove(path)
        raise


def _remove(path):
    try:
        os.unlink(path)
    except FileNotFoundError:
        pass


def _discard(path):
    """Remove ``path`` if that works. For the way out of a failure: a second error must not replace the first."""
    with suppress(OSError):
        os.unlink(path)


def _errno_name(error):
    """The errno name of an ``OSError`` (``ENOSPC``), or ``OSError`` when it has none. A detail says no more of it."""
    return errno.errorcode.get(error.errno, "OSError")


def _create_backup(snapshot, now):
    stamp = datetime.fromtimestamp(now, UTC).strftime(_BACKUP_STAMP)
    base = snapshot.target.name + _BACKUP_INFIX + stamp
    for attempt in range(100):
        path = snapshot.target.parent / (base if attempt == 0 else f"{base}-{attempt}")
        try:
            _create(path, snapshot.data, snapshot)
        except FileExistsError:
            continue
        return path
    raise ImageRefusal(PROMOTE_WRITE_FAILED, "no free backup name")


def _flush_directory(directory):
    descriptor = os.open(directory, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _write_pin(state, snapshot, *, kind, new_pin, candidate_id, clock):
    """Steps 4 to 8 of the design for one pin change. The caller holds both locks.

    An ``OSError`` of a local file step is the refusal ``PROMOTE_WRITE_FAILED``, and its detail names the errno and
    nothing else (never ``str(error)``, which holds a path). Before the rename the deployment file is unchanged and
    the temporary file and the backup are removed again. A rename that fails aborts the row and keeps the backup.
    After the rename the pin may have moved, so the row stays ``pending`` and the next command that resolves it
    decides from the file hash. Any other exception is not mapped.
    """
    new_text, change = plan_edit(snapshot.text, new_pin)
    check_reparse(snapshot, new_text, new_pin)
    new_data = new_text.encode("utf-8")
    new_sha = sha256_of(new_data)
    target = snapshot.target
    temporary = target.parent / (target.name + _TEMPORARY_SUFFIX)
    backup = None
    committed = False
    try:
        _remove(temporary)  # a crashed run left it; this run holds the lock
        _create(temporary, new_data, snapshot)
        backup = _create_backup(snapshot, clock())
        if sha256_of(target.read_bytes()) != snapshot.sha256:
            raise ImageRefusal(PROMOTE_FILE_CHANGED, "the file changed after it was read; nothing was written")
        with state.transaction():
            if kind == "promotion":
                _record_previous_pin(state, snapshot)
            row = state.journal_insert(
                kind=kind,
                old_pin=snapshot.pin,
                new_pin=new_pin,
                old_file_sha256=snapshot.sha256,
                new_file_sha256=new_sha,
                backup_name=backup.name,
                backup_sha256=snapshot.sha256,
                contained_pin=snapshot.pin,
                candidate_id=candidate_id,
            )
        committed = True
    except BaseException as error:
        if not committed:
            _discard(temporary)
            if backup is not None:
                _discard(backup)
        if isinstance(error, OSError):
            raise ImageRefusal(
                PROMOTE_WRITE_FAILED,
                f"a local file step failed ({_errno_name(error)}); the deployment file was not changed",
            ) from None
        raise
    try:
        os.replace(temporary, target)
    except OSError as error:
        with state.transaction():
            state.journal_abort(row.id)
        _discard(temporary)
        raise ImageRefusal(
            PROMOTE_WRITE_FAILED,
            f"the rename failed ({_errno_name(error)}); the backup {backup.name} is kept",
        ) from None
    try:
        _flush_directory(target.parent)
    except OSError as error:
        raise ImageRefusal(
            PROMOTE_WRITE_FAILED,
            f"the directory flush after the rename failed ({_errno_name(error)}): the pin may have moved and the "
            "journal row stays pending. The next image promote --write, image promote --rollback --write or "
            "image run --apply resolves it from the file hash",
        ) from None
    with state.transaction():
        state.journal_complete(row.id)
        if kind == "promotion":
            _mark_promoted(state, row)
    return {
        "journal_id": row.id,
        "old_pin": snapshot.pin,
        "new_pin": new_pin,
        "backup": backup.name,
        "backup_sha256": snapshot.sha256,
        "file_sha256_before": snapshot.sha256,
        "file_sha256_after": new_sha,
        "change": change,
    }


def _record_previous_pin(state, snapshot):
    """Record the image behind the pin that this promotion replaces, when no build owns it.

    A pin that an operator set by hand has no build row. Its image id comes from
    the pin observations that a pass left. Nothing here says the image still
    exists: that is evidence only a describe read can give, and
    ``image status --refresh`` records it.
    """
    deployment = snapshot.config.deployment
    if state.version_owner(deployment, snapshot.pin) is not None:
        return
    seen = state.pin_for_version(snapshot.pin)
    if seen is not None:
        state.insert_pin_record(deployment=deployment, version=snapshot.pin, image_id=seen.image_id)


# -- rollback ------------------------------------------------------------------------


@dataclass(frozen=True)
class RollbackPlan:
    row: object
    previous_pin: int
    backup_name: str
    target: object


def rollback_plan(state, snapshot, now):
    """What ``--rollback`` would do, or the refusal that stops it.

    It uses the backup that the newest completed promotion row names and no other
    (a newer backup of an aborted row or of a rollback is never read).
    """
    config = snapshot.config
    row = state.journal_last_complete(kind="promotion")
    if row is None:
        raise ImageRefusal(ROLLBACK_NO_PROMOTION, "no completed promotion is recorded")
    if row.new_pin != snapshot.pin:
        raise StateRefusal(
            PIN_RECORD_DIVERGED,
            f"the newest completed promotion wrote pin {row.new_pin} and the file pins {snapshot.pin}",
        )
    _check_backup(snapshot, row)
    owner = state.version_owner(config.deployment, row.old_pin)
    if owner is None or owner.image_state is None or owner.template_version_exists is None:
        raise ImageRefusal(
            ROLLBACK_PREVIOUS_NOT_RECORDED,
            f"launch-template version {row.old_pin} and its image are not recorded as read: run image status --refresh",
        )
    if owner.template_version_exists != 1 or owner.image_state != AVAILABLE:
        raise ImageRefusal(
            ROLLBACK_PREVIOUS_MISSING,
            f"the record shows launch-template version {row.old_pin} existing {_flag(owner.template_version_exists)}"
            f" and its image {owner.image_state}",
        )
    check_fresh(owner.verified_at, now, config.image.promote_max_age_seconds)
    return RollbackPlan(row=row, previous_pin=row.old_pin, backup_name=row.backup_name, target=owner)


def _is_generated_backup_name(target_name, name):
    """Whether ``_create_backup`` can give ``name`` to a backup of ``target_name``: a real UTC time and a counter."""
    pattern = re.escape(target_name + _BACKUP_INFIX) + r"([0-9]{8}T[0-9]{6}Z)(?:-[1-9][0-9]?)?"
    match = re.fullmatch(pattern, name)
    if match is None:
        return False
    try:
        datetime.strptime(match.group(1), _BACKUP_STAMP)
    except ValueError:  # the shape of a stamp, but no such time: 20260230T010000Z or 20260101T250000Z
        return False
    return True


def _read_regular_file(path):
    """The bytes of ``path``, which must be a regular file itself.

    The file is opened without following a symbolic link and without waiting for a writer, so a link or a FIFO in
    place of a backup is an ``OSError`` here and never a read of another file or a hang.
    """
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC)
    try:
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise OSError(errno.EINVAL, "not a regular file")
        chunks = []
        while chunk := os.read(descriptor, 1 << 16):
            chunks.append(chunk)
        return b"".join(chunks)
    finally:
        os.close(descriptor)


def _check_backup(snapshot, row):
    """The named backup must exist, hold the recorded bytes and contain the recorded previous pin."""
    name = row.backup_name
    # Only a name that ``_create_backup`` makes is opened or shown: anything else in the row is damage or a hand
    # edit, and it is named by the row id, never echoed.
    if not _is_generated_backup_name(snapshot.target.name, name):
        raise StateRefusal(
            PIN_RECORD_DIVERGED, f"journal row {row.id} names a backup that this tool does not make next to the file"
        )
    try:
        data = _read_regular_file(snapshot.target.parent / name)
    except OSError:
        raise StateRefusal(PIN_RECORD_DIVERGED, f"the backup {name} cannot be read as a regular file") from None
    if sha256_of(data) != row.backup_sha256:
        raise StateRefusal(PIN_RECORD_DIVERGED, f"the backup {name} no longer has its recorded sha256")
    try:
        pin = tomllib.loads(data.decode("utf-8"))["aws"]["launch_template_version"]
    # ``ValueError`` covers a bad encoding, bad TOML and an integer longer than Python converts.
    except (ValueError, KeyError, TypeError, RecursionError):
        raise StateRefusal(PIN_RECORD_DIVERGED, f"the backup {name} cannot be parsed for its pin") from None
    # An integer only: ``3.0`` equals ``3`` but the strict loader refuses it, so it is not the recorded pin.
    if type(pin) is not int or pin != row.old_pin or row.contained_pin != row.old_pin:
        raise StateRefusal(PIN_RECORD_DIVERGED, f"the backup {name} does not contain the recorded previous pin")


def try_rollback_plan(state, snapshot, now):
    """``(plan, None)``, or ``(None, refusal)`` when ``--rollback`` would refuse."""
    try:
        return rollback_plan(state, snapshot, now), None
    except (ImageRefusal, StateRefusal) as refusal:
        return None, refusal


def describe_rollback(plan, refusal):
    """What ``--rollback`` would do today, as a report that never raises."""
    if plan is None:
        return {"available": False, **refusal.as_dict()}
    owner = plan.target
    return {
        "available": True,
        "to_pin": plan.previous_pin,
        "backup": plan.backup_name,
        "image_id": owner.image_id,
        "image_state": owner.image_state,
        "verified_at": owner.verified_at,
    }


# -- the commands --------------------------------------------------------------------


def promote(config, *, state, clock, events, version=None, rollback=False, write=False):
    """Print the evidence of a promotion or a rollback, or write it.

    ``state`` is an ``ImageState`` that is not connected; this function opens
    it as the form needs and closes it. Without ``write`` the command reads only
    local files (the state file, the deployment TOML and the backup that a
    journal row names) and builds no client. The state is opened for reading
    only: nothing is created, locked or written, and a pending journal row is
    reported with the resolution that the file hash implies, not resolved. The
    candidate, the rollback target and the plan are those that ``--write`` finds
    after it resolves the row: the command resolves the rows on a copy of the
    state in memory and drops the copy. A row that the file matches by neither
    hash is not resolved, as ``--write`` refuses to resolve it, so the command
    plans against that row as it is. Once there is a candidate or a rollback
    row to show, the write check is the first planning refusal that ``--write``
    meets for the files and rows as read (a lock that another command holds at
    the time of the write is not foreseen).
    Without one, the selection refusal is raised before the directory, zone-pin
    and divergence checks, so in a state that holds two refusals the command can
    name another one than ``--write`` names first. A refusal is raised.

    A local file-system error in the write path is the refusal
    ``PROMOTE_WRITE_FAILED``, and its detail names the errno and no path.
    ``_write_pin`` says what each failure leaves behind.

    A write appends one ``image_promoted`` event through ``events``, a function
    of the event name and its fields. The command line hands in the writer of
    the passes (``image_pipeline.write_event``) with the state directory, the
    deployment and the clock bound, so the event goes to the same file under the
    same allowlist and this module imports no adapter. The event is written
    after the journal row is complete and never before. It holds the previous and
    the new pin, the candidate's build id (``None`` for a rollback), the image id
    behind the new pin, whether it was a rollback and the short digest of the new
    file: ids, digests, counts and codes, no path, no content of the file and no
    URL. A dry run, a refusal and the resolution of a pending row write none.

    By the time the event is written the pin has moved. A failing write (the
    file system's ``OSError``, or a value that the allowlist refuses) therefore
    does not raise and does not undo the promotion: the result's ``notes`` lists
    ``event_write:<code>``, an errno name or an exception type and never a path.
    ``notes`` is empty when the event was written.
    """
    if write:
        snapshot = read_snapshot(config)
        check_state_owner(config, snapshot, state)
        return _write(snapshot, state, clock, events, version, rollback, owner=config)
    # The preview holds the file open until it has checked it again after the copy of the state.
    snapshot = read_snapshot(config, hold=True)
    try:
        check_state_owner(config, snapshot, state)
        return _preview(snapshot, state, clock, version, rollback)
    finally:
        os.close(snapshot.held)


def check_state_owner(config, snapshot, state):
    """Refuse ``PROMOTE_STATE_CHANGED`` unless ``state`` and the events belong to the file that ``snapshot`` read.

    The command line picks the state directory, and binds the event writer to the deployment, from the file as it
    loaded it at startup. Every pin fact comes from ``snapshot``, a later read. When the file changed its
    ``[image].state_directory`` or its ``deployment`` in between, a preview would plan from another database than the
    file names, and a write would journal the move where the next recovery command does not look (#199). The detail
    names no path.
    """
    planned = snapshot.config
    directory = planned.image.state_directory
    same_directory = directory is not None and Path(directory).resolve() == Path(state.directory).resolve()
    if planned.deployment != config.deployment or not same_directory:
        raise ImageRefusal(
            PROMOTE_STATE_CHANGED,
            "the deployment file names another state directory or deployment than the one this command opened; "
            "run the command again; nothing was written",
        )


def _missing_state(rollback):
    if rollback:
        return ImageRefusal(ROLLBACK_NO_PROMOTION, "no image state file exists, so no promotion is recorded")
    return ImageRefusal(PROMOTE_NO_CANDIDATE, "no image state file exists, so no candidate is recorded")


def _first_refusal(*checks):
    """The first refusal that a check raises, or ``None``. Every check only reads."""
    for check in checks:
        try:
            check()
        except (ImageRefusal, StateRefusal) as refusal:
            return refusal
    return None


def _check_edit(snapshot, new_pin):
    new_text, _ = plan_edit(snapshot.text, new_pin)
    check_reparse(snapshot, new_text, new_pin)


def _diverged(pending):
    return any(item["implied_resolution"] == "diverged" for item in pending)


def _check_no_divergence(pending):
    if _diverged(pending):
        raise StateRefusal(PROMOTION_JOURNAL_DIVERGED, "the file matches neither hash of a pending journal row")


def _check_plan(refusal):
    """Raise the refusal that planning the rollback met, if it met one (``try_rollback_plan`` caught it)."""
    if refusal is not None:
        raise refusal


def _write_check(refusal):
    return {"ok": refusal is None, **(refusal.as_dict() if refusal is not None else {})}


def _try_change(snapshot, new_pin):
    try:
        return plan_edit(snapshot.text, new_pin)[1]
    except ImageRefusal:
        return None


def _preview(snapshot, state, clock, version, rollback):
    config = snapshot.config
    if not state.connect_readonly():
        raise _missing_state(rollback)
    try:
        planned = state.detached_copy()
    finally:
        state.close()
    # The file was read before the copy. A promotion that renamed the file in between would leave the preview
    # resolving its row against the old hash, so the file is checked again: the copy lies between two reads of the
    # same file with the same bytes, or the preview refuses. Nothing is locked for this.
    if not _is_unchanged(snapshot):
        planned.close()
        raise ImageRefusal(
            PROMOTE_FILE_CHANGED, "the file changed while the preview read it and the state; run the preview again"
        )
    pending = []
    try:
        # The report of the pending rows comes first, so a refusal still carries it; then every row, as the write
        # checks them before it resolves a pending row, so a damaged row refuses both alike.
        pending = pending_report(planned, snapshot.sha256, readable_only=True)
        planned.journal_check()
        # ``--write`` resolves a pending row before it plans, so the preview plans against the rows as that leaves
        # them. The copy takes the resolution and the file never does. A row that matches neither hash is not
        # resolved, as the write refuses to resolve it: the copy keeps that row, and ``_check_no_divergence`` puts
        # it in the write check. Any other refusal of the resolution is the write's too, so it is raised here.
        if not _diverged(pending):
            resolve_pending(planned, snapshot.sha256)
        # The clock is read once the state is copied and resolved: evidence that a refresh wrote just before the copy
        # is not in the future of this preview, as it is not in the future of the write, which reads the clock then.
        now = clock()
        plan, refusal = try_rollback_plan(planned, snapshot, now)
        report = {"pending_journal": pending, "rollback": describe_rollback(plan, refusal)}
        if rollback:
            row = planned.journal_last_complete(kind="promotion")
            if row is None:
                raise refusal
            # The checks of ``--rollback --write``, in the order it runs them. It writes the previous pin
            # of the row that the plan chose, so that is the pin whose edit is checked.
            first = _first_refusal(
                lambda: check_directory(snapshot.target),
                lambda: check_zone_pins(snapshot.raw),
                lambda: _check_no_divergence(pending),
                lambda: _check_plan(refusal),
                lambda: _check_edit(snapshot, row.old_pin),
            )
            return {
                "decision": "preview",
                "action": "rollback",
                "pin_change": _try_change(snapshot, row.old_pin),
                "write_check": _write_check(first),
                **report,
            }
        build = select_candidate(planned, config.deployment, version)
        refusal = _first_refusal(
            lambda: check_directory(snapshot.target),
            lambda: check_zone_pins(snapshot.raw),
            lambda: _check_no_divergence(pending),
            lambda: verify_candidate(build, config.image, now, snapshot.pin),
            lambda: _check_edit(snapshot, build.template_version),
        )
        return {
            "decision": "preview",
            "action": "promote",
            "evidence": evidence(build, now),
            "pin_change": _try_change(snapshot, build.template_version),
            "previous_pin": _pin_report(planned, snapshot),
            "write_check": _write_check(refusal),
            **report,
        }
    except Refusal as refusal:
        # A refused preview still says that a row is pending and how the hash resolves it: the refusal can be
        # the consequence of that resolution, and the row on disk stays pending until a write resolves it.
        if pending:
            refusal.report = {**refusal.report, "pending_journal": pending}
        raise
    finally:
        planned.close()


def _pin_report(state, snapshot):
    """The pin that a promotion would replace, with what the state records about its image."""
    owner = state.version_owner(snapshot.config.deployment, snapshot.pin)
    seen = state.pin_for_version(snapshot.pin)
    image_id = owner.image_id if owner is not None and owner.image_id else (seen.image_id if seen else None)
    return {
        "version": snapshot.pin,
        "image_id": image_id,
        "recorded": owner is not None,
        "image_state": owner.image_state if owner is not None else None,
        "verified_at": owner.verified_at if owner is not None else None,
    }


def _announce(events, done, *, build_id, image_id, rollback):
    """Append the ``image_promoted`` event of a completed write; return the notes that it leaves in the result.

    The caller has completed the journal row, so the pin has moved: a failing write is a note and never an error,
    because an error would turn a finished promotion into an unexpected failure. The note carries the errno name or
    the type name and never ``str(error)``, which holds the path.
    """
    try:
        events(
            "image_promoted",
            old_pin=done["old_pin"],
            new_pin=done["new_pin"],
            build_id=build_id,
            image_id=image_id,
            rollback=rollback,
            file_digest=_short(done["file_sha256_after"]),
        )
    except (OSError, ValueError) as error:
        code = _errno_name(error) if isinstance(error, OSError) else type(error).__name__
        return [f"event_write:{code}"]
    return []


def _write(snapshot, state, clock, events, version, rollback, *, owner):
    # Nothing is written until the checks that need only the file pass.
    check_directory(snapshot.target)
    check_zone_pins(snapshot.raw)
    if not state.probe_for_write():
        raise _missing_state(rollback)
    with file_lock(snapshot.target):
        try:
            state.acquire_writer()  # before the writable open, which rolls back a hot journal
            state.connect()
            # The first read decided only the two refusals above. Every other fact comes from bytes
            # read now, with both locks held: a promotion that finished after the first read must
            # not make its own pending row look aborted.
            checked = snapshot.target
            snapshot = read_snapshot(snapshot.config)
            if snapshot.target != checked:
                # The path was followed to another file: the directory that was checked and the lock
                # that is held belong to the first one, so nothing may be written to this one.
                raise ImageRefusal(
                    PROMOTE_FILE_CHANGED,
                    "the deployment path leads to another file than the one that was checked and locked; "
                    "nothing was written",
                )
            check_zone_pins(snapshot.raw)
            # The bytes read under the locks decide too: the state and the events must still be the ones they name.
            check_state_owner(owner, snapshot, state)
            config = snapshot.config
            # Every row first: a damaged completed row must stop the write here, not after the file changed.
            state.journal_check()
            resolved = resolve_pending(state, snapshot.sha256)
            now = clock()
            if rollback:
                plan = rollback_plan(state, snapshot, now)
                subject = {"to_pin": plan.previous_pin, "target": plan.target.id}
                done = _write_pin(
                    state,
                    snapshot,
                    kind="rollback",
                    new_pin=plan.previous_pin,
                    candidate_id=plan.target.id,
                    clock=clock,
                )
                decision, build_id, image_id = "rolled_back", None, plan.target.image_id
            else:
                build = select_candidate(state, config.deployment, version)
                verify_candidate(build, config.image, now, snapshot.pin)
                subject = {"evidence": evidence(build, now)}
                done = _write_pin(
                    state,
                    snapshot,
                    kind="promotion",
                    new_pin=build.template_version,
                    candidate_id=build.id,
                    clock=clock,
                )
                decision, build_id, image_id = "promoted", build.id, build.image_id
            # The rename is done and the row is complete: from here on nothing may undo or hide the promotion.
            notes = _announce(events, done, build_id=build_id, image_id=image_id, rollback=rollback)
        finally:
            state.close()
    return {"decision": decision, "resolved": resolved, **subject, **done, "notes": notes}
