"""Run from the repository: uv run --project deploy python deploy/manage.py ACTION."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import remote
from config import load_config


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--config", type=Path, default=Path(__file__).parent / "private/settings.py"
    )
    commands = parser.add_subparsers(dest="action", required=True)
    for name in ("check", "provision", "setup", "cloud-status", "status", "smoke", "ssh"):
        commands.add_parser(name)
    for name in ("bringup", "prepare", "init-db", "import-catalog", "deploy", "rollback"):
        command = commands.add_parser(name)
        command.add_argument("--revision", default="HEAD", required=name == "rollback")
        command.add_argument(
            "--allow-dirty",
            action="store_true",
            help="Explicitly deploy the chosen commit, excluding uncommitted files",
        )
    logs = commands.add_parser("logs")
    logs.add_argument("component", choices=("app", "caddy", "postgres"))
    logs.add_argument("--revision")
    destroy = commands.add_parser("destroy")
    destroy.add_argument("--confirm", required=True, help="Exact NAME from settings")
    args = parser.parse_args(argv)
    config = load_config(args.config)
    if args.action == "check":
        remote.local_check(config)
        return
    if args.action == "smoke":
        remote.smoke(config)
        return
    # Only these explicitly invoked actions load the AWS control implementation.
    import cloud

    if args.action == "provision":
        print(f"Provisioning {config.name} in {config.region}...", flush=True)
        state = cloud.provision(config)
        print(f"Instance ready: {state['ip']}. Next: setup.")
        return
    if args.action == "cloud-status":
        print(json.dumps(cloud.status(config), indent=2))
        return
    if args.action == "destroy":
        print(f"DNS is manual: restore or remove the A record for {config.hostname} yourself.")
        cloud.destroy(config, args.confirm)
        (config.state_dir / "secrets.json").unlink(missing_ok=True)
        (config.state_dir / "known_hosts").unlink(missing_ok=True)
        print("Teardown complete. Fresh application secrets will be generated on next setup.")
        return
    revision = None
    if hasattr(args, "allow_dirty"):
        revision = remote.commit_id(args.revision, args.allow_dirty)
    if args.action == "bringup":
        remote.local_check(config)
        print("Provisioning the Lightsail instance and networking...", flush=True)
        cloud.provision(config)
    cloud.trust_host(config)
    target = remote.Remote(config, cloud.load_state(config))
    if args.action == "bringup":
        print("Installing host services...", flush=True)
        target.setup()
        print(f"Preparing release {revision}...", flush=True)
        target.prepare(revision)
        print("Initializing PostgreSQL and importing the catalog...", flush=True)
        target.initialize_database(revision)
        target.import_catalog(revision)
        print("Activating the application...", flush=True)
        target.release("activate", revision)
        print(f"Set the DNS A record for {config.hostname} to {target.state['ip']}.")
        print("Bring-up steps complete. Allow DNS/TLS issuance, then run smoke.")
    elif args.action == "setup":
        target.setup()
    elif args.action == "prepare":
        target.prepare(revision)
    elif args.action == "init-db":
        target.initialize_database(revision)
    elif args.action == "import-catalog":
        target.import_catalog(revision)
    elif args.action in {"deploy", "rollback"}:
        target.push(revision)
        target.release("activate", revision)
    elif args.action == "status":
        target.release("status")
    elif args.action == "ssh":
        remote.run("ssh", *target.options, target.host)
    elif args.action == "logs":
        target.logs(args.component, args.revision)


if __name__ == "__main__":
    main()
