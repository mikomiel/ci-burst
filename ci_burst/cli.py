"""Command line interface.

Observation is the default: ``plan``, ``status`` and ``export`` perform no
mutation. Only ``apply`` may launch, and only ``reap --apply`` may terminate.
Output is one JSON document with stable reason codes and no credentials, tokens,
user data or raw provider payloads.
"""

import argparse
import json
import sys
from pathlib import Path

from .config import load
from .controller import Controller
from .errors import Refusal
from .export import Exporter

FAILED_DECISIONS = ("launch_failed", "cleanup_failed", "cleanup_degraded", "uncertain")


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
    return parser


def main(argv=None):
    arguments = build_parser().parse_args(argv)
    try:
        config = load(arguments.config)
        if arguments.command == "export":
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
        return 2
    except Exception as error:  # noqa: BLE001 - the boundary reports a type name only
        print(json.dumps({"error": {"reason": "UNEXPECTED", "detail": type(error).__name__}}, indent=2))
        return 1
    print(json.dumps(result, indent=2, sort_keys=True, default=str))
    if result.get("decision") == "refused":
        return 2
    if result.get("decision") in FAILED_DECISIONS:
        return 1
    return 0


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


if __name__ == "__main__":
    sys.exit(main())
