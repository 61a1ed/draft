"""Executable runbook: run one named step, inspect its output, then run the next."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import remote
from config import load_config, load_state

CLOUD_STEPS = {
    "create-key": "create_key",
    "create-instance": "create_instance",
    "open-ports": "open_ports",
    "allocate-ip": "allocate_ip",
    "attach-ip": "attach_ip",
    "trust-host": "trust_host",
}
DELETE_STEPS = {
    "delete-instance": "delete_instance",
    "release-ip": "release_ip",
    "delete-key": "delete_key",
}
DATABASE_STEPS = {"init-db": "init", "import-catalog": "import", "verify-db": "verify"}


def show_steps(config_path):
    prefix = ["uv", "run", "--project", "deploy", "python", "deploy/manage.py"]
    if config_path != Path(__file__).parent / "private/settings.py":
        prefix += ["--config", str(config_path)]
    print("Run each command separately from the repository root; stop on the first error.")
    print("Fresh machine only (skip these create steps for an existing deployment):")
    print(remote.command_text([*prefix, "create-certs"]))
    for action in ("check", *CLOUD_STEPS):
        print(remote.command_text([*prefix, action]))
    print("Existing machine: use its saved state.json, SSH key and known_hosts.")
    print("Install, prepare, initialize, import, then activate:")
    for action in (
        "setup", "open-db-port", "verify-db", "prepare", "init-db", "import-catalog", "deploy",
    ):
        args = [] if action in {"setup", "open-db-port"} else ["--revision", "HEAD"]
        print(remote.command_text([*prefix, action, *args]))
    print("Replace HEAD with one full commit ID to pin all steps to the same source.")
    print("Set DNS manually to the saved IP, then run:")
    print(remote.command_text([*prefix, "smoke"]))
    print("See deploy/README.md for failures, direct host commands and explicit deletion steps.")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--config", type=Path, default=Path(__file__).parent / "private/settings.py"
    )
    commands = parser.add_subparsers(dest="action", required=True)
    for name in (
        "steps", "check", *CLOUD_STEPS, "setup", "cloud-status", "status", "smoke", "ssh",
        "create-certs", "renew-certs", "open-db-port",
    ):
        commands.add_parser(name)
    for name in ("prepare", *DATABASE_STEPS, "deploy", "rollback"):
        command = commands.add_parser(name)
        command.add_argument("--revision", default="HEAD", required=name == "rollback")
        command.add_argument(
            "--allow-dirty", action="store_true",
            help="Explicitly deploy the chosen commit, excluding uncommitted files",
        )
        if name in DATABASE_STEPS:
            command.add_argument("--host", help="Database address; defaults to the saved IP")
    logs = commands.add_parser("logs")
    logs.add_argument("component", choices=("app", "caddy", "postgres"))
    logs.add_argument("--revision")
    for name in DELETE_STEPS:
        command = commands.add_parser(name)
        command.add_argument("--confirm", required=True, help="Exact NAME from settings")
    # Old commands must never silently create/reconcile/delete a whole deployment.
    for name in ("bringup", "provision", "destroy"):
        command = commands.add_parser(name, help="Replaced by explicit steps; does not execute")
        command.add_argument("--allow-dirty", action="store_true", help=argparse.SUPPRESS)
        command.add_argument("--revision", help=argparse.SUPPRESS)
        command.add_argument("--confirm", help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    if args.action == "steps":
        show_steps(args.config)
        return
    if args.action in {"bringup", "provision", "destroy"}:
        parser.error(
            f"{args.action} was replaced by explicit runbook steps; no actions were taken. "
            "Run 'steps' or read deploy/README.md. For an existing host, start with 'setup'. "
            "Deletion uses delete-instance, release-ip and delete-key individually."
        )
    config = load_config(args.config)
    if args.action in {"create-certs", "renew-certs"}:
        from tls import ensure_certificates

        path = ensure_certificates(config, renew=args.action == "renew-certs")
        print(f"Database certificates ready: {path}")
        if args.action == "renew-certs":
            print("Run setup to install the renewed server certificate, then verify-db.")
        return
    if args.action == "check":
        remote.local_check(config)
        return
    if args.action == "smoke":
        remote.smoke(config)
        return
    if args.action in {*CLOUD_STEPS, *DELETE_STEPS, "cloud-status", "open-db-port"}:
        import cloud

        if args.action == "cloud-status":
            print(json.dumps(cloud.status(config), indent=2))
        elif args.action == "open-db-port":
            cloud.open_database_port(config)
        elif args.action in DELETE_STEPS:
            print(f"DNS is manual: restore or remove the A record for {config.hostname} yourself.")
            getattr(cloud, DELETE_STEPS[args.action])(config, args.confirm)
        else:
            getattr(cloud, CLOUD_STEPS[args.action])(config)
        return

    revision = None
    if hasattr(args, "allow_dirty"):
        revision = remote.commit_id(args.revision, args.allow_dirty)
        print(f"Selected committed revision: {revision}", flush=True)
    if args.action in DATABASE_STEPS:
        from database import run_database

        run_database(
            config, load_state(config), DATABASE_STEPS[args.action], revision, host=args.host,
        )
        return
    # Host operations use verified SSH; ordinary database operations above do not.
    target = remote.Remote(config, load_state(config))
    if args.action == "setup":
        target.setup()
    elif args.action == "prepare":
        target.prepare(revision)
    elif args.action in {"deploy", "rollback"}:
        target.push(revision)
        target.release("activate", revision)
        print(f"DNS A record: {config.hostname} -> {target.state['ip']} (set manually).")
    elif args.action == "status":
        target.release("status")
    elif args.action == "ssh":
        remote.run("ssh", *target.options, target.host, timeout=None, interactive=True)
    elif args.action == "logs":
        target.logs(args.component, args.revision)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print(
            "\nCancelled by Ctrl-C. Deployment did not complete. "
            "Remote work may still be running; inspect the host before retrying. "
            "Saved deployment state has been retained.",
            file=sys.stderr, flush=True,
        )
        raise SystemExit(130) from None
