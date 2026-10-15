"""Explicit Lightsail runbook steps. Each action makes only its named change.

state.json is a receipt of names and identities, not a desired-state engine.
Existing version-1 receipts and their original resource names remain usable.
"""

import hashlib
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

import boto3
from botocore.config import Config as AWSConfig
from botocore.exceptions import ClientError
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from config import Config, load_state
from remote import command_text, run

WAIT_SECONDS = 900
READS = {
    "key_pair": ("get_key_pair", "keyPairName", "keyPair"),
    "instance": ("get_instance", "instanceName", "instance"),
    "static_ip": ("get_static_ip", "staticIpName", "staticIp"),
}


def protect_ssh_key(path: Path) -> None:
    """Restrict only the private key; leave settings and directories editable."""
    if os.name == "nt":
        identity = run("whoami", stdout=subprocess.PIPE, text=True, timeout=15).stdout.strip()
        run(
            "icacls", str(path), "/inheritance:r", "/grant:r", f"{identity}:F", timeout=15,
        )
    else:
        path.chmod(0o600)


def ensure_ssh_key(config: Config) -> Path:
    """Keep the existing local key; generate one only when neither half exists."""
    config.state_dir.mkdir(parents=True, exist_ok=True)
    private_path = config.state_dir / "id_rsa"
    public_path = config.state_dir / "id_rsa.pub"
    if private_path.exists():
        try:
            key = serialization.load_pem_private_key(private_path.read_bytes(), password=None)
        except (ValueError, TypeError):
            raise RuntimeError("Existing SSH private key is invalid; it has not been overwritten")
        if not isinstance(key, rsa.RSAPrivateKey):
            raise RuntimeError("Existing SSH private key must be RSA for Lightsail import")
    else:
        if public_path.exists():
            raise RuntimeError("SSH public key exists without its private key; restore it")
        key = rsa.generate_private_key(public_exponent=65537, key_size=3072)
        with private_path.open("xb") as handle:
            handle.write(key.private_bytes(
                serialization.Encoding.PEM, serialization.PrivateFormat.TraditionalOpenSSL,
                serialization.NoEncryption(),
            ))
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


def _save(config, state):
    config.state_dir.mkdir(parents=True, exist_ok=True)
    path = config.state_dir / "state.json"
    temporary = path.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(state, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


def _session(config):
    return boto3.Session(profile_name=config.aws_profile, region_name=config.region)


def _client(config, state):
    session = _session(config)
    # Never replay a mutating request automatically after an ambiguous network failure.
    options = AWSConfig(connect_timeout=10, read_timeout=30, retries={"total_max_attempts": 1})
    _show(config, "get_caller_identity", {}, service="sts")
    account = session.client("sts", config=options).get_caller_identity()["Account"]
    if state and account != state.get("account_id"):
        raise RuntimeError("Current AWS account differs from the account in deployment state")
    print(f"AWS account: {account}; region: {config.region}", file=sys.stderr, flush=True)
    return session.client("lightsail", config=options), account


def _show(config, method, parameters, *, service="lightsail", query=None):
    command = ["aws", "--region", config.region]
    if config.aws_profile:
        command += ["--profile", config.aws_profile]
    command += [service, method.replace("_", "-"), "--cli-input-json", json.dumps(parameters)]
    if query:
        command += ["--query", query]
    print(command_text(command), file=sys.stderr, flush=True)


def _read(client, kind, name):
    method, argument, result = READS[kind]
    try:
        return getattr(client, method)(**{argument: name})[result]
    except ClientError as error:
        if error.response["Error"]["Code"] == "NotFoundException":
            return None
        raise


def _recorded(client, state, kind):
    entry = state.get("resources", {}).get(kind, {})
    if not entry.get("name") or not entry.get("arn"):
        raise RuntimeError(f"No recorded identity for {kind}; inspect cloud-status first")
    resource = _read(client, kind, entry["name"])
    if (not resource or resource.get("arn") != entry["arn"]
            or resource.get("name") != entry["name"]):
        raise RuntimeError(f"Recorded {kind} is missing or replaced; inspect cloud-status")
    return resource


def _wait_operations(config, client, response):
    operations = response.get("operations", [])
    if "operation" in response:
        operations = [response["operation"]]
    deadline = time.monotonic() + WAIT_SECONDS
    for operation in operations:
        while True:
            print(
                f"AWS operation {operation['id']}: {operation.get('status', 'unknown')}", flush=True
            )
            if operation.get("status") in {"Succeeded", "Completed"}:
                break
            if operation.get("status") == "Failed":
                raise RuntimeError(
                    f"Lightsail operation {operation['id']} failed "
                    f"({operation.get('errorCode', 'unknown')}): "
                    f"{operation.get('errorDetails', '')}"
                )
            if time.monotonic() >= deadline:
                _show(config, "get_operation", {"operationId": operation["id"]})
                raise TimeoutError("AWS operation is still pending; inspect it before retrying")
            time.sleep(min(5, max(0, deadline - time.monotonic())))
            operation = client.get_operation(operationId=operation["id"])["operation"]


def _perform(config, client, method, **parameters):
    _show(config, method, parameters)
    try:
        response = getattr(client, method)(**parameters)
        _wait_operations(config, client, response)
    except BaseException:
        print(
            f"Stopped at {method.replace('_', '-')}. Run cloud-status and inspect this "
            "request in Lightsail before retrying. No later runbook step was run.",
            file=sys.stderr, flush=True,
        )
        raise
    print(f"Completed AWS {method.replace('_', '-')}", flush=True)


def _start_creation(config, client, state, kind, name):
    if _read(client, kind, name) is not None:
        raise RuntimeError(f"{kind} {name!r} already exists; inspect cloud-status; nothing changed")
    if state.get("resources", {}).get(kind, {}).get("arn"):
        raise RuntimeError(f"Recorded {kind} is missing; inspect the receipt before recreating it")
    state.setdefault("resources", {})[kind] = {"name": name}
    state.pop("destroyed", None)
    _save(config, state)


def _finish_creation(config, client, state, kind, name):
    resource = _read(client, kind, name)
    if not resource or not resource.get("arn"):
        raise RuntimeError(f"Cannot read created {kind}; inspect cloud-status before retrying")
    state["resources"][kind] = {"name": name, "arn": resource["arn"]}
    _save(config, state)
    print(f"Recorded {kind}: {name} ({resource['arn']})", flush=True)
    return state


def create_key(config):
    state = load_state(config)
    client, account = _client(config, state)
    if not state:
        state = {
            "version": 1, "name": config.name, "region": config.region,
            "hostname": config.hostname, "account_id": account,
            "instance_name": config.name, "key_pair_name": config.name + "-key",
            "static_ip_name": config.name + "-ip", "ssh_user": "ubuntu", "resources": {},
        }
    name = state["key_pair_name"]
    _start_creation(config, client, state, "key_pair", name)
    if state.get("private_key") and not Path(state["private_key"]).is_file():
        raise RuntimeError("The recorded private key is missing; restore it before proceeding")
    private = ensure_ssh_key(config)
    if state.get("private_key") and Path(state["private_key"]).resolve() != private:
        raise RuntimeError("Private-key path does not match the saved deployment")
    key_hash = hashlib.sha256(private.with_suffix(".pub").read_bytes()).hexdigest()
    if state.get("key_sha256", key_hash) != key_hash:
        raise RuntimeError("Local SSH key differs from the deployment's imported key")
    state.update(private_key=str(private), key_sha256=key_hash)
    _save(config, state)
    # Lightsail accepts OpenSSH public-key text despite this parameter's name.
    _perform(config, client, "import_key_pair", keyPairName=name,
             publicKeyBase64=private.with_suffix(".pub").read_text(encoding="ascii").strip())
    return _finish_creation(config, client, state, "key_pair", name)


def create_instance(config):
    state = load_state(config)
    if not state.get("private_key") or not Path(state["private_key"]).is_file():
        raise RuntimeError("The recorded private key is missing; restore it before create-instance")
    client, _ = _client(config, state)
    key = _recorded(client, state, "key_pair")
    name = state["instance_name"]
    _start_creation(config, client, state, "instance", name)
    _perform(
        config, client, "create_instances", instanceNames=[name],
        availabilityZone=config.availability_zone or config.region + "a",
        blueprintId=config.blueprint_id, bundleId=config.bundle_id,
        keyPairName=key["name"], ipAddressType="ipv4",
    )
    return _finish_creation(config, client, state, "instance", name)


def open_ports(config):
    state = load_state(config)
    client, _ = _client(config, state)
    instance = _recorded(client, state, "instance")
    _perform(config, client, "put_instance_public_ports", instanceName=instance["name"], portInfos=[
        {"fromPort": 22, "toPort": 22, "protocol": "tcp", "cidrs": list(config.ssh_cidrs)},
        {"fromPort": 80, "toPort": 80, "protocol": "tcp", "cidrs": ["0.0.0.0/0"]},
        {"fromPort": 443, "toPort": 443, "protocol": "tcp", "cidrs": ["0.0.0.0/0"]},
    ])
    return state


def allocate_ip(config):
    state = load_state(config)
    if not state:
        raise RuntimeError("Run create-key first to record the deployment names")
    client, _ = _client(config, state)
    name = state["static_ip_name"]
    _start_creation(config, client, state, "static_ip", name)
    _perform(config, client, "allocate_static_ip", staticIpName=name)
    return _finish_creation(config, client, state, "static_ip", name)


def attach_ip(config):
    state = load_state(config)
    client, _ = _client(config, state)
    instance = _recorded(client, state, "instance")
    address = _recorded(client, state, "static_ip")
    if address.get("isAttached"):
        raise RuntimeError(
            f"Static IP is already attached to {address.get('attachedTo')}; inspect cloud-status"
        )
    _perform(config, client, "attach_static_ip", staticIpName=address["name"],
             instanceName=instance["name"])
    state["ip"] = address["ipAddress"]
    _save(config, state)
    print(f"Host address: {state['ip']}. Next: trust-host, then setup.", flush=True)
    return state


def _delete(config, confirmation, kind, method, parameter):
    if confirmation != config.name:
        raise ValueError("Deletion requires the exact deployment NAME as confirmation")
    state = load_state(config)
    client, _ = _client(config, state)
    resource = _recorded(client, state, kind)
    if kind == "static_ip" and resource.get("isAttached"):
        if resource.get("attachedTo") != state.get("instance_name"):
            raise RuntimeError("Static IP is attached to another instance; inspect cloud-status")
        raise RuntimeError("Static IP is still attached; run delete-instance before release-ip")
    _perform(config, client, method, **{parameter: resource["name"]})
    del state["resources"][kind]
    if kind in {"instance", "static_ip"}:
        state.pop("ip", None)
    _save(config, state)
    return state


def delete_instance(config, confirmation):
    return _delete(config, confirmation, "instance", "delete_instance", "instanceName")


def release_ip(config, confirmation):
    return _delete(config, confirmation, "static_ip", "release_static_ip", "staticIpName")


def delete_key(config, confirmation):
    return _delete(config, confirmation, "key_pair", "delete_key_pair", "keyPairName")


def status(config):
    state = load_state(config)
    if not state:
        return {"name": config.name, "region": config.region, "resources": {}}
    client, _ = _client(config, state)
    resources = {}
    for kind, entry in state.get("resources", {}).items():
        method, parameter, _ = READS[kind]
        _show(config, method, {parameter: entry["name"]})
        resource = _read(client, kind, entry["name"])
        resources[kind] = {
            "name": entry["name"], "exists": resource is not None,
            "matches_receipt": bool(resource and entry.get("arn") == resource.get("arn")),
        }
        if resource:
            resources[kind].update({key: resource[key] for key in
                                   ("arn", "state", "ipAddress", "attachedTo") if key in resource})
    return {"name": config.name, "region": config.region, "account_id": state["account_id"],
            "ip": state.get("ip"), "hostname": config.hostname, "resources": resources}


def trust_host(config):
    """Read authenticated host keys once; never print the temporary AWS private key."""
    state = load_state(config)
    if not state.get("ip"):
        raise RuntimeError("Run attach-ip before trust-host")
    client, _ = _client(config, state)
    instance = _recorded(client, state, "instance")
    parameters = {"instanceName": instance["name"], "protocol": "ssh"}
    _show(config, "get_instance_access_details", parameters,
          query="accessDetails.{ipAddress:ipAddress,hostKeys:hostKeys}")
    try:
        details = client.get_instance_access_details(**parameters)["accessDetails"]
    except Exception:
        print("Could not fetch host keys; inspect cloud-status, then retry trust-host.",
              file=sys.stderr, flush=True)
        raise
    if details.get("ipAddress") != state["ip"] or not details.get("hostKeys"):
        raise RuntimeError("Host keys are not ready for the saved IP; retry trust-host later")
    lines = []
    for key in details["hostKeys"]:
        algorithm = key.get("algorithm", "")
        if not re.fullmatch(r"ssh-(?:rsa|ed25519)|ecdsa-sha2-nistp(?:256|384|521)", algorithm):
            continue
        parts = key.get("publicKey", "").split()
        encoded = (
            parts[1] if len(parts) >= 2 and parts[0] == algorithm else (parts[0] if parts else "")
        )
        try:
            serialization.load_ssh_public_key(f"{algorithm} {encoded}".encode("ascii"))
        except (ValueError, TypeError, UnicodeError):
            raise RuntimeError("AWS returned an invalid SSH host key; nothing was trusted")
        lines.append(f"{state['ip']} {algorithm} {encoded}\n")
    if not lines:
        raise RuntimeError("AWS has no supported host key; inspect it, then retry trust-host")
    path = config.state_dir / "known_hosts"
    temporary = path.with_suffix(".tmp")
    temporary.write_text("".join(lines), encoding="utf-8")
    temporary.replace(path)
    print(f"Verified SSH host keys saved to {path}", flush=True)
    return path
