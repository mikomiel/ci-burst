import json
import tempfile
import unittest
import urllib.parse
from pathlib import Path

from ci_burst.config import load
from ci_burst.errors import ObservationRefusal
from ci_burst.forgejo import ForgejoObserver, UrllibTransport, _RefuseRedirects

from support import write_config

FIXTURES = Path(__file__).parent / "fixtures" / "forgejo"


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


if __name__ == "__main__":
    unittest.main()
