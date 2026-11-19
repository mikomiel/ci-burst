import http.client
import itertools
import json
import os
import socket
import ssl
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.parse
from pathlib import Path
from unittest import mock

from ci_burst.config import load
from ci_burst.errors import DeadlineRefusal, ObservationRefusal
from ci_burst.forgejo import (
    _WATCHDOG_THREAD,
    ForgejoObserver,
    ForgejoSource,
    UrllibTransport,
    _RefuseRedirects,
    _Watchdog,
    _WatchedHTTPSHandler,
    _WatchedRequest,
    require_protected,
)

from support import write_config

FIXTURES = Path(__file__).parent / "fixtures" / "forgejo"
CONTRACT_MANIFEST = FIXTURES / "contract.json"
RUNNERS_FIXTURE = FIXTURES.parent / "forgejo-runners.json"


class StubTransport:
    def __init__(self, body=b"", error=None):
        self.body = body
        self.error = error
        self.calls = []

    def get(self, url, headers):
        self.calls.append((url, dict(headers)))
        if self.error is not None:
            raise self.error
        return self.body


def fixture(name):
    return (FIXTURES / name).read_bytes()


class FakeResponse:
    def __init__(self, status, body):
        self.status = status
        self.body = body

    def read(self, limit):
        return self.body[:limit]

    def __enter__(self):
        return self

    def __exit__(self, *arguments):
        return False


class StubUrllibTransport(UrllibTransport):
    def __init__(self, status, body, limit=1024):
        super().__init__(timeout_seconds=1, max_response_bytes=limit)
        self._response = FakeResponse(status, body)

    def _open(self, request):
        return self._response


class TransportTests(unittest.TestCase):
    def test_response_above_the_bound_is_refused(self):
        transport = StubUrllibTransport(200, b"x" * 32, limit=16)
        with self.assertRaises(ObservationRefusal) as raised:
            transport.get("https://forgejo.example/x", {})
        self.assertEqual(raised.exception.reason, "FORGEJO_RESPONSE_TOO_LARGE")

    def test_non_200_is_refused(self):
        transport = StubUrllibTransport(503, b"{}")
        with self.assertRaises(ObservationRefusal) as raised:
            transport.get("https://forgejo.example/x", {})
        self.assertEqual(raised.exception.reason, "FORGEJO_UNEXPECTED_STATUS")

    def test_url_carries_no_token(self):
        transport = StubUrllibTransport(200, b"[]")
        self.assertEqual(
            transport.get("https://forgejo.example/api", {"Authorization": "token secret"}),
            b"[]",
        )


class ObserverTests(unittest.TestCase):
    def setUp(self):
        self._directory = tempfile.TemporaryDirectory()
        self.addCleanup(self._directory.cleanup)
        self.root = Path(self._directory.name)
        self.config = load(write_config(self.root))

    def observe(self, body, token="secret-token"):
        transport = StubTransport(body=body)
        observer = ForgejoObserver(
            self.config, transport=transport, environ={"CI_BURST_TEST_TOKEN": token} if token else {}
        )
        return observer.observe(), transport

    def runners(self, body, token="secret-token"):
        transport = StubTransport(body=body)
        observer = ForgejoObserver(
            self.config, transport=transport, environ={"CI_BURST_TEST_TOKEN": token} if token else {}
        )
        return observer.runners(), transport

    def test_registration_fixture_is_counted_by_status_and_ephemerality(self):
        snapshot, transport = self.runners(RUNNERS_FIXTURE.read_bytes())
        self.assertEqual(snapshot.contract, "forgejo-17.0.1-runners")
        self.assertEqual((snapshot.total, snapshot.active, snapshot.offline), (3, 2, 1))
        self.assertEqual(
            (snapshot.ephemeral_total, snapshot.ephemeral_active, snapshot.ephemeral_offline),
            (2, 1, 1),
        )
        url, headers = transport.calls[0]
        self.assertEqual(urllib.parse.urlsplit(url).path, "/api/v1/repos/owner/repo/actions/runners")
        self.assertEqual(headers["Authorization"], "token secret-token")

    def test_null_registration_list_is_a_verified_empty_fleet(self):
        snapshot, _ = self.runners(b"null")
        self.assertEqual(snapshot.total, 0)
        self.assertEqual(snapshot.ephemeral_active, 0)

    def test_running_attempt_is_fleet_context_not_demand(self):
        observation, _ = self.observe(fixture("jobs_mixed_waiting_running.json"))
        self.assertEqual(observation.demand, 1)
        self.assertEqual(observation.running, 1)
        self.assertEqual(observation.assigned, 1)

    def test_registration_shape_errors_are_refused(self):
        cases = [
            (b"not-json", "FORGEJO_RUNNERS_INVALID_JSON"),
            (b'{"name": "x"}', "FORGEJO_RUNNERS_UNEXPECTED_SHAPE"),
            (b'[{"name": "x", "status": "busy", "labels": [], "ephemeral": true}]', "FORGEJO_RUNNERS_UNKNOWN_STATUS"),
            (b'[{"name": "x", "status": [], "labels": [], "ephemeral": true}]', "FORGEJO_RUNNERS_UNKNOWN_STATUS"),
            (b'[{"name": "x", "status": {}, "labels": [], "ephemeral": true}]', "FORGEJO_RUNNERS_UNKNOWN_STATUS"),
            (b'[{"name": "x", "status": "active", "labels": [], "ephemeral": "yes"}]', "FORGEJO_RUNNERS_UNEXPECTED_SHAPE"),
            (b'[{"name": "", "status": "active", "labels": [], "ephemeral": true}]', "FORGEJO_RUNNERS_UNEXPECTED_SHAPE"),
            (b'[{"name": "x", "status": "active", "labels": [1], "ephemeral": true}]', "FORGEJO_RUNNERS_UNEXPECTED_SHAPE"),
        ]
        for body, reason in cases:
            with self.subTest(body=body):
                with self.assertRaises(ObservationRefusal) as raised:
                    self.runners(body)
                self.assertEqual(raised.exception.reason, reason)

    def test_missing_token_refuses_registration_read(self):
        with self.assertRaises(ObservationRefusal) as raised:
            self.runners(RUNNERS_FIXTURE.read_bytes(), token=None)
        self.assertEqual(raised.exception.reason, "FORGEJO_TOKEN_MISSING")
        self.assertNotIn("secret", str(raised.exception))

    def test_runnable_fixture_contributes_demand(self):
        observation, transport = self.observe(fixture("jobs_runnable.json"))
        self.assertEqual(observation.demand, 1)
        self.assertEqual(observation.attempts[0].handle, "h-1-1")
        self.assertEqual(observation.contract, "forgejo-17.0.1-runners-jobs")
        url, headers = transport.calls[0]
        parsed = urllib.parse.urlsplit(url)
        self.assertEqual(parsed.path, "/api/v1/repos/owner/repo/actions/runners/jobs")
        self.assertEqual(headers["Authorization"], "token secret-token")

    def test_label_filter_is_mandatory_and_bare(self):
        # The adapter always sends the explicit filter. An absent `labels`
        # parameter is not a no-match in the v17.0.1 handler (it admits every
        # waiting or running job), and the configured executor suffix
        # (`name:host`) is not a job label and must not be sent.
        _, transport = self.observe(fixture("jobs_runnable.json"))
        query = urllib.parse.parse_qs(urllib.parse.urlsplit(transport.calls[0][0]).query)
        self.assertEqual(query, {"labels": ["ci-burst,homelab"]})

    def test_null_payload_is_a_verified_empty_queue(self):
        observation, _ = self.observe(b"null")
        self.assertEqual(observation.demand, 0)
        self.assertEqual(observation.status, "empty")
        self.assertEqual(observation.seen, 0)
        self.assertEqual(observation.notes, ())

    def test_empty_queue_is_verified_zero(self):
        observation, _ = self.observe(fixture("jobs_empty.json"))
        self.assertEqual(observation.demand, 0)
        self.assertEqual(observation.status, "empty")

    def test_dependency_blocked_is_not_demand(self):
        # Dependency-blocked work carries its own status. A `waiting` job is
        # not blocked by a populated `needs`: the server fills that list from
        # the workflow and does not clear it when the dependency finishes.
        observation, _ = self.observe(fixture("jobs_blocked.json"))
        self.assertEqual(observation.demand, 0)
        self.assertEqual(observation.blocked, 1)
        self.assertEqual(observation.notes, ())

    def test_the_shared_pool_label_makes_fleet_jobs_demand(self):
        # The deployment advertises its routing label, the heavy pool's label and
        # the architecture: a strong-ci job is demand for cloud capacity, and so
        # is one that also names the architecture.
        config = load(
            write_config(
                self.root,
                config={
                    "worker": {
                        "labels": ["cloud-ci:host", "strong-ci:host", "linux-arm64:host"]
                    }
                },
            )
        )

        def observe(runs_on):
            body = json.dumps(
                [
                    {
                        "id": 7,
                        "attempt": 1,
                        "handle": "h-7-1",
                        "status": "waiting",
                        "runs_on": runs_on,
                        "needs": [],
                    }
                ]
            ).encode()
            observer = ForgejoObserver(
                config, transport=StubTransport(body=body), environ={"CI_BURST_TEST_TOKEN": "secret"}
            )
            return observer.observe()

        with self.subTest(runs_on="strong-ci alone"):
            observation = observe(["strong-ci"])
            self.assertEqual(observation.demand, 1)
            self.assertEqual(observation.incompatible, 0)

        with self.subTest(runs_on="strong-ci plus the architecture"):
            observation = observe(["strong-ci", "linux-arm64"])
            self.assertEqual(observation.demand, 1)

        with self.subTest(runs_on="a label the worker does not advertise"):
            observation = observe(["strong-ci", "linux-x86_64"])
            self.assertEqual(observation.demand, 0)
            self.assertEqual(observation.incompatible, 1)

    def test_waiting_job_with_static_needs_is_demand(self):
        # Measured on forgejo 17.0.1+gitea-1.22.0: a job waiting for a runner
        # still lists the dependency that already succeeded, so `needs` is
        # metadata and never decides eligibility.
        observation, _ = self.observe(fixture("jobs_waiting_with_needs.json"))
        self.assertEqual(observation.demand, 1)
        self.assertEqual(observation.blocked, 0)
        attempt = observation.attempts[0]
        self.assertEqual(attempt.handle, "h-3-1")
        self.assertEqual(attempt.needs, ("classify",))

    def test_assigned_job_is_not_demand(self):
        observation, _ = self.observe(fixture("jobs_assigned.json"))
        self.assertEqual(observation.demand, 0)
        self.assertEqual(observation.assigned, 1)

    def test_incompatible_label_is_not_demand(self):
        observation, _ = self.observe(fixture("jobs_incompatible.json"))
        self.assertEqual(observation.demand, 0)
        self.assertEqual(observation.incompatible, 1)

    def test_prefiltered_job_with_an_extra_label_is_incompatible(self):
        # The server filter is a union over the job's labels, so a job selected
        # by one shared label must still be rejected when its complete label set
        # requires something the worker does not advertise.
        record = {
            "id": 7,
            "run_id": 1,
            "attempt": 1,
            "handle": "h-7-1",
            "status": "waiting",
            "runs_on": ["homelab", "linux-arm64"],
            "needs": [],
        }
        observation, _ = self.observe(json.dumps([record]).encode())
        self.assertEqual(observation.demand, 0)
        self.assertEqual(observation.incompatible, 1)

    def test_job_requiring_the_routing_and_architecture_labels_is_demand(self):
        # The burst worker advertises the routing label and the image's
        # architecture label; a job asking for both bare labels is eligible.
        self.config = load(
            write_config(
                self.root,
                config={"worker": {"labels": ["cloud-ci:host", "linux-arm64:host"]}},
            )
        )
        record = {
            "id": 8,
            "run_id": 1,
            "attempt": 1,
            "handle": "h-8-1",
            "status": "waiting",
            "runs_on": ["cloud-ci", "linux-arm64"],
            "needs": [],
        }
        observation, transport = self.observe(json.dumps([record]).encode())
        self.assertEqual(observation.demand, 1)
        self.assertEqual(observation.incompatible, 0)
        query = urllib.parse.parse_qs(urllib.parse.urlsplit(transport.calls[0][0]).query)
        self.assertEqual(query, {"labels": ["cloud-ci,linux-arm64"]})

    def test_repeated_record_is_deduplicated(self):
        observation, _ = self.observe(fixture("jobs_duplicate.json"))
        self.assertEqual(observation.demand, 1)
        self.assertEqual(observation.duplicates, 1)
        self.assertEqual(observation.seen, 2)

    def test_retry_keeps_only_the_waiting_attempt(self):
        observation, _ = self.observe(fixture("jobs_retry.json"))
        self.assertEqual(observation.demand, 1)
        self.assertEqual(observation.attempts[0].handle, "h-6-2")
        self.assertEqual(observation.assigned, 1)

    def test_job_without_labels_is_not_demand(self):
        body = json.dumps(
            [
                {
                    "id": 30,
                    "attempt": 1,
                    "handle": "h-30",
                    "status": "waiting",
                    "runs_on": [],
                    "needs": [],
                }
            ]
        ).encode()
        observation, _ = self.observe(body)
        self.assertEqual(observation.demand, 0)
        self.assertEqual(observation.incompatible, 1)
        self.assertIn("job_without_labels", observation.notes)

    def test_missing_dependency_metadata_is_unavailable(self):
        body = json.dumps(
            [{"id": 40, "attempt": 1, "handle": "h-40", "status": "waiting", "runs_on": ["homelab"]}]
        ).encode()
        with self.assertRaises(ObservationRefusal) as raised:
            self.observe(body)
        self.assertEqual(raised.exception.reason, "FORGEJO_UNEXPECTED_SHAPE")

    def test_null_dependency_metadata_means_no_dependencies(self):
        body = json.dumps(
            [
                {
                    "id": 41,
                    "attempt": 1,
                    "handle": "h-41",
                    "status": "waiting",
                    "runs_on": ["homelab"],
                    "needs": None,
                }
            ]
        ).encode()
        observation, _ = self.observe(body)
        self.assertEqual(observation.demand, 1)

    def test_hostile_handle_is_not_echoed_in_the_plan(self):
        body = json.dumps(
            [
                {
                    "id": 43,
                    "attempt": 1,
                    "handle": "tok en/with spaces",
                    "status": "waiting",
                    "runs_on": ["homelab"],
                    "needs": [],
                }
            ]
        ).encode()
        observation, _ = self.observe(body)
        self.assertEqual(observation.demand, 1)
        self.assertEqual(observation.as_dict()["demand_attempts"], ["<redacted>"])

    def test_untrusted_values_are_not_echoed_in_refusals(self):
        marker = "synthetic-secret-token"
        body = json.dumps(
            [{"id": 42, "attempt": 1, "handle": "h-42", "status": marker, "runs_on": ["homelab"], "needs": []}]
        ).encode()
        with self.assertRaises(ObservationRefusal) as raised:
            self.observe(body)
        self.assertEqual(raised.exception.reason, "FORGEJO_UNKNOWN_STATUS")
        self.assertNotIn(marker, json.dumps(raised.exception.as_dict()))

    def test_unknown_status_makes_demand_unavailable(self):
        with self.assertRaises(ObservationRefusal) as raised:
            self.observe(fixture("jobs_unknown_status.json"))
        self.assertEqual(raised.exception.reason, "FORGEJO_UNKNOWN_STATUS")

    def test_sparse_record_is_an_unexpected_shape(self):
        with self.assertRaises(ObservationRefusal) as raised:
            self.observe(fixture("jobs_sparse.json"))
        self.assertEqual(raised.exception.reason, "FORGEJO_UNEXPECTED_SHAPE")

    def test_inconsistent_duplicate_attempt_is_refused(self):
        with self.assertRaises(ObservationRefusal) as raised:
            self.observe(fixture("jobs_inconsistent_attempt.json"))
        self.assertEqual(raised.exception.reason, "FORGEJO_INCONSISTENT_ATTEMPT")
        self.assertNotIn("h-9-1", json.dumps(raised.exception.as_dict()))

    def test_invalid_json_is_refused(self):
        with self.assertRaises(ObservationRefusal) as raised:
            self.observe(b"not json")
        self.assertEqual(raised.exception.reason, "FORGEJO_INVALID_JSON")

    def test_non_array_payload_is_refused(self):
        with self.assertRaises(ObservationRefusal) as raised:
            self.observe(b'{"jobs": []}')
        self.assertEqual(raised.exception.reason, "FORGEJO_UNEXPECTED_SHAPE")

    def test_missing_token_is_refused_and_not_echoed(self):
        transport = StubTransport(body=fixture("jobs_empty.json"))
        observer = ForgejoObserver(self.config, transport=transport, environ={})
        with self.assertRaises(ObservationRefusal) as raised:
            observer.observe()
        self.assertEqual(raised.exception.reason, "FORGEJO_TOKEN_MISSING")
        self.assertEqual(raised.exception.detail, "CI_BURST_TEST_TOKEN")
        self.assertEqual(transport.calls, [])

    def test_cloud_refusal_is_not_echoed_as_empty(self):
        transport = StubTransport(error=ObservationRefusal("FORGEJO_UNAVAILABLE", "TimeoutError"))
        observer = ForgejoObserver(
            self.config, transport=transport, environ={"CI_BURST_TEST_TOKEN": "secret-token"}
        )
        with self.assertRaises(ObservationRefusal):
            observer.observe()

    def test_redirects_are_refused_outright(self):
        with self.assertRaises(ObservationRefusal) as raised:
            _RefuseRedirects().redirect_request(None, None, 302, "Found", {}, "https://elsewhere.example")
        self.assertEqual(raised.exception.reason, "FORGEJO_REDIRECT_REFUSED")

    def test_observation_dict_has_no_secret(self):
        observation, _ = self.observe(fixture("jobs_runnable.json"))
        self.assertNotIn("secret-token", json.dumps(observation.as_dict()))


class ForgejoContractMatrixTests(unittest.TestCase):
    """Replay the pinned contract reading; no server, token or credential."""

    def setUp(self):
        self._directory = tempfile.TemporaryDirectory()
        self.addCleanup(self._directory.cleanup)
        self.root = Path(self._directory.name)
        self.config = load(write_config(self.root))
        self.manifest = json.loads(CONTRACT_MANIFEST.read_text(encoding="utf-8"))

    def observe(self, body):
        transport = StubTransport(body=body)
        observer = ForgejoObserver(
            self.config, transport=transport, environ={"CI_BURST_TEST_TOKEN": "secret-token"}
        )
        return observer.observe(), transport

    def test_the_manifest_is_a_pinned_contract_reading(self):
        manifest = self.manifest
        self.assertEqual(manifest["manifest_version"], 1)
        self.assertEqual(manifest["kind"], "contract-reading")
        self.assertIn("not a live observation", manifest["statement"])
        self.assertEqual(manifest["contract"], "forgejo-17.0.1-runners-jobs")
        self.assertEqual(manifest["upstream"]["project"], "forgejo")
        self.assertEqual(manifest["upstream"]["tag"], "v17.0.1")
        self.assertEqual(manifest["upstream"]["source_path"], "routers/api/v1/shared/runners.go")
        self.assertEqual(manifest["upstream"]["handler"], "GetActionRunJobs")
        self.assertEqual(
            manifest["adapter_filter"],
            ",".join(sorted({label.split(":", 1)[0] for label in self.config.worker.labels})),
        )
        legend = manifest["provenance_legend"]
        self.assertEqual(set(legend), {"upstream-shape", "defensive-synthetic"})
        for entry in manifest["fixtures"]:
            with self.subTest(fixture=entry["fixture"]):
                self.assertIn(entry["provenance"], legend)
                self.assertTrue(entry["pins"])
                self.assertIn(entry["expect"]["outcome"], {"observation", "refusal"})
                variant = entry["request_variant"]
                if variant["labels_parameter"] == "absent":
                    self.assertNotIn("labels", variant)
                else:
                    self.assertEqual(variant["labels_parameter"], "explicit")
                    self.assertTrue(variant["labels"])

    def test_the_manifest_covers_every_fixture_once(self):
        pinned = [entry["fixture"] for entry in self.manifest["fixtures"]]
        self.assertEqual(len(pinned), len(set(pinned)))
        self.assertEqual(
            set(pinned),
            {path.name for path in FIXTURES.glob("*.json")} - {CONTRACT_MANIFEST.name},
        )

    def test_every_fixture_classifies_as_the_manifest_expects(self):
        for entry in self.manifest["fixtures"]:
            with self.subTest(fixture=entry["fixture"]):
                expect = entry["expect"]
                if expect["outcome"] == "refusal":
                    with self.assertRaises(ObservationRefusal) as raised:
                        self.observe(fixture(entry["fixture"]))
                    self.assertEqual(raised.exception.reason, expect["reason"])
                    continue
                observation, transport = self.observe(fixture(entry["fixture"]))
                self.assertEqual(observation.contract, self.manifest["contract"])
                self.assertEqual(observation.demand, expect["demand"])
                self.assertEqual(
                    [attempt.handle for attempt in observation.attempts], expect["attempts"]
                )
                self.assertEqual(observation.seen, expect["seen"])
                self.assertEqual(observation.blocked, expect["blocked"])
                self.assertEqual(observation.assigned, expect["assigned_or_finished"])
                self.assertEqual(observation.incompatible, expect["incompatible_labels"])
                self.assertEqual(observation.duplicates, expect["duplicate_records"])
                self.assertEqual(list(observation.notes), expect["notes"])
                self.assertEqual(observation.status, "demand" if expect["demand"] else "empty")
                query = urllib.parse.parse_qs(urllib.parse.urlsplit(transport.calls[0][0]).query)
                self.assertEqual(query.get("labels"), [self.manifest["adapter_filter"]])

    def test_upstream_shapes_match_the_pinned_handler(self):
        # The pinned handler selects only waiting and running jobs, and its
        # response record always marshals `needs` and `runs_on`, possibly null.
        for entry in self.manifest["fixtures"]:
            if entry["provenance"] != "upstream-shape":
                continue
            with self.subTest(fixture=entry["fixture"]):
                payload = json.loads(fixture(entry["fixture"]))
                if payload is None:
                    continue
                self.assertIsInstance(payload, list)
                self.assertTrue(payload)
                for record in payload:
                    self.assertIn(record["status"], {"waiting", "running"})
                    self.assertIn("needs", record)
                    self.assertIn("runs_on", record)

    def test_the_absent_filter_shape_is_unreachable(self):
        # Source: an absent `labels` parameter applies no filter at all, so the
        # response can list every waiting and running job in the repository.
        observation, transport = self.observe(fixture("jobs_filter_absent.json"))
        self.assertEqual(observation.seen, 3)
        query = urllib.parse.parse_qs(
            urllib.parse.urlsplit(transport.calls[0][0]).query, keep_blank_values=True
        )
        self.assertEqual(query.get("labels"), [self.manifest["adapter_filter"]])
        self.assertTrue(query["labels"][0])

    def test_every_label_configuration_sends_a_non_empty_labels_parameter(self):
        for labels in (
            ["ci-burst:host", "homelab:host"],
            ["homelab:host"],
            ["homelab", "homelab:host"],
        ):
            with self.subTest(labels=labels):
                config = load(write_config(self.root, config={"worker": {"labels": labels}}))
                transport = StubTransport(body=b"null")
                observer = ForgejoObserver(
                    config, transport=transport, environ={"CI_BURST_TEST_TOKEN": "secret-token"}
                )
                observer.observe()
                query = urllib.parse.parse_qs(
                    urllib.parse.urlsplit(transport.calls[0][0]).query, keep_blank_values=True
                )
                self.assertTrue(query.get("labels"))
                self.assertNotEqual(query["labels"], [""])


SHA = "0123456789abcdef0123456789abcdef01234567"
TOKEN_ENV = "EXAMPLE_IMAGE_TOKEN"
APPLY_TOKEN_ENV = "CI_BURST_TEST_TOKEN"


class RecordingEnviron(dict):
    """Records every variable name that is read, to prove which ones are not."""

    def __init__(self, *arguments):
        super().__init__(*arguments)
        self.reads = []

    def get(self, key, default=None):
        self.reads.append(key)
        return super().get(key, default)

    def __getitem__(self, key):
        self.reads.append(key)
        return super().__getitem__(key)


class SourceTransport:
    """The fake transport of the source reads: a body, a stream or a refusal."""

    def __init__(self, body=b"", chunks=(), error=None):
        self.body = body
        self.chunks = chunks
        self.error = error
        self.calls = []
        self.streams = []
        self.deadlines = []
        self.pulled = 0

    def get(self, url, headers):
        self.calls.append((url, dict(headers)))
        if self.error is not None:
            raise self.error
        return self.body

    def stream(self, url, headers, max_bytes, deadline=None):
        self.streams.append((url, dict(headers), max_bytes))
        self.deadlines.append(deadline)
        if self.error is not None:
            raise self.error
        received = 0
        for chunk in self.chunks:
            self.pulled += 1
            received += len(chunk)
            if received > max_bytes:
                raise ObservationRefusal("FORGEJO_ARCHIVE_TOO_LARGE", f"limit {max_bytes} bytes")
            yield chunk


def status_refusal(code):
    return ObservationRefusal("FORGEJO_UNEXPECTED_STATUS", f"HTTP {code}")


def branch_body(sha=SHA, **extra):
    return json.dumps({"name": "main", "commit": {"id": sha}, **extra}).encode()


class ForgejoSourceTests(unittest.TestCase):
    def source(self, transport, environ=None):
        return ForgejoSource(
            "https://forgejo.example",
            "example-org/example-repo",
            TOKEN_ENV,
            transport=transport,
            environ={TOKEN_ENV: "image-secret"} if environ is None else environ,
        )

    def test_branch_returns_the_head_sha_and_the_protected_flag(self):
        transport = SourceTransport(body=branch_body(protected=True))
        head = self.source(transport).branch("main")
        self.assertEqual((head.sha, head.protected), (SHA, True))
        url, headers = transport.calls[0]
        self.assertEqual(urllib.parse.urlsplit(url).path, "/api/v1/repos/example-org/example-repo/branches/main")
        self.assertEqual(headers["Authorization"], "token image-secret")
        self.assertIs(require_protected(head), head)

    def test_unprotected_or_missing_flag_is_unprotected(self):
        for extra in ({"protected": False}, {}, {"protected": "true"}, {"protected": None}):
            with self.subTest(extra=extra):
                head = self.source(SourceTransport(body=branch_body(**extra))).branch("main")
                with self.assertRaises(ObservationRefusal) as raised:
                    require_protected(head)
                self.assertEqual(raised.exception.reason, "IMAGE_BRANCH_UNPROTECTED")

    def test_branch_without_a_commit_sha_is_an_unexpected_shape(self):
        for body in (b"[]", b'{"protected": true}', branch_body(sha="main", protected=True), b"not json"):
            with self.subTest(body=body):
                with self.assertRaises(ObservationRefusal) as raised:
                    self.source(SourceTransport(body=body)).branch("main")
                self.assertIn(raised.exception.reason, {"FORGEJO_UNEXPECTED_SHAPE", "FORGEJO_INVALID_JSON"})

    def test_unauthorized_and_forbidden_are_source_forbidden_on_every_read(self):
        for code in (401, 403):
            reads = {
                "branch": lambda source: source.branch("main"),
                "raw": lambda source: source.raw_file(SHA, "uv.lock"),
                "archive": lambda source: list(source.archive(SHA, 1024)),
            }
            for name, read in reads.items():
                with self.subTest(code=code, read=name):
                    transport = SourceTransport(error=status_refusal(code))
                    with self.assertRaises(ObservationRefusal) as raised:
                        read(self.source(transport))
                    self.assertEqual(raised.exception.reason, "IMAGE_SOURCE_FORBIDDEN")
                    self.assertNotIn("image-secret", repr(raised.exception.as_dict()))

    def test_other_statuses_keep_their_own_refusal(self):
        with self.assertRaises(ObservationRefusal) as raised:
            self.source(SourceTransport(error=status_refusal(500))).branch("main")
        self.assertEqual(raised.exception.reason, "FORGEJO_UNEXPECTED_STATUS")

    def test_raw_file_is_read_at_the_sha_and_returns_the_bytes(self):
        transport = SourceTransport(body=b"lock-bytes")
        self.assertEqual(self.source(transport).raw_file(SHA, "docs/uv.lock"), b"lock-bytes")
        url, _ = transport.calls[0]
        parts = urllib.parse.urlsplit(url)
        self.assertEqual(parts.path, "/api/v1/repos/example-org/example-repo/raw/docs/uv.lock")
        self.assertEqual(urllib.parse.parse_qs(parts.query), {"ref": [SHA]})

    def test_missing_raw_path_is_a_basis_path_refusal(self):
        with self.assertRaises(ObservationRefusal) as raised:
            self.source(SourceTransport(error=status_refusal(404))).raw_file(SHA, "uv.lock")
        self.assertEqual(raised.exception.reason, "IMAGE_BASIS_PATH_MISSING")

    def test_raw_path_cannot_escape_and_ref_must_be_a_sha(self):
        transport = SourceTransport(body=b"x")
        source = self.source(transport)
        for sha, path in ((SHA, "../secrets"), (SHA, "/etc/passwd"), (SHA, "a/../b"), ("main", "uv.lock")):
            with self.subTest(sha=sha, path=path):
                with self.assertRaises(ObservationRefusal):
                    source.raw_file(sha, path)
        self.assertEqual(transport.calls, [])

    def test_branch_name_cannot_escape_the_branches_path(self):
        transport = SourceTransport(body=branch_body(protected=True))
        source = self.source(transport)
        for name in ("", "/main", "../../x", "a/../b", ".."):
            with self.subTest(name=name):
                with self.assertRaises(ObservationRefusal):
                    source.branch(name)
        self.assertEqual(transport.calls, [])
        source.branch("release/1.x")
        self.assertEqual(len(transport.calls), 1)

    def test_archive_passes_the_deadline_to_the_transport(self):
        transport = SourceTransport(chunks=[b"a"])
        self.assertEqual(list(self.source(transport).archive(SHA, 16, deadline=42.0)), [b"a"])
        list(self.source(transport).archive(SHA, 16))
        self.assertEqual(transport.deadlines, [42.0, None])

    def test_archive_is_requested_by_sha_never_by_branch(self):
        transport = SourceTransport(chunks=[b"abc", b"def"])
        source = self.source(transport)
        self.assertEqual(b"".join(source.archive(SHA, 1024)), b"abcdef")
        url, headers, cap = transport.streams[0]
        self.assertEqual(
            urllib.parse.urlsplit(url).path, f"/api/v1/repos/example-org/example-repo/archive/{SHA}.tar.gz"
        )
        self.assertEqual((headers["Authorization"], cap), ("token image-secret", 1024))
        for ref in ("main", "refs/pull/1/head", SHA[:12]):
            with self.subTest(ref=ref):
                with self.assertRaises(ObservationRefusal):
                    list(source.archive(ref, 1024))
        self.assertEqual(len(transport.streams), 1)

    def test_archive_over_the_cap_aborts_the_stream(self):
        transport = SourceTransport(chunks=[b"x" * 10] * 5)
        with self.assertRaises(ObservationRefusal) as raised:
            list(self.source(transport).archive(SHA, 25))
        self.assertEqual(raised.exception.reason, "FORGEJO_ARCHIVE_TOO_LARGE")
        self.assertEqual(transport.pulled, 3)

    def test_only_the_named_token_variable_is_read(self):
        environ = RecordingEnviron({TOKEN_ENV: "image-secret", APPLY_TOKEN_ENV: "apply-secret"})
        transport = SourceTransport(body=branch_body(protected=True), chunks=[b"a"])
        source = self.source(transport, environ=environ)
        source.branch("main")
        source.raw_file(SHA, "uv.lock")
        list(source.archive(SHA, 16))
        self.assertEqual(set(environ.reads), {TOKEN_ENV})
        for _, headers in transport.calls:
            self.assertNotIn("apply-secret", headers["Authorization"])

    def test_absent_image_token_refuses_without_falling_back_to_the_apply_token(self):
        environ = RecordingEnviron({APPLY_TOKEN_ENV: "apply-secret"})
        transport = SourceTransport(body=branch_body(protected=True))
        with self.assertRaises(ObservationRefusal) as raised:
            self.source(transport, environ=environ).branch("main")
        self.assertEqual((raised.exception.reason, raised.exception.detail), ("FORGEJO_TOKEN_MISSING", TOKEN_ENV))
        self.assertEqual(environ.reads, [TOKEN_ENV])
        self.assertEqual(transport.calls, [])

    def test_a_malformed_token_is_refused_without_its_value_on_every_read(self):
        reads = {
            "branch": lambda source: source.branch("main"),
            "raw_file": lambda source: source.raw_file(SHA, "uv.lock"),
            "archive": lambda source: list(source.archive(SHA, 16)),
        }
        malformed = (
            "sentinel-image-secret\n",
            "sentinel image secret",
            "sentinel-image-secret\r\nX-Injected: 1",
            "sentinel-image-secret\tsentinel",  # a tab is not printable ASCII
            "sentinel-image-secret\x7f",  # DEL
            "sentinel-image-secret-é",  # a character outside ASCII
        )
        for token in malformed:
            for name, read in reads.items():
                with self.subTest(read=name, token=repr(token)):
                    transport = SourceTransport(body=branch_body(protected=True), chunks=[b"a"])
                    source = self.source(transport, environ=RecordingEnviron({TOKEN_ENV: token}))
                    with self.assertRaises(ObservationRefusal) as raised:
                        read(source)
                    refusal = raised.exception
                    self.assertEqual((refusal.reason, refusal.detail), ("FORGEJO_TOKEN_INVALID", TOKEN_ENV))
                    self.assertEqual(transport.calls, [])
                    for text in (str(refusal), repr(refusal), json.dumps(refusal.as_dict())):
                        self.assertNotIn("sentinel", text)


class StreamingFakeResponse(FakeResponse):
    def __init__(self, status, body):
        super().__init__(status, body)
        self.offset = 0
        self.closed = False

    def read1(self, limit):
        chunk = self.body[self.offset : self.offset + limit]
        self.offset += len(chunk)
        return chunk

    def read(self, limit):
        raise AssertionError("the stream reads with read1, one receive at a time")

    def __exit__(self, *arguments):
        self.closed = True
        return False


class StreamTransportTests(unittest.TestCase):
    def transport(self, status, body):
        transport = UrllibTransport(timeout_seconds=1, max_response_bytes=16)
        response = StreamingFakeResponse(status, body)
        transport._open = lambda request, timeout=None: response
        return transport, response

    def test_stream_yields_the_body_within_the_cap(self):
        transport, response = self.transport(200, b"x" * 100)
        chunks = list(transport.stream("https://forgejo.example/a", {}, 100, chunk_bytes=30))
        self.assertEqual(b"".join(chunks), b"x" * 100)
        self.assertTrue(response.closed)

    def test_stream_over_the_cap_stops_reading_and_closes_the_connection(self):
        transport, response = self.transport(200, b"x" * 1000)
        with self.assertRaises(ObservationRefusal) as raised:
            list(transport.stream("https://forgejo.example/a", {}, 100, chunk_bytes=30))
        self.assertEqual(raised.exception.reason, "FORGEJO_ARCHIVE_TOO_LARGE")
        self.assertTrue(response.closed)
        self.assertLessEqual(response.offset, 101)

    def test_stream_refuses_a_non_200(self):
        transport, _ = self.transport(503, b"")
        with self.assertRaises(ObservationRefusal) as raised:
            list(transport.stream("https://forgejo.example/a", {}, 100))
        self.assertEqual((raised.exception.reason, raised.exception.detail), ("FORGEJO_UNEXPECTED_STATUS", "HTTP 503"))

    def test_stream_keeps_the_http_status_of_an_error_response(self):
        transport = UrllibTransport(timeout_seconds=1, max_response_bytes=16)

        def refuse(request, timeout=None):
            raise urllib.error.HTTPError(request.full_url, 403, "Forbidden", {}, None)

        transport._open = refuse
        with self.assertRaises(ObservationRefusal) as raised:
            list(transport.stream("https://forgejo.example/a", {}, 100))
        self.assertEqual((raised.exception.reason, raised.exception.detail), ("FORGEJO_UNEXPECTED_STATUS", "HTTP 403"))


    def test_stream_stops_between_chunks_once_the_deadline_passes_and_closes_the_connection(self):
        # The first tick sets the socket timeout; the rest are checked before each read.
        ticks = iter([0, 0, 1, 2, 3, 4, 5])
        transport = UrllibTransport(timeout_seconds=1, max_response_bytes=16, clock=lambda: next(ticks))
        response = StreamingFakeResponse(200, b"x" * 1000)
        transport._open = lambda request, timeout=None: response
        received = []
        with self.assertRaises(DeadlineRefusal) as raised:
            for chunk in transport.stream("https://forgejo.example/a", {}, 1000, chunk_bytes=10, deadline=3):
                received.append(chunk)
        self.assertEqual(raised.exception.reason, "pass_deadline_exceeded")
        self.assertEqual(len(received), 3)
        self.assertEqual(response.offset, 30)
        self.assertTrue(response.closed)

    def test_stream_without_a_deadline_never_reads_the_clock(self):
        def clock():
            raise AssertionError("the clock is only read when a deadline is given")

        transport = UrllibTransport(timeout_seconds=1, max_response_bytes=16, clock=clock)
        transport._open = lambda request, timeout=None: StreamingFakeResponse(200, b"x" * 50)
        self.assertEqual(b"".join(transport.stream("https://forgejo.example/a", {}, 100, chunk_bytes=20)), b"x" * 50)


    def test_a_drip_feed_is_stopped_inside_one_chunk_once_the_deadline_passes(self):
        # Each receive returns one byte and costs one second. A read that filled
        # the whole 64 KiB chunk would run far past the deadline before returning.
        clock = {"now": 0}

        class DripResponse(StreamingFakeResponse):
            def read1(self, limit):
                clock["now"] += 1
                return super().read1(min(limit, 1))

        transport = UrllibTransport(timeout_seconds=30, max_response_bytes=16, clock=lambda: clock["now"])
        response = DripResponse(200, b"x" * 100000)
        opened = []

        def open_(request, timeout=None):
            opened.append(timeout)
            return response

        transport._open = open_
        with self.assertRaises(DeadlineRefusal):
            list(transport.stream("https://forgejo.example/a", {}, 1000000, deadline=5))
        self.assertEqual(response.offset, 5)
        self.assertEqual(opened, [5])
        self.assertTrue(response.closed)

    def test_a_deadline_already_passed_refuses_before_opening(self):
        transport = UrllibTransport(timeout_seconds=1, max_response_bytes=16, clock=lambda: 10)

        def open_(request, timeout=None):
            raise AssertionError("no request once the deadline has passed")

        transport._open = open_
        with self.assertRaises(DeadlineRefusal):
            list(transport.stream("https://forgejo.example/a", {}, 100, deadline=10))


CHUNKED_HEAD = b"HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\nConnection: close\r\n\r\n"
LENGTH_HEAD = b"HTTP/1.1 200 OK\r\nContent-Length: 1000\r\nConnection: close\r\n\r\n"


class LoopbackServer:
    """Serve one scripted connection on 127.0.0.1 so the real http.client parser reads it.

    ``script(connection, stop)`` runs once the request head has arrived. Every
    wait is bounded and ``close`` ends the thread, so a failing test leaves
    neither a blocked client nor a running thread behind.
    """

    def __init__(self, script):
        self._script = script
        self.stop = threading.Event()
        self._listener = socket.create_server(("127.0.0.1", 0))
        self._listener.settimeout(0.1)
        self.url = "http://127.0.0.1:%d/archive" % self._listener.getsockname()[1]
        self._thread = threading.Thread(target=self._serve, name="loopback-server", daemon=True)
        self._thread.start()

    def _serve(self):
        connection = None
        while connection is None:
            if self.stop.is_set():
                return
            try:
                connection, _ = self._listener.accept()
            except TimeoutError:
                continue
            except OSError:
                return
        with connection:
            connection.settimeout(5)
            try:
                head = b""
                while b"\r\n\r\n" not in head:
                    received = connection.recv(4096)
                    if not received:
                        return
                    head += received
                self._script(connection, self.stop)
            except OSError:
                pass  # the client went away: nothing is left to send

    def close(self):
        self.stop.set()
        self._thread.join(5)
        self._listener.close()


def drip(connection, stop, data, interval=0.05, limit=5.0):
    """Send ``data``, an iterable of byte values, one byte per ``interval`` seconds.

    It gives up at ``limit`` seconds, once ``stop`` is set, or when the peer is
    gone, so a regression that removes the cancellation ends the test late
    instead of hanging the suite.
    """
    give_up = time.monotonic() + limit
    for value in data:
        if stop.wait(interval) or time.monotonic() >= give_up:
            return
        connection.sendall(bytes([value]))


def live_watchdogs():
    return [thread for thread in threading.enumerate() if thread.name == _WATCHDOG_THREAD]


def settled_watchdogs():
    """The watchdog timers that are still alive once a cancelled one has had time to end."""
    for thread in live_watchdogs():
        thread.join(2)
    return live_watchdogs()


class StreamDeadlineTests(unittest.TestCase):
    """``deadline`` is absolute: a server that keeps sending cannot hold the stream past it.

    Each test reads a real loopback connection through the real opener and the
    real ``http.client`` parser. A blocked read is drained in a thread that the
    test waits for at most ``BOUND`` seconds.
    """

    DEADLINE = 0.5
    BOUND = 3.0

    def setUp(self):
        # The opener honours proxy variables; this traffic never leaves the host.
        patched = mock.patch.dict(os.environ, {"no_proxy": "127.0.0.1", "NO_PROXY": "127.0.0.1"})
        patched.start()
        self.addCleanup(patched.stop)
        self._servers = []
        self._workers = []
        self.addCleanup(self._release)

    def _release(self):
        for server in self._servers:
            server.close()
        for worker in self._workers:
            worker.join(5)

    def serve(self, script):
        server = LoopbackServer(script)
        self._servers.append(server)
        return server

    def transport(self):
        return UrllibTransport(timeout_seconds=1, max_response_bytes=1024, clock=time.monotonic)

    def assert_refused_at_the_deadline(self, script):
        server = self.serve(script)
        transport = self.transport()
        deadline = time.monotonic() + self.DEADLINE
        outcome = {}

        def read():
            try:
                outcome["chunks"] = list(transport.stream(server.url, {}, 1024, deadline=deadline))
            except Exception as error:  # the test thread judges it
                outcome["error"] = error

        worker = threading.Thread(target=read, name="stream-under-test", daemon=True)
        self._workers.append(worker)
        started = time.monotonic()
        worker.start()
        worker.join(self.BOUND)
        elapsed = time.monotonic() - started
        self.assertFalse(
            worker.is_alive(), "still blocked %.1f s after a %.1f s deadline" % (elapsed, self.DEADLINE)
        )
        refusal = outcome.get("error")
        self.assertIsInstance(refusal, DeadlineRefusal, msg=repr(outcome))
        self.assertEqual((refusal.reason, refusal.detail), ("pass_deadline_exceeded", "archive stream"))

    def test_a_chunk_extension_drip_is_cut_off_at_the_deadline(self):
        # `1;` opens a chunk-size line that never ends. http.client reads the
        # line with repeated receives, each one inside the socket timeout.
        def script(connection, stop):
            connection.sendall(CHUNKED_HEAD + b"1;")
            drip(connection, stop, itertools.repeat(ord("x")))

        self.assert_refused_at_the_deadline(script)

    def test_a_status_line_or_header_drip_is_cut_off_at_the_deadline(self):
        # The head never ends, so the drip holds the open itself.
        heads = {
            "status line": (b"", b"HTTP/1.1 200 OK"),
            "header": (b"HTTP/1.1 200 OK\r\n", b"X-Padding: "),
        }
        for name, (sent_at_once, dripped) in heads.items():
            with self.subTest(drip=name):

                def script(connection, stop, sent_at_once=sent_at_once, dripped=dripped):
                    connection.sendall(sent_at_once)
                    drip(connection, stop, itertools.chain(dripped, itertools.repeat(ord("x"))))

                self.assert_refused_at_the_deadline(script)

    def test_a_body_drip_is_cut_off_and_never_ends_as_a_complete_body(self):
        # A shut-down socket reads as a clean end of stream. The body promised
        # 1000 bytes and got a few, so that end must not look like success.
        def script(connection, stop):
            connection.sendall(LENGTH_HEAD)
            drip(connection, stop, itertools.repeat(ord("x")))

        self.assert_refused_at_the_deadline(script)

    def test_a_normal_chunked_response_is_yielded_whole_and_its_timer_is_cancelled(self):
        body = b"5\r\nhello\r\n6\r\n world\r\n0\r\n\r\n"
        server = self.serve(lambda connection, stop: connection.sendall(CHUNKED_HEAD + body))
        before = set(live_watchdogs())
        stream = self.transport().stream(server.url, {}, 1024, chunk_bytes=4, deadline=time.monotonic() + 60)
        first = next(stream)
        # The check can see a timer: one runs while the stream is open.
        self.assertTrue(set(live_watchdogs()) - before)
        self.assertEqual(b"".join([first, *stream]), b"hello world")
        self.assertEqual(settled_watchdogs(), [])

    def test_the_timer_is_cancelled_when_the_stream_fails_or_is_closed(self):
        def script(connection, stop):
            connection.sendall(CHUNKED_HEAD + b"5\r\nhello\r\n")
            stop.wait(5)

        with self.subTest(end="closed before the end"):
            stream = self.transport().stream(self.serve(script).url, {}, 1024, deadline=time.monotonic() + 60)
            self.assertEqual(next(stream), b"hello")
            stream.close()
            self.assertEqual(settled_watchdogs(), [])
        with self.subTest(end="over the cap"):
            stream = self.transport().stream(self.serve(script).url, {}, 3, deadline=time.monotonic() + 60)
            with self.assertRaises(ObservationRefusal) as raised:
                next(stream)
            self.assertEqual(raised.exception.reason, "FORGEJO_ARCHIVE_TOO_LARGE")
            self.assertEqual(settled_watchdogs(), [])

    def test_refusals_that_are_not_a_deadline_keep_their_reason_with_and_without_a_deadline(self):
        # The watched opener must refuse redirects (the request carries a token)
        # and report a failure before the deadline as it always did.
        redirect = b"HTTP/1.1 302 Found\r\nLocation: http://forgejo.example/elsewhere\r\nContent-Length: 0\r\n\r\n"
        forbidden = b"HTTP/1.1 403 Forbidden\r\nContent-Length: 0\r\nConnection: close\r\n\r\n"
        replies = {
            "redirect": (redirect, "FORGEJO_REDIRECT_REFUSED", "HTTP 302"),
            "forbidden": (forbidden, "FORGEJO_UNEXPECTED_STATUS", "HTTP 403"),
        }
        for name, (reply, reason, detail) in replies.items():
            for deadline in (None, time.monotonic() + 60):
                with self.subTest(reply=name, deadline=deadline is not None):
                    server = self.serve(lambda connection, stop, reply=reply: connection.sendall(reply))
                    with self.assertRaises(ObservationRefusal) as raised:
                        list(self.transport().stream(server.url, {}, 1024, deadline=deadline))
                    self.assertEqual((raised.exception.reason, raised.exception.detail), (reason, detail))
        with self.subTest(reply="connection refused"):
            listener = socket.create_server(("127.0.0.1", 0))
            url = "http://127.0.0.1:%d/archive" % listener.getsockname()[1]
            listener.close()
            with self.assertRaises(ObservationRefusal) as raised:
                list(self.transport().stream(url, {}, 1024, deadline=time.monotonic() + 60))
            self.assertEqual(raised.exception.reason, "FORGEJO_UNAVAILABLE")

    def test_a_stream_without_a_deadline_arms_no_timer(self):
        server = self.serve(lambda connection, stop: connection.sendall(CHUNKED_HEAD + b"5\r\nhello\r\n0\r\n\r\n"))
        before = set(live_watchdogs())
        stream = self.transport().stream(server.url, {}, 1024)
        self.assertEqual(next(stream), b"hello")
        self.assertEqual(set(live_watchdogs()), before)
        self.assertEqual(list(stream), [])


class RecordingSocket:
    def __init__(self, error=None):
        self.error = error
        self.shutdowns = []

    def shutdown(self, how):
        self.shutdowns.append(how)
        if self.error is not None:
            raise self.error


def fired_watchdog():
    watchdog = _Watchdog(0)
    watchdog.start()
    give_up = time.monotonic() + 2
    while not watchdog.fired and time.monotonic() < give_up:
        time.sleep(0.005)
    return watchdog


class WatchdogTests(unittest.TestCase):
    def test_a_socket_reported_after_the_time_is_up_is_shut_down_at_once(self):
        # The timer can fire before the connection exists; the connect hook then
        # does the cutting as soon as it reports the socket.
        watchdog = fired_watchdog()
        self.assertTrue(watchdog.fired)
        sock = RecordingSocket()
        watchdog.attach(sock)
        self.assertEqual(sock.shutdowns, [socket.SHUT_RDWR])
        watchdog.attach(RecordingSocket(error=OSError("already closed")))  # no error escapes

    def test_the_tls_handler_keeps_certificate_verification_and_reports_its_socket(self):
        watchdog = fired_watchdog()
        handler = _WatchedHTTPSHandler()
        opened = {}
        handler.do_open = lambda factory, request, **keywords: opened.update(factory=factory, keywords=keywords)
        handler.https_open(_WatchedRequest("https://forgejo.example/archive", watchdog=watchdog))
        context = opened["keywords"]["context"]
        self.assertEqual(context.verify_mode, ssl.CERT_REQUIRED)
        self.assertTrue(context.check_hostname)
        connection = opened["factory"]("forgejo.example", timeout=1, **opened["keywords"])
        self.assertIsInstance(connection, http.client.HTTPSConnection)
        connected = RecordingSocket()

        def connect(self):  # the plain connect and the handshake, without a network
            self.sock = connected

        with mock.patch.object(http.client.HTTPSConnection, "connect", connect):
            connection.connect()
        self.assertEqual(connected.shutdowns, [socket.SHUT_RDWR])


if __name__ == "__main__":
    unittest.main()
