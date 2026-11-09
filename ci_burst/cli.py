"""Command line interface.

Observation is the default: ``plan`` and ``status`` perform no mutation. Only
``apply`` may launch, and only ``reap --apply`` may terminate. Output is one
JSON document with stable reason codes and no credentials, tokens, user data or
raw provider payloads.
"""

import argparse
import json
import sys

from .config import load
from .controller import Controller
from .errors import Refusal

FAILED_DECISIONS = ("launch_failed", "cleanup_failed", "uncertain")


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
    return parser


def main(argv=None):
    arguments = build_parser().parse_args(argv)
    try:
        config = load(arguments.config)
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


if __name__ == "__main__":
    sys.exit(main())
