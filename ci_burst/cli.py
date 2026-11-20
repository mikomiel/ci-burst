"""Command line interface.

Observation is the default: ``plan``, ``status`` and ``export`` perform no
mutation. Only ``apply`` may launch, and only ``reap --apply`` may terminate.
Output is one JSON document with stable reason codes and no credentials, tokens,
user data or raw provider payloads.

``image`` is the opt-in warm-image group (``plan``, ``run``, ``status`` and
``promote``). Its exit codes are the same three: 0 for a completed decision
(``disabled`` included), 2 for a refusal (a ``refused`` result too) and 1 for a
provider failure (a refusal of the provider that escapes, or the result
``failed``). With ``[image]`` absent or disabled every form answers
``disabled`` before it opens a state file, builds a client or reads a network.
"""

import argparse
import functools
import json
import sys
import time
from pathlib import Path

from . import image_promote, image_status
from .aws import ImageAws
from .config import load
from .controller import Controller
from .errors import ImageRefusal, ProviderRefusal, Refusal
from .export import Exporter
from .forgejo import ForgejoSource
from .image_pipeline import ImagePipeline, write_event
from .image_state import ImageState

FAILED_DECISIONS = ("launch_failed", "cleanup_failed", "cleanup_degraded", "uncertain")
#: A build that ended badly. Only the image group reports it.
IMAGE_FAILED_DECISIONS = ("failed",)

IMAGE_UNTIL_DONE_NEEDS_APPLY = "IMAGE_UNTIL_DONE_NEEDS_APPLY"


def build_parser():
    parser = argparse.ArgumentParser(
        prog="ci-burst",
        description="Bounded AWS Spot runner capacity for one Forgejo repository queue.",
    )
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("plan", "apply", "status"):
        command = commands.add_parser(name)
        command.add_argument("--config", required=True, help="path to the deployment TOML file")
    reap = commands.add_parser("reap", help="independent expiry cleanup; never launches")
    reap.add_argument("--config", required=True, help="path to the deployment TOML file")
    reap.add_argument("--apply", action="store_true", help="request termination of expired owned instances")
    export = commands.add_parser(
        "export",
        help="write bounded worker console evidence and a metrics textfile; never mutates",
    )
    export.add_argument("--config", required=True, help="path to the deployment TOML file")
    export.add_argument("--metrics-file", required=True, help="path of the Prometheus textfile to replace atomically")
    export.add_argument(
        "--events-file",
        default=None,
        help="append-only JSON-lines event file (default: <ledger directory>/export/worker-events.ndjson)",
    )
    export.add_argument(
        "--state-file",
        default=None,
        help="dedupe record (default: <ledger directory>/export/observed.json)",
    )
    _add_image_parser(commands)
    return parser


def _add_image_parser(commands):
    group = commands.add_parser("image", help="the opt-in automated warm-image build and its promotion")
    image_commands = group.add_subparsers(dest="image_command", required=True)

    def add(name, **kwargs):
        command = image_commands.add_parser(name, **kwargs)
        command.add_argument("--config", required=True, help="path to the deployment TOML file")
        return command

    add("plan", help="say whether a build would start and what a pass would delete; writes nothing")
    run = add("run", help="without --apply this is plan; --apply runs one bounded pass")
    run.add_argument("--apply", action="store_true", help="run a pass that may build, record and clean up")
    run.add_argument("--until-done", action="store_true", help="repeat passes until the build ends (needs --apply)")
    run.add_argument("--force", action="store_true", help="skip the unchanged-digest and minimum-interval checks")
    status = add("status", help="report the builds, the candidate, the pin and the rollback target")
    status.add_argument(
        "--refresh",
        action="store_true",
        help="read the provider (describe only) and record the verification evidence",
    )
    promote = add("promote", help="print the evidence of a promotion; with --write move the pin")
    choice = promote.add_mutually_exclusive_group()
    choice.add_argument("--version", type=int, help="promote the candidate that owns this launch-template version")
    choice.add_argument("--rollback", action="store_true", help="restore the pin that the last promotion replaced")
    promote.add_argument("--write", action="store_true", help="edit the deployment file (the default only prints)")


def main(argv=None):
    arguments = build_parser().parse_args(argv)
    try:
        _check_arguments(arguments)
        config = load(arguments.config)
        if arguments.command == "image":
            result = _image(config, arguments)
        elif arguments.command == "export":
            result = _export(config, arguments)
        else:
            controller = Controller(config)
            if arguments.command == "plan":
                result = controller.plan()
            elif arguments.command == "apply":
                result = controller.apply()
            elif arguments.command == "status":
                result = controller.status()
            else:
                result = controller.reap(apply=arguments.apply)
    except Refusal as refusal:
        print(json.dumps({"error": refusal.as_dict()}, indent=2, sort_keys=True))
        # A provider refusal that escapes an image command is a provider failure.
        return 1 if arguments.command == "image" and isinstance(refusal, ProviderRefusal) else 2
    except Exception as error:  # noqa: BLE001 - the boundary reports a type name only
        print(json.dumps({"error": {"reason": "UNEXPECTED", "detail": type(error).__name__}}, indent=2))
        return 1
    print(json.dumps(result, indent=2, sort_keys=True, default=str))
    if result.get("decision") == "refused":
        return 2
    if result.get("decision") in FAILED_DECISIONS:
        return 1
    if arguments.command == "image" and result.get("decision") in IMAGE_FAILED_DECISIONS:
        return 1
    return 0


def _check_arguments(arguments):
    """Refuse a combination of options before the configuration is read or anything is called."""
    image_run = arguments.command == "image" and arguments.image_command == "run"
    if image_run and arguments.until_done and not arguments.apply:
        raise ImageRefusal(IMAGE_UNTIL_DONE_NEEDS_APPLY, "--until-done repeats applying passes: add --apply")


def _export(config, arguments):
    """One observation run, with paths derived from the deployment state."""
    directory = Path(config.state.database).parent / "export"
    events_file = arguments.events_file or str(directory / "worker-events.ndjson")
    state_file = arguments.state_file or str(directory / "observed.json")
    return Exporter(config).run(
        events_file=events_file,
        metrics_file=arguments.metrics_file,
        state_file=state_file,
    )


# -- the image group ------------------------------------------------------------------
#
# Everything that reaches the provider, the forge or the clock goes through the four
# functions below, and only from here, so a test replaces them and nothing else.


def make_image_aws(config):
    """A fresh image-role session. One instance is one SDK session (see ``ImageAws``)."""
    return ImageAws(config.aws.region, config.image.profile)


def make_image_source(config):
    """The read-only forge client, with the token variable of ``[image.source]`` and no other."""
    return ForgejoSource(
        config.forgejo.base_url,
        config.forgejo.repository,
        config.image.source.token_env,
        timeout_seconds=config.forgejo.timeout_seconds,
        max_response_bytes=config.forgejo.max_response_bytes,
    )


def image_clock():
    return time.time()


def image_pause(seconds):
    time.sleep(seconds)


class _Sessions:
    """The aws factory of a pipeline. It keeps the newest session, so a pass's pin can be stamped on it."""

    def __init__(self, config):
        self._config = config
        self.last = None

    def __call__(self):
        self.last = make_image_aws(self._config)
        return self.last


def _image(config, arguments):
    """Dispatch one ``image`` command. A deployment without the image path answers ``disabled`` at once."""
    if not config.image.enabled:
        return {"decision": "disabled"}
    state = ImageState(config.image.state_directory, clock=image_clock)
    command = arguments.image_command
    if command == "status":
        if arguments.refresh:
            return image_status.refresh(config, state=state, aws=make_image_aws(config), clock=image_clock)
        return image_status.status(config, state=state, clock=image_clock)
    if command == "promote":
        return image_promote.promote(
            config,
            state=state,
            clock=image_clock,
            events=functools.partial(write_event, state.directory, config.deployment, image_clock),
            version=arguments.version,
            rollback=arguments.rollback,
            write=arguments.write,
        )
    sessions = _Sessions(config)
    pipeline = ImagePipeline(
        config,
        arguments.config,
        aws_factory=sessions,
        source_factory=lambda: make_image_source(config),
        state=state,
        clock=image_clock,
        sleep=image_pause,
    )
    if command == "plan":
        return pipeline.plan()
    if not arguments.apply:
        return pipeline.run_pass(apply=False, force=arguments.force)
    return _apply(config, arguments, pipeline, sessions, state)


def _apply(config, arguments, pipeline, sessions, state):
    """An applying run: resolve a pending promotion, run the pass (or passes), then stamp the pins.

    The pipeline itself never reads the deployment file's bytes, so the
    resolution of a crashed promotion lives here (``_resolve_journal``).
    """
    try:
        resolved = _resolve_journal(config, state)
        if arguments.until_done:
            result = pipeline.run_until_done(force=arguments.force)
        else:
            result = pipeline.run_pass(apply=True, force=arguments.force)
        result["journal_resolved"] = resolved
        result["pin_evidence"] = _stamp_pins(config, state, sessions)
        return result
    finally:
        state.close()


def _resolve_journal(config, state):
    """Resolve the pending journal row of a promotion that crashed, from the hash of the deployment file.

    The state is left open with the writer lock held, so the pass that follows runs
    under the same lock. A file that matches neither hash of a row refuses the
    whole run with nothing written (``PROMOTION_JOURNAL_DIVERGED``). With no state
    file there is nothing to resolve, and nothing is created here: a refusal that
    needs no network leaves no file behind.
    """
    if not state.connect_readonly():
        return []
    state.close()  # reopen for writing: a read-only connection cannot be upgraded
    state.connect()
    state.acquire_writer()
    return image_promote.resolve_pending(state, image_promote.read_snapshot(config).sha256)


def _stamp_pins(config, state, sessions):
    """Record what the provider says about the live pin and the rollback target, from the pass's session.

    A pin that a pass observed (or that an operator set by hand) has no row until
    now, and a rollback needs a row that shows its image. These are describe-only
    reads on the session the pass already holds. A failure is reported here and
    does not replace the result of the pass.
    """
    if sessions.last is None:
        return {"recorded": False, "reason": "IMAGE_NO_SESSION"}
    try:
        report = image_status.refresh(config, state=state, aws=sessions.last, clock=image_clock, candidates=False)
    except Refusal as refusal:
        return {"recorded": False, **refusal.as_dict()}
    return {
        "recorded": True,
        "versions": [item["template_version"] for item in report["refreshed"]],
        "pin_records_created": report["pin_records_created"],
    }


if __name__ == "__main__":
    sys.exit(main())
