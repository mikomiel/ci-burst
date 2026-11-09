"""Qualified, read-only observation of one Forgejo repository job queue.

The adapter knows exactly one interface: a JSON array of ``ActionRunJob``
records from the repository runner-jobs endpoint. It performs no mutation, sets
no job fields, and never treats an unreadable or unexpected response as an empty
queue. Live provisioning additionally requires the operator to declare
``live-verified`` in configuration; the bundled contract is validated against
synthetic fixtures only.
"""

import json
import os
import re
import urllib.error
import urllib.request
from dataclasses import dataclass, field

from .errors import ObservationRefusal

CONTRACT = "forgejo-17.0.1-runners-jobs"

#: Statuses the qualified contract understands. A record with any other value
#: makes runnable semantics unproven for the whole observation, so demand is
#: reported unavailable rather than guessed.
RUNNABLE_STATUS = "waiting"
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
            "incompatible_labels": self.incompatible,
            "duplicate_records": self.duplicates,
            "notes": list(self.notes),
        }


class UrllibTransport:
    """Bounded HTTPS GET with redirects refused.

    Redirects are refused outright: the request carries an Authorization
    header, and following a cross-origin redirect would disclose it. TLS
    verification stays at the standard-library default and is never disabled.
    """

    def __init__(self, timeout_seconds, max_response_bytes):
        self._timeout = timeout_seconds
        self._limit = max_response_bytes

    def get(self, url, headers):
        request = urllib.request.Request(url, headers=headers, method="GET")
        try:
            with self._open(request) as response:
                status = response.status
                body = response.read(self._limit + 1)
        except urllib.error.HTTPError as error:
            raise ObservationRefusal("FORGEJO_UNEXPECTED_STATUS", f"HTTP {error.code}") from None
        except ObservationRefusal:
            raise
        except (urllib.error.URLError, TimeoutError, OSError) as error:
            raise ObservationRefusal("FORGEJO_UNAVAILABLE", type(error).__name__) from None
        if len(body) > self._limit:
            raise ObservationRefusal("FORGEJO_RESPONSE_TOO_LARGE", f"limit {self._limit} bytes")
        if status != 200:
            raise ObservationRefusal("FORGEJO_UNEXPECTED_STATUS", f"HTTP {status}")
        return body

    def _open(self, request):
        """Open one request; tests replace this boundary with a fake response."""
        return urllib.request.build_opener(_RefuseRedirects()).open(request, timeout=self._timeout)


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
        owner, repository = self._config.forgejo.repository.split("/", 1)
        return f"{self._config.forgejo.base_url}/api/v1/repos/{owner}/{repository}/actions/runners/jobs"

    def observe(self):
        token = self._environ.get(self._config.forgejo.token_env)
        if not token:
            raise ObservationRefusal("FORGEJO_TOKEN_MISSING", self._config.forgejo.token_env)
        body = self._transport.get(
            self.endpoint(),
            {
                "Authorization": f"token {token}",
                "Accept": "application/json",
                "User-Agent": "ci-burst/0.1",
            },
        )
        records = _parse_records(body)
        return _reduce(records, self._worker_label_names())

    def _worker_label_names(self):
        names = set()
        for label in self._config.worker.labels:
            names.add(label)
            names.add(label.split(":", 1)[0])
        return frozenset(names)


_IDENTIFIER = re.compile(r"^[A-Za-z0-9._:-]{1,128}$")


def _safe_identifier(value):
    """Echo an opaque provider identifier only when its shape is bounded."""
    return value if _IDENTIFIER.fullmatch(value) else "<redacted>"


def _parse_records(body):
    try:
        payload = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise ObservationRefusal("FORGEJO_INVALID_JSON") from None
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
    # A Go nil slice marshals as null and means "no dependencies". An absent
    # key is different: dependency metadata is unknown, so demand is not
    # established. Neither may be confused with an unread empty queue.
    if needs is not None and (
        not isinstance(needs, list) or any(not isinstance(need, str) for need in needs)
    ):
        raise ObservationRefusal("FORGEJO_UNEXPECTED_SHAPE", f"{where}.needs must be an array of strings")
    if status != RUNNABLE_STATUS and status not in ASSIGNED_OR_TERMINAL_STATUSES:
        raise ObservationRefusal("FORGEJO_UNKNOWN_STATUS", "unrecognized job status")
    return handle, job_id, attempt, status, tuple(runs_on), tuple(needs or ())


def _reduce(records, worker_labels):
    """Deduplicate attempts and keep only runnable, label-compatible demand."""
    attempts = []
    seen = {}
    blocked = assigned = incompatible = duplicates = 0
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
        if status != RUNNABLE_STATUS:
            assigned += 1
            continue
        if needs:
            blocked += 1
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
        notes=tuple(notes),
    )
