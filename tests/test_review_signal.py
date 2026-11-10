"""The review relay: a review must reach the gate, and the merge, without waiting for a schedule.

The behaviour lives in three declarations across workflow files, so this test is structural: it pins the
relay's shape (a no-op that may run from a head), both `workflow_run` lists, and the schedules that remain the
backstop. The failure it guards is silent: drop the relay from either list and everything still works, hours
late, which looks like nothing is wrong.
"""

import unittest
from pathlib import Path

WORKFLOWS = Path(__file__).resolve().parents[1] / ".github" / "workflows"


def read(name: str) -> str:
    path = WORKFLOWS / name
    if not path.is_file():
        raise AssertionError(f"{name} is missing from .github/workflows")
    return path.read_text()


def workflow_run_workflows(text: str) -> list[str]:
    """The `workflows:` list under the top-level `workflow_run:` trigger, in order.

    Read line by line instead of importing a YAML parser: this repository has one runtime dependency (boto3)
    and the test suite must not add a second just to pin two lists.
    """
    lines = text.splitlines()
    for index, line in enumerate(lines):
        if line.strip() != "workflow_run:":
            continue
        indent = len(line) - len(line.lstrip())
        for following in lines[index + 1 :]:
            if not following.strip() or following.lstrip().startswith("#"):
                continue
            if len(following) - len(following.lstrip()) <= indent:
                break
            stripped = following.strip()
            if stripped.startswith("workflows:"):
                value = stripped.split(":", 1)[1].strip()
                if not (value.startswith("[") and value.endswith("]")):
                    raise AssertionError(f"workflows must be a one-line list, got {value!r}")
                return [item.strip() for item in value.strip("[]").split(",") if item.strip()]
    raise AssertionError("no workflow_run.workflows list found")


class ReviewSignalTests(unittest.TestCase):
    def test_the_relay_is_a_no_op_that_may_run_from_a_head(self) -> None:
        text = read("review-signal.yml")

        self.assertIn("pull_request_review:", text, "only a review event may start the relay")
        self.assertIn("types: [submitted, dismissed]", text)
        # This file runs the head's copy of itself, so its committed shape is the whole of its safety.
        self.assertIn("permissions: {}", text)
        self.assertNotIn("uses:", text, "a relay step may not call another action")
        self.assertNotIn("env:", text, "a top-level environment could carry a secret into the no-op")
        self.assertNotIn("${{", text, "an expression could read the pull request")

        # Startswith would also accept 'echo x; id' and 'echo "$(id)"', so pin the one command exactly.
        commands = [line.strip() for line in text.splitlines() if line.strip().startswith("run:")]
        self.assertEqual(
            ['run: echo "review event relayed to review-gate and automerge"'],
            commands,
            "the relay may only print its one line",
        )

    def test_the_gate_wakes_on_the_relay_and_the_merge_wakes_on_the_gate(self) -> None:
        gate = read("review-gate.yml")
        self.assertEqual(["CI", "review-signal"], workflow_run_workflows(gate))
        self.assertIn("  pull_request_target:", gate, "the lifecycle events stay")
        self.assertIn("schedule:", gate, "the schedule stays the backstop")

        merge = read("automerge.yml")
        self.assertEqual(["CI", "review-gate"], workflow_run_workflows(merge))
        self.assertIn("schedule:", merge, "the schedule stays the backstop")


if __name__ == "__main__":
    unittest.main()
