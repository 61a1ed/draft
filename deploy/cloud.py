"""Lightsail lifecycle operations, invoked only by the authenticated administrator.

The local manifest is part of the deployment: retain it until teardown completes.
Resources are identified by their recorded ARN. Incomplete provisioning requires
manual inspection and cleanup; it is never resumed or adopted on a retry.
DNS records are managed manually by the operator.
"""

import base64
import hashlib
import json
import os
import re
import subprocess
import time
import uuid
from contextlib import contextmanager
from pathlib import Path

import boto3
from botocore.exceptions import ClientError
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from config import Config

OWNER_TAG = "draft-deployment-id"
WAIT_SECONDS = 900
MANUAL_CLEANUP = (
    "Provisioning is incomplete. Retain state.json and the SSH key, allow any "
    "in-flight AWS operations to finish, and inspect the named resources in AWS. "
    "Manually delete resources whose ARN was not saved, then run destroy --confirm NAME "
    "to remove the remaining deployment before provisioning again."
)


def protect_ssh_key(path: Path) -> None:
    """Set permissions on the private key only, as required by OpenSSH."""
    if os.name == "nt":
        identity = subprocess.run(
            ["whoami"], check=True, capture_output=True, text=True
        ).stdout.strip()
        subprocess.run(
            ["icacls", str(path), "/inheritance:r", "/grant:r", f"{identity}:F"],
            check=True,
            capture_output=True,
        )
    else:
        path.chmod(0o600)


def _prepare_directory(config: Config) -> None:
    config.state_dir.mkdir(parents=True, exist_ok=True)


@contextmanager
def _lock(config: Config):
    _prepare_directory(config)
    with (config.state_dir / "cloud.lock").open("a+b") as handle:
        handle.seek(0)
        handle.write(b"0")
        handle.flush()
        handle.seek(0)
        try:
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            raise RuntimeError("Another cloud command is using this deployment state")
        try:
            yield
        finally:
            handle.seek(0)
            if os.name == "nt":
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(handle, fcntl.LOCK_UN)


def load_state(config: Config) -> dict:
    path = config.state_dir / "state.json"
    if not path.exists():
        return {}
    try:
        state = json.loads(path.read_text(encoding="utf-8"))
    except (ValueError, OSError):
        raise RuntimeError(
            "Cannot read deployment state.json; restore it before proceeding"
        )
    if (
        not isinstance(state, dict)
        or state.get("version") != 1
        or state.get("name") != config.name
        or state.get("region") != config.region
        or state.get("hostname") != config.hostname
    ):
        raise RuntimeError("Deployment state does not match NAME, REGION and HOSTNAME")
    return state


def _save(config: Config, state: dict) -> None:
    path = config.state_dir / "state.json"
    temporary = path.with_suffix(".json.tmp")
    with temporary.open("w", encoding="utf-8") as handle:
        json.dump(state, handle, indent=2)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
    temporary.replace(path)


def ensure_ssh_key(config: Config) -> Path:
    """Generate once locally, preserving any existing private key on every retry."""
    _prepare_directory(config)
    private_path = config.state_dir / "id_rsa"
    public_path = config.state_dir / "id_rsa.pub"
    if private_path.exists():
        try:
            key = serialization.load_pem_private_key(private_path.read_bytes(), password=None)
        except (ValueError, TypeError):
            raise RuntimeError(
                "Existing SSH private key is invalid; it has not been overwritten"
            )
        if not isinstance(key, rsa.RSAPrivateKey):
            raise RuntimeError("Existing SSH private key must be RSA for Lightsail import")
    else:
        if public_path.exists():
            raise RuntimeError(
                "SSH public key exists without its private key; restore the private key"
            )
        key = rsa.generate_private_key(public_exponent=65537, key_size=3072)
        # Exclusive creation prevents accidental replacement, including concurrent runs.
        with private_path.open("xb") as handle:
            handle.write(
                key.private_bytes(
                    serialization.Encoding.PEM,
                    serialization.PrivateFormat.TraditionalOpenSSL,
                    serialization.NoEncryption(),
                )
            )
    protect_ssh_key(private_path)
    public_key = key.public_key().public_bytes(
        serialization.Encoding.OpenSSH, serialization.PublicFormat.OpenSSH
    )
    if public_path.exists():
        if public_path.read_bytes().strip().split()[:2] != public_key.split():
            raise RuntimeError("SSH public key does not match the existing private key")
    else:
        with public_path.open("xb") as handle:
            handle.write(public_key + b"\n")
    return private_path.resolve()


def _session(config: Config):
    return boto3.Session(profile_name=config.aws_profile, region_name=config.region)


def _account(session, state: dict) -> str:
    account = session.client("sts").get_caller_identity()["Account"]
    if state and state.get("account_id") != account:
        raise RuntimeError("Current AWS account differs from the account in deployment state")
    return account


def _get(client, kind: str, name: str) -> dict | None:
    method, argument, result = {
        "instance": ("get_instance", "instanceName", "instance"),
        "key_pair": ("get_key_pair", "keyPairName", "keyPair"),
        "static_ip": ("get_static_ip", "staticIpName", "staticIp"),
    }[kind]
    try:
        return getattr(client, method)(**{argument: name})[result]
    except ClientError as error:
        if error.response["Error"]["Code"] == "NotFoundException":
            return None
        raise


def _wait_operations(client, response: dict) -> None:
    operations = response.get("operations", [])
    if "operation" in response:
        operations = [response["operation"]]
    deadline = time.monotonic() + WAIT_SECONDS
    for operation in operations:
        while operation.get("status") not in {"Succeeded", "Completed"}:
            if operation.get("status") == "Failed":
                raise RuntimeError(
                    f"Lightsail operation failed: {operation.get('id')} "
                    f"({operation.get('errorCode', 'unknown error')}): "
                    f"{operation.get('errorDetails', 'no details')}"
                )
            if time.monotonic() >= deadline:
                raise RuntimeError("Timed out waiting for Lightsail; inspect the operation in AWS")
            time.sleep(3)
            operation = client.get_operation(operationId=operation["id"])["operation"]


def _wait_resource(client, kind: str, name: str, *, absent=False) -> dict | None:
    deadline = time.monotonic() + WAIT_SECONDS
    while time.monotonic() < deadline:
        resource = _get(client, kind, name)
        if (absent and resource is None) or (not absent and resource is not None):
            return resource
        time.sleep(3)
    raise RuntimeError(f"Timed out waiting for {kind}; inspect the resource in AWS")


def _owned(state: dict, kind: str, resource: dict) -> None:
    entry = state["resources"].get(kind, {})
    if resource.get("name") != entry.get("name"):
        raise RuntimeError(f"Refusing to use an unowned {kind}")
    if entry.get("arn"):
        if resource.get("arn") == entry["arn"]:
            return
        raise RuntimeError(
            f"The {kind} was replaced outside this deployment; refusing to modify it"
        )
    raise RuntimeError(
        f"Cannot prove ownership of existing {kind} {entry.get('name')!r}: its ARN "
        "was not saved. Inspect/delete it manually in AWS, then rerun destroy."
    )


def _ensure(config: Config, state: dict, client, kind: str, name: str, create) -> dict:
    existing = _get(client, kind, name)
    if existing is not None:
        _owned(state, kind, existing)
        return existing
    entry = state["resources"].get(kind)
    if entry and entry.get("arn"):
        raise RuntimeError(
            f"The recorded {kind} is missing; destroy the remaining deployment "
            "before rebuilding so application secrets are rotated"
        )
    # Record the name before the request so an operator can locate a partial result.
    state["resources"][kind] = {"name": name}
    _save(config, state)
    response = create()
    _wait_operations(client, response)
    resource = _wait_resource(client, kind, name)
    state["resources"][kind]["arn"] = resource["arn"]
    _save(config, state)
    return resource


def _pages(client, method: str, result: str, **kwargs):
    while True:
        page = getattr(client, method)(**kwargs)
        yield from page[result]
        if not page.get("nextPageToken"):
            break
        kwargs["pageToken"] = page["nextPageToken"]


def _validate_catalog(config: Config, client) -> str:
    blueprint = next(
        (
            item
            for item in _pages(client, "get_blueprints", "blueprints", includeInactive=False)
            if item["blueprintId"] == config.blueprint_id
        ),
        None,
    )
    if blueprint is None or blueprint.get("platform") != "LINUX_UNIX":
        raise RuntimeError("Configured Ubuntu blueprint is unavailable in REGION")
    bundle = next(
        (
            item
            for item in _pages(client, "get_bundles", "bundles", includeInactive=False)
            if item["bundleId"] == config.bundle_id
        ),
        None,
    )
    if (
        bundle is None
        or not bundle.get("isActive", True)
        or "LINUX_UNIX" not in bundle.get("supportedPlatforms", [])
        or bundle.get("ramSizeInGb", 0) < 2
        or bundle.get("publicIpv4AddressCount", 0) < 1
    ):
        raise RuntimeError(
            "BUNDLE_ID must be an available Linux bundle with public IPv4 and at least 2 GB RAM"
        )
    regions = client.get_regions(includeAvailabilityZones=True)["regions"]
    region = next((item for item in regions if item["name"] == config.region), None)
    zones = [
        item["zoneName"]
        for item in (region or {}).get("availabilityZones", [])
        if item.get("state") == "available"
    ]
    zone = config.availability_zone or (sorted(zones)[0] if zones else None)
    if zone not in zones:
        raise RuntimeError("No matching available Lightsail Availability Zone in REGION")
    return zone


def provision(config: Config) -> dict:
    """Create a host and IPv4 address, or verify a completed deployment."""
    with _lock(config):
        state = load_state(config)
        if state and not state.get("destroyed") and not state.get("ip"):
            raise RuntimeError(MANUAL_CLEANUP)
        if state.get("ip") and set(state.get("resources", {})) != {
            "key_pair",
            "instance",
            "static_ip",
        }:
            raise RuntimeError("Deployment resources are incomplete; finish teardown first")
        session = _session(config)
        account = _account(session, state)
        client = session.client("lightsail")
        zone = _validate_catalog(config, client)
        if state.get("resources") and not (config.state_dir / "id_rsa").exists():
            raise RuntimeError(
                "The deployment private key is missing; restore it before proceeding"
            )
        private_key = ensure_ssh_key(config)
        key_hash = hashlib.sha256(private_key.with_suffix(".pub").read_bytes()).hexdigest()
        if not state:
            deployment_id = uuid.uuid4().hex
            state = {
                "version": 1,
                "name": config.name,
                "region": config.region,
                "hostname": config.hostname,
                "account_id": account,
                "deployment_id": deployment_id,
                "instance_name": config.name,
                "key_pair_name": f"{config.name}-key-{deployment_id[:8]}",
                "static_ip_name": f"{config.name}-ip-{deployment_id[:8]}",
                "private_key": str(private_key),
                "key_sha256": key_hash,
                "ssh_user": "ubuntu",
                "resources": {},
            }
            _save(config, state)
        if state.get("private_key") != str(private_key):
            raise RuntimeError("Private-key path does not match the saved deployment")
        if state.get("key_sha256") != key_hash:
            raise RuntimeError("Local SSH key differs from the deployment's imported key")
        if state.get("spec") and state["spec"] != {
            "blueprint": config.blueprint_id,
            "bundle": config.bundle_id,
            "zone": zone,
        }:
            raise RuntimeError("Instance image, size or zone changed; destroy before rebuilding")
        state["spec"] = {"blueprint": config.blueprint_id, "bundle": config.bundle_id, "zone": zone}
        state.pop("destroyed", None)
        _save(config, state)
        try:
            public_key = private_key.with_suffix(".pub").read_bytes()
            _ensure(
                config,
                state,
                client,
                "key_pair",
                state["key_pair_name"],
                lambda: client.import_key_pair(
                    keyPairName=state["key_pair_name"],
                    publicKeyBase64=base64.b64encode(public_key).decode("ascii"),
                ),
            )
            instance = _ensure(
                config,
                state,
                client,
                "instance",
                state["instance_name"],
                lambda: client.create_instances(
                    instanceNames=[state["instance_name"]],
                    availabilityZone=zone,
                    blueprintId=config.blueprint_id,
                    bundleId=config.bundle_id,
                    keyPairName=state["key_pair_name"],
                    ipAddressType="ipv4",
                    tags=[{"key": OWNER_TAG, "value": state["deployment_id"]}],
                ),
            )
            if instance.get("sshKeyName") != state["key_pair_name"]:
                raise RuntimeError("Instance has a different SSH key; refusing to continue")
            deadline = time.monotonic() + WAIT_SECONDS
            while instance.get("state", {}).get("name") != "running":
                if instance.get("state", {}).get("name") in {"stopped", "stopping", "terminated"}:
                    raise RuntimeError(
                        "Instance is stopped or terminating; inspect it before retrying"
                    )
                if time.monotonic() >= deadline:
                    raise RuntimeError("Timed out waiting for instance to run; inspect it in AWS")
                time.sleep(3)
                instance = _get(client, "instance", state["instance_name"])
            _wait_operations(
                client,
                client.put_instance_public_ports(
                    instanceName=state["instance_name"],
                    portInfos=[
                        {
                            "fromPort": 22,
                            "toPort": 22,
                            "protocol": "tcp",
                            "cidrs": list(config.ssh_cidrs),
                        },
                        {"fromPort": 80, "toPort": 80, "protocol": "tcp", "cidrs": ["0.0.0.0/0"]},
                        {"fromPort": 443, "toPort": 443, "protocol": "tcp", "cidrs": ["0.0.0.0/0"]},
                    ],
                ),
            )
            static_ip = _ensure(
                config,
                state,
                client,
                "static_ip",
                state["static_ip_name"],
                lambda: client.allocate_static_ip(staticIpName=state["static_ip_name"]),
            )
            if (
                static_ip.get("isAttached")
                and static_ip.get("attachedTo") != state["instance_name"]
            ):
                raise RuntimeError(
                    "Static IP is attached to a different instance; refusing to move it"
                )
            if not static_ip.get("isAttached"):
                _wait_operations(
                    client,
                    client.attach_static_ip(
                        staticIpName=state["static_ip_name"], instanceName=state["instance_name"]
                    ),
                )
            state["ip"] = static_ip["ipAddress"]
            _save(config, state)
            return state
        except Exception as error:
            if not state.get("ip"):
                error.add_note(MANUAL_CLEANUP)
            raise


def destroy(config: Config, confirmation: str) -> None:
    """Delete owned resources after manual DNS removal, preserving the local SSH key."""
    if confirmation != config.name:
        raise ValueError("Teardown requires the exact deployment NAME as confirmation")
    with _lock(config):
        state = load_state(config)
        if not state:
            raise RuntimeError("No deployment state; there are no proven owned resources to delete")
        session = _session(config)
        _account(session, state)
        client = session.client("lightsail")
        # Check every resource before deleting any, including IP attachment changes.
        for kind, entry in state["resources"].items():
            resource = _get(client, kind, entry["name"])
            if resource is not None:
                _owned(state, kind, resource)
                if (
                    kind == "static_ip"
                    and resource.get("isAttached")
                    and resource.get("attachedTo") != state["instance_name"]
                ):
                    raise RuntimeError("Static IP moved to another instance; refusing teardown")
        for kind in ("instance", "static_ip", "key_pair"):
            entry = state["resources"].get(kind)
            if not entry:
                continue
            resource = _get(client, kind, entry["name"])
            if resource is not None:
                _owned(state, kind, resource)
                if kind == "instance":
                    response = client.delete_instance(instanceName=entry["name"])
                elif kind == "static_ip":
                    if resource.get("isAttached"):
                        _wait_operations(
                            client, client.detach_static_ip(staticIpName=entry["name"])
                        )
                    response = client.release_static_ip(staticIpName=entry["name"])
                else:
                    response = client.delete_key_pair(keyPairName=entry["name"])
                _wait_operations(client, response)
                _wait_resource(client, kind, entry["name"], absent=True)
            del state["resources"][kind]
            _save(config, state)
        state.pop("ip", None)
        state.pop("spec", None)
        state["destroyed"] = True
        _save(config, state)


def status(config: Config) -> dict:
    state = load_state(config)
    if not state:
        return {"name": config.name, "region": config.region, "provisioned": False}
    session = _session(config)
    _account(session, state)
    client = session.client("lightsail")
    resources = {}
    for kind, entry in state["resources"].items():
        resource = _get(client, kind, entry["name"])
        if resource:
            _owned(state, kind, resource)
        resources[kind] = {"name": entry["name"], "exists": resource is not None}
        if kind == "instance" and resource:
            resources[kind]["state"] = resource.get("state", {}).get("name")
    return {
        "name": config.name,
        "region": config.region,
        "account_id": state["account_id"],
        "ip": state.get("ip"),
        "hostname": config.hostname,
        "resources": resources,
        "destroyed": state.get("destroyed", False),
    }


def trust_host(config: Config) -> Path:
    """Obtain host keys through authenticated AWS, never unauthenticated ssh-keyscan."""
    with _lock(config):
        state = load_state(config)
        if not state.get("ip") or state.get("destroyed"):
            raise RuntimeError("Provision the instance before preparing SSH")
        session = _session(config)
        _account(session, state)
        client = session.client("lightsail")
        instance = _get(client, "instance", state["instance_name"])
        if not instance:
            raise RuntimeError("The deployment instance no longer exists")
        _owned(state, "instance", instance)
        # This response also contains an AWS-generated temporary private key. Never
        # log, serialize or return that response; only consume authenticated host keys.
        deadline = time.monotonic() + 300
        while True:
            try:
                details = client.get_instance_access_details(
                    instanceName=state["instance_name"], protocol="ssh"
                )["accessDetails"]
            except ClientError as error:
                if error.response["Error"]["Code"] != "OperationFailureException":
                    raise
                details = {}
            if details.get("ipAddress") == state["ip"] and details.get("hostKeys"):
                break
            if time.monotonic() >= deadline:
                raise RuntimeError(
                    "AWS has not reported host keys for the static IP yet; retry later"
                )
            time.sleep(5)
        lines = []
        for key in details.get("hostKeys", []):
            algorithm = key.get("algorithm", "")
            if not re.fullmatch(r"ssh-(?:rsa|ed25519)|ecdsa-sha2-nistp(?:256|384|521)", algorithm):
                continue
            parts = key.get("publicKey", "").split()
            encoded = (
                parts[1]
                if len(parts) >= 2 and parts[0] == algorithm
                else (parts[0] if len(parts) == 1 else "")
            )
            try:
                serialization.load_ssh_public_key(f"{algorithm} {encoded}".encode("ascii"))
            except (ValueError, TypeError, UnicodeError):
                raise RuntimeError(
                    "AWS returned an invalid SSH host key; nothing was trusted"
                )
            lines.append(f"{state['ip']} {algorithm} {encoded}\n")
        if not lines:
            raise RuntimeError("AWS has not reported SSH host keys yet; wait briefly and retry")
        path = config.state_dir / "known_hosts"
        temporary = path.with_suffix(".tmp")
        temporary.write_text("".join(lines), encoding="utf-8")
        temporary.replace(path)
        return path
