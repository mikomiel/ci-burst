"""Qualified, read-only observation of one Forgejo repository.

The adapter knows two interfaces: a JSON array of ``ActionRunJob`` records from
the repository runner-jobs endpoint, from which demand is derived, and a JSON
array of runner registrations from the repository runners endpoint, from which
the fleet snapshot is derived. Both are read-only: the adapter performs no
mutation, sets no job fields, and never treats an unreadable or unexpected
response as an empty queue. Live provisioning additionally requires the operator
to declare ``live-verified`` in configuration; the bundled contracts are
validated against synthetic fixtures.
"""

import json
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field

from .errors import DeadlineRefusal, ObservationRefusal

CONTRACT = "forgejo-17.0.1-runners-jobs"

#: The registration list is a second interface with its own qualified shape.
#: The endpoint is owner-scoped in Forgejo 17.0.1, the same requirement the
#: runner-jobs endpoint carries, so the controller's existing token serves both.
RUNNERS_CONTRACT = "forgejo-17.0.1-runners"
RUNNER_STATUSES = frozenset({"active", "offline"})

#: Statuses the qualified contract understands. A record with any other value
#: makes runnable semantics unproven for the whole observation, so demand is
#: reported unavailable rather than guessed.
#:
#: ``waiting`` is the only runnable status, and it means "awaiting a runner":
#: the server reports a job that way while its dependencies are already done.
#: ``blocked`` is the server's dependency signal, so dependency-blocked work is
#: counted and never launched. ``needs`` is static workflow metadata -- it stays
#: populated after the dependency has succeeded -- so it never decides
#: eligibility. Measured on ``forgejo 17.0.1+gitea-1.22.0``: a ``waiting`` job
#: still lists the dependency that already succeeded, and a dependent job
#: reports ``blocked`` until it does.
RUNNABLE_STATUS = "waiting"
BLOCKED_STATUS = "blocked"
ASSIGNED_OR_TERMINAL_STATUSES = frozenset({"running", "success", "failure", "cancelled", "skipped"})


@dataclass(frozen=True)
class Attempt:
    """One observed job attempt, identified by its contract handle."""

    handle: str
    job_id: int
    attempt: int
    status: str
    labels: tuple
    needs: tuple


@dataclass(frozen=True)
class Observation:
    contract: str
    attempts: tuple
    seen: int
    blocked: int
    assigned: int
    incompatible: int
    duplicates: int
    #: Running attempts whose complete label set the configured worker supports.
    #: This is fleet context, never demand: a running attempt is already
    #: assigned, and nothing here claims that this deployment's workers run it.
    running: int = 0
    notes: tuple = field(default=())

    @property
    def demand(self):
        return len(self.attempts)

    @property
    def status(self):
        return "demand" if self.attempts else "empty"

    def as_dict(self):
        return {
            "contract": self.contract,
            "status": self.status,
            "demand_attempts": [_safe_identifier(attempt.handle) for attempt in self.attempts],
            "jobs_seen": self.seen,
            "blocked": self.blocked,
            "assigned_or_finished": self.assigned,
            "running_attempts": self.running,
            "incompatible_labels": self.incompatible,
            "duplicate_records": self.duplicates,
            "notes": list(self.notes),
        }


@dataclass(frozen=True)
class Runners:
    """One bounded snapshot of a repository's runner registrations.

    Counts only: a registration is evidence that *some* worker registered, not
    that this deployment launched it, and no field here links a registration to
    a job attempt. The ephemeral counts are the fleet's own workers because the
    shipped bootstrap registers an ephemeral runner.
    """

    contract: str
    total: int
    active: int
    offline: int
    ephemeral_total: int
    ephemeral_active: int
    ephemeral_offline: int


class UrllibTransport:
    """Bounded HTTPS GET with redirects refused.

    Redirects are refused outright: the request carries an Authorization
    header, and following a cross-origin redirect would disclose it. TLS
    verification stays at the standard-library default and is never disabled.
    """

    def __init__(self, timeout_seconds, max_response_bytes, clock=time.time):
        self._timeout = timeout_seconds
        self._limit = max_response_bytes
        self._clock = clock

    def get(self, url, headers):
        request = urllib.request.Request(url, headers=headers, method="GET")
        try:
            with self._open(request) as response:
                status = response.status
                body = response.read(self._limit + 1)
        except (urllib.error.URLError, TimeoutError, OSError) as error:
            raise _transport_refusal(error) from None
        if len(body) > self._limit:
            raise ObservationRefusal("FORGEJO_RESPONSE_TOO_LARGE", f"limit {self._limit} bytes")
        if status != 200:
            raise ObservationRefusal("FORGEJO_UNEXPECTED_STATUS", f"HTTP {status}")
        return body

    def stream(self, url, headers, max_bytes, chunk_bytes=65536, deadline=None):
        """Yield the body in chunks and abort once it passes ``max_bytes``.

        The request opens on the first ``next()``. Passing the cap closes the
        connection before the refusal is raised, so no byte beyond the cap
        past one chunk is read, and nothing is buffered or written to disk.

        The socket timeout bounds one operation, not the transfer, so a server
        that drips bytes could hold the loop. ``deadline`` is an absolute time
        on this transport's clock, the convention of ``aws.py``: it is checked
        before every read and refuses with ``DeadlineRefusal`` once reached.
        Each read is ``read1``, at most one receive, so a drip cannot keep one
        read filling a chunk past the deadline; the socket timeout is also cut
        to the time left, so one receive overruns the deadline by at most that.
        """
        request = urllib.request.Request(url, headers=headers, method="GET")
        timeout = self._timeout
        if deadline is not None:
            remaining = deadline - self._clock()
            if remaining <= 0:
                raise DeadlineRefusal("archive stream")
            timeout = min(timeout, remaining)
        try:
            with self._open(request, timeout) as response:
                if response.status != 200:
                    raise ObservationRefusal("FORGEJO_UNEXPECTED_STATUS", f"HTTP {response.status}")
                received = 0
                while True:
                    if deadline is not None and self._clock() >= deadline:
                        raise DeadlineRefusal("archive stream")
                    chunk = response.read1(min(chunk_bytes, max_bytes - received + 1))
                    if not chunk:
                        return
                    received += len(chunk)
                    if received > max_bytes:
                        raise ObservationRefusal("FORGEJO_ARCHIVE_TOO_LARGE", f"limit {max_bytes} bytes")
                    yield chunk
        except (urllib.error.URLError, TimeoutError, OSError) as error:
            raise _transport_refusal(error) from None

    def _open(self, request, timeout=None):
        """Open one request; tests replace this boundary with a fake response."""
        timeout = self._timeout if timeout is None else timeout
        return urllib.request.build_opener(_RefuseRedirects()).open(request, timeout=timeout)


def _transport_refusal(error):
    if isinstance(error, urllib.error.HTTPError):
        return ObservationRefusal("FORGEJO_UNEXPECTED_STATUS", f"HTTP {error.code}")
    return ObservationRefusal("FORGEJO_UNAVAILABLE", type(error).__name__)


class _RefuseRedirects(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, file_pointer, code, message, headers, new_url):
        raise ObservationRefusal("FORGEJO_REDIRECT_REFUSED", f"HTTP {code}")


class ForgejoObserver:
    """Read demand without mutating Forgejo."""

    def __init__(self, config, transport=None, environ=None):
        self._config = config
        self._transport = transport or UrllibTransport(
            config.forgejo.timeout_seconds, config.forgejo.max_response_bytes
        )
        self._environ = environ if environ is not None else os.environ

    def endpoint(self):
        query = urllib.parse.urlencode({"labels": self._label_filter()})
        return self._repository_api("/actions/runners/jobs?" + query)

    def runners_endpoint(self):
        return self._repository_api("/actions/runners")

    def _repository_api(self, suffix):
        owner, repository = self._config.forgejo.repository.split("/", 1)
        return f"{self._config.forgejo.base_url}/api/v1/repos/{owner}/{repository}{suffix}"

    def _label_filter(self):
        """The bare label names the worker advertises, as the server expects.

        An absent ``labels`` parameter is not a narrowing filter: the endpoint
        then admits every waiting or running job in the repository, so the
        parameter is mandatory rather than an optimisation. It is only a
        prefilter: the complete ``runs_on`` comparison in ``_reduce`` still
        decides eligibility, so a job selected by one shared label remains
        incompatible when it also requires a label the worker does not advertise.
        """
        return ",".join(sorted({label.split(":", 1)[0] for label in self._config.worker.labels}))

    def _token(self):
        token = self._environ.get(self._config.forgejo.token_env)
        if not token:
            raise ObservationRefusal("FORGEJO_TOKEN_MISSING", self._config.forgejo.token_env)
        return token

    def _headers(self, token):
        return {
            "Authorization": f"token {token}",
            "Accept": "application/json",
            "User-Agent": "ci-burst/0.1",
        }

    def observe(self):
        body = self._transport.get(self.endpoint(), self._headers(self._token()))
        records = _parse_records(body)
        return _reduce(records, self._worker_label_names())

    def runners(self):
        """Read the registration list, a contract separate from the queue.

        The caller must treat a refusal as unavailable fleet evidence, not as
        an admission refusal: the queue remains the only gate on launching.
        """
        body = self._transport.get(self.runners_endpoint(), self._headers(self._token()))
        records = _parse_runners(body)
        return _reduce_runners(records)

    def _worker_label_names(self):
        names = set()
        for label in self._config.worker.labels:
            names.add(label)
            names.add(label.split(":", 1)[0])
        return frozenset(names)


#: A git object id: 40 hex digits (sha1) or 64 (sha256). The archive is only ever
#: requested by one of these, never by a branch or tag name.
_COMMIT_SHA = re.compile(r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")

_FORBIDDEN_DETAILS = frozenset({"HTTP 401", "HTTP 403"})

#: Printable ASCII without space. A token outside this set is refused before it
#: reaches a header: http.client would quote the rejected value in its error.
_TOKEN_SHAPE = re.compile(r"[\x21-\x7e]+")


@dataclass(frozen=True)
class BranchHead:
    """The head commit of one branch and the forge's protected flag."""

    sha: str
    protected: bool


class ForgejoSource:
    """Read-only access to the source of one repository, for the image build.

    Separate from ``ForgejoObserver`` on purpose: it takes the name of its own
    token variable and nothing else, so the token that ``apply`` uses cannot be
    read here and there is no fallback to it. It takes plain parameters, no
    configuration object. Every read refuses with ``IMAGE_SOURCE_FORBIDDEN`` on
    a 401 or 403 and never echoes the token.
    """

    def __init__(
        self,
        base_url,
        repository,
        token_env,
        transport=None,
        environ=None,
        timeout_seconds=10,
        max_response_bytes=1_048_576,
    ):
        self._base_url = base_url
        self._owner, self._repository = repository.split("/", 1)
        self._token_env = token_env
        self._transport = transport or UrllibTransport(timeout_seconds, max_response_bytes)
        self._environ = environ if environ is not None else os.environ

    def branch(self, name):
        """Return the head sha and the protected flag of ``name``.

        Judging the flag is the caller's: a missing or false flag is
        ``IMAGE_BRANCH_UNPROTECTED`` through ``require_protected``.
        """
        if not name or name.startswith("/") or ".." in name.split("/"):
            raise ObservationRefusal("FORGEJO_UNEXPECTED_SHAPE", "branch name escapes the repository")
        body = self._get(self._api("/branches/" + urllib.parse.quote(name, safe="/")))
        try:
            payload = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise ObservationRefusal("FORGEJO_INVALID_JSON") from None
        commit = payload.get("commit") if isinstance(payload, dict) else None
        sha = commit.get("id") if isinstance(commit, dict) else None
        if not isinstance(sha, str) or not _COMMIT_SHA.fullmatch(sha):
            raise ObservationRefusal("FORGEJO_UNEXPECTED_SHAPE", "branch has no commit id")
        return BranchHead(sha=sha, protected=payload.get("protected") is True)

    def raw_file(self, sha, path):
        """Return the bytes of ``path`` at ``sha``; the read also tests the token.

        An absent path is ``IMAGE_BASIS_PATH_MISSING``.
        """
        if not _COMMIT_SHA.fullmatch(sha):
            raise ObservationRefusal("FORGEJO_UNEXPECTED_SHAPE", "ref must be a commit sha")
        if path.startswith("/") or ".." in path.split("/"):
            raise ObservationRefusal("IMAGE_BASIS_PATH_MISSING", "path escapes the repository")
        url = self._api("/raw/" + urllib.parse.quote(path, safe="/") + "?" + urllib.parse.urlencode({"ref": sha}))
        try:
            return self._get(url)
        except ObservationRefusal as refusal:
            if refusal.reason == "FORGEJO_UNEXPECTED_STATUS" and refusal.detail == "HTTP 404":
                raise ObservationRefusal("IMAGE_BASIS_PATH_MISSING") from None
            raise

    def archive(self, sha, max_bytes, deadline=None):
        """Yield the ``.tar.gz`` archive of commit ``sha`` in chunks.

        Requested by sha, never by branch. The request opens on the first
        ``next()``. The stream aborts with ``FORGEJO_ARCHIVE_TOO_LARGE`` once it
        passes ``max_bytes``, and with ``DeadlineRefusal`` once the optional
        ``deadline`` (an absolute time on the transport's clock) is reached. The
        caller closes the generator to abandon it.
        """
        if not _COMMIT_SHA.fullmatch(sha):
            raise ObservationRefusal("FORGEJO_UNEXPECTED_SHAPE", "archive must be requested by commit sha")
        url = self._api(f"/archive/{sha}.tar.gz")
        try:
            yield from self._transport.stream(url, self._headers(), max_bytes, deadline=deadline)
        except ObservationRefusal as refusal:
            raise _source_refusal(refusal) from None

    def _api(self, suffix):
        return f"{self._base_url}/api/v1/repos/{self._owner}/{self._repository}{suffix}"

    def _headers(self):
        token = self._environ.get(self._token_env)
        if not token:
            raise ObservationRefusal("FORGEJO_TOKEN_MISSING", self._token_env)
        if not _TOKEN_SHAPE.fullmatch(token):
            raise ObservationRefusal("FORGEJO_TOKEN_INVALID", self._token_env)
        return {"Authorization": f"token {token}", "User-Agent": "ci-burst/0.1"}

    def _get(self, url):
        try:
            return self._transport.get(url, self._headers())
        except ObservationRefusal as refusal:
            raise _source_refusal(refusal) from None


def require_protected(head):
    """Refuse unless the forge reports the branch protected; there is no override."""
    if not head.protected:
        raise ObservationRefusal("IMAGE_BRANCH_UNPROTECTED")
    return head


def _source_refusal(refusal):
    if refusal.reason == "FORGEJO_UNEXPECTED_STATUS" and refusal.detail in _FORBIDDEN_DETAILS:
        return ObservationRefusal("IMAGE_SOURCE_FORBIDDEN", refusal.detail)
    return refusal


_IDENTIFIER = re.compile(r"^[A-Za-z0-9._:-]{1,128}$")


def _safe_identifier(value):
    """Echo an opaque provider identifier only when its shape is bounded."""
    return value if _IDENTIFIER.fullmatch(value) else "<redacted>"


def _parse_records(body):
    try:
        payload = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise ObservationRefusal("FORGEJO_INVALID_JSON") from None
    if payload is None:
        # The server marshals its Go nil slice as JSON null when the label
        # filter matches nothing. That is a verified empty queue, not a shape
        # error: the request succeeded and the server answered.
        return []
    if not isinstance(payload, list):
        raise ObservationRefusal("FORGEJO_UNEXPECTED_SHAPE", "expected a JSON array of action run jobs")
    return payload


def _record(record, index):
    where = f"record {index}"
    if not isinstance(record, dict):
        raise ObservationRefusal("FORGEJO_UNEXPECTED_SHAPE", f"{where} is not an object")
    handle = record.get("handle")
    status = record.get("status")
    job_id = record.get("id")
    attempt = record.get("attempt")
    runs_on = record.get("runs_on")
    if not isinstance(handle, str) or not handle:
        raise ObservationRefusal("FORGEJO_UNEXPECTED_SHAPE", f"{where}.handle must be a non-empty string")
    if not isinstance(status, str) or not status:
        raise ObservationRefusal("FORGEJO_UNEXPECTED_SHAPE", f"{where}.status must be a non-empty string")
    if type(job_id) is not int:
        raise ObservationRefusal("FORGEJO_UNEXPECTED_SHAPE", f"{where}.id must be an integer")
    if type(attempt) is not int or attempt < 1:
        raise ObservationRefusal("FORGEJO_UNEXPECTED_SHAPE", f"{where}.attempt must be an integer of at least one")
    if not isinstance(runs_on, list) or any(not isinstance(label, str) or not label for label in runs_on):
        raise ObservationRefusal("FORGEJO_UNEXPECTED_SHAPE", f"{where}.runs_on must be an array of labels")
    if "needs" not in record:
        raise ObservationRefusal("FORGEJO_UNEXPECTED_SHAPE", f"{where}.needs is absent")
    needs = record["needs"]
    # Dependency metadata, carried but never used to decide eligibility: the
    # server populates it from the workflow and does not clear it when the
    # dependency finishes. A Go nil slice marshals as null and means "no
    # `needs:` key". An absent key is still a shape error, so the record is
    # refused rather than read as "no dependencies".
    if needs is not None and (
        not isinstance(needs, list) or any(not isinstance(need, str) for need in needs)
    ):
        raise ObservationRefusal("FORGEJO_UNEXPECTED_SHAPE", f"{where}.needs must be an array of strings")
    if status not in (RUNNABLE_STATUS, BLOCKED_STATUS) and status not in ASSIGNED_OR_TERMINAL_STATUSES:
        raise ObservationRefusal("FORGEJO_UNKNOWN_STATUS", "unrecognized job status")
    return handle, job_id, attempt, status, tuple(runs_on), tuple(needs or ())


def _parse_runners(body):
    try:
        payload = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise ObservationRefusal("FORGEJO_RUNNERS_INVALID_JSON") from None
    if payload is None:
        # A nil slice marshals as JSON null: a verified empty registration list.
        return []
    if not isinstance(payload, list):
        raise ObservationRefusal("FORGEJO_RUNNERS_UNEXPECTED_SHAPE", "expected a JSON array of runners")
    return payload


def _runner_record(record, index):
    where = f"runner {index}"
    if not isinstance(record, dict):
        raise ObservationRefusal("FORGEJO_RUNNERS_UNEXPECTED_SHAPE", f"{where} is not an object")
    name = record.get("name")
    if not isinstance(name, str) or not name:
        raise ObservationRefusal("FORGEJO_RUNNERS_UNEXPECTED_SHAPE", f"{where}.name must be a non-empty string")
    status = record.get("status")
    if not isinstance(status, str) or status not in RUNNER_STATUSES:
        raise ObservationRefusal("FORGEJO_RUNNERS_UNKNOWN_STATUS", where)
    labels = record.get("labels")
    if not isinstance(labels, list) or any(not isinstance(label, str) or not label for label in labels):
        raise ObservationRefusal("FORGEJO_RUNNERS_UNEXPECTED_SHAPE", f"{where}.labels must be an array of labels")
    ephemeral = record.get("ephemeral")
    if type(ephemeral) is not bool:
        raise ObservationRefusal("FORGEJO_RUNNERS_UNEXPECTED_SHAPE", f"{where}.ephemeral must be a boolean")
    return status, ephemeral


def _reduce_runners(records):
    """Count registrations by status and by the ephemeral flag.

    The shipped bootstrap registers an ephemeral runner, so only those counts
    describe this deployment's own workers; a persistent runner on the same
    repository is counted in the totals and left out of the fleet estimate.
    """
    total = active = offline = ephemeral_total = ephemeral_active = ephemeral_offline = 0
    for index, record in enumerate(records):
        status, ephemeral = _runner_record(record, index)
        total += 1
        if status == "active":
            active += 1
        else:
            offline += 1
        if ephemeral:
            ephemeral_total += 1
            if status == "active":
                ephemeral_active += 1
            else:
                ephemeral_offline += 1
    return Runners(
        contract=RUNNERS_CONTRACT,
        total=total,
        active=active,
        offline=offline,
        ephemeral_total=ephemeral_total,
        ephemeral_active=ephemeral_active,
        ephemeral_offline=ephemeral_offline,
    )


def _reduce(records, worker_labels):
    """Deduplicate attempts and keep only runnable, label-compatible demand.

    A ``waiting`` record is demand whatever its ``needs`` says; a ``blocked``
    record is dependency-blocked work and is counted, never launched; anything
    already assigned or finished is counted as such. A ``running`` record whose
    labels the worker supports is also counted as fleet context, never demand.
    """
    attempts = []
    seen = {}
    blocked = assigned = incompatible = duplicates = running = 0
    notes = []
    for index, record in enumerate(records):
        handle, job_id, attempt, status, labels, needs = _record(record, index)
        signature = (job_id, attempt, status, labels, needs)
        if handle in seen:
            if seen[handle] != signature:
                raise ObservationRefusal("FORGEJO_INCONSISTENT_ATTEMPT", "conflicting duplicate attempt")
            duplicates += 1
            continue
        seen[handle] = signature
        if status == BLOCKED_STATUS:
            blocked += 1
            continue
        if status == "running" and labels and set(labels).issubset(worker_labels):
            running += 1
        if status != RUNNABLE_STATUS:
            assigned += 1
            continue
        if not labels:
            incompatible += 1
            notes.append("job_without_labels")
            continue
        if not set(labels).issubset(worker_labels):
            incompatible += 1
            continue
        attempts.append(Attempt(handle=handle, job_id=job_id, attempt=attempt, status=status, labels=labels, needs=needs))
    attempts.sort(key=lambda item: (item.job_id, item.attempt, item.handle))
    return Observation(
        contract=CONTRACT,
        attempts=tuple(attempts),
        seen=len(records),
        blocked=blocked,
        assigned=assigned,
        incompatible=incompatible,
        duplicates=duplicates,
        running=running,
        notes=tuple(notes),
    )
