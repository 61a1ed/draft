"""Load and validate the administrator's private Python configuration."""

import json
import re
import runpy
from dataclasses import dataclass, field
from ipaddress import ip_network
from pathlib import Path
from urllib.parse import urlsplit


@dataclass(frozen=True, kw_only=True)
class Config:
    name: str
    region: str
    hostname: str
    state_dir: Path
    ssh_cidrs: tuple[str, ...] = ()
    catalog_csv: Path | None = None
    image_root: Path | None = None
    oidc_client_id: str = field(default="", repr=False)
    oidc_client_secret: str = field(default="", repr=False)
    callback_path: str = ""
    availability_zone: str | None = None
    blueprint_id: str = "ubuntu_24_04"
    bundle_id: str = "small_3_0"
    aws_profile: str | None = None
    uv_version: str = "0.12.20"
    node_version: str = "24.22.0"

    @property
    def public_url(self) -> str:
        return f"https://{self.hostname}"

    @property
    def oidc_redirect_uri(self) -> str:
        return self.public_url + self.callback_path


def load_state(config: Config) -> dict:
    """Read the host receipt locally, including receipts from the old bringup command."""
    path = config.state_dir / "state.json"
    if not path.exists():
        return {}
    try:
        state = json.loads(path.read_text(encoding="utf-8"))
    except (ValueError, OSError):
        raise RuntimeError("Cannot read deployment state.json; restore it before proceeding")
    if (
        not isinstance(state, dict)
        or state.get("version") != 1
        or state.get("name") != config.name
        or state.get("region") != config.region
        or state.get("hostname") != config.hostname
    ):
        raise RuntimeError("Deployment state does not match NAME, REGION and HOSTNAME")
    return state


def _text(values: dict, key: str, default=None) -> str:
    value = values.get(key, default)
    if not isinstance(value, str) or not value.strip() or value != value.strip():
        raise ValueError(f"{key} must be a nonempty string without surrounding whitespace")
    return value


def _optional(values: dict, key: str) -> str | None:
    return None if values.get(key) is None else _text(values, key)


def _path(values: dict, key: str, base: Path, default=None) -> Path:
    value = values.get(key, default)
    if not isinstance(value, (str, Path)) or not str(value):
        raise ValueError(f"{key} must be a filesystem path")
    path = Path(value).expanduser()
    return (base / path).resolve() if not path.is_absolute() else path.resolve()


def _oidc(values: dict, hostname: str) -> tuple[str, str, str]:
    try:
        raw_json = values.get("GOOGLE_OIDC_JSON")
        if not isinstance(raw_json, str):
            raise ValueError("Expected a JSON string")
        document = json.loads(raw_json)
    except (ValueError, TypeError):
        raise ValueError("GOOGLE_OIDC_JSON must contain valid Google web-client JSON")
    web = document.get("web") if isinstance(document, dict) else None
    if not isinstance(web, dict):
        raise ValueError("GOOGLE_OIDC_JSON must describe a web application client")
    for key in ("client_id", "client_secret"):
        if not isinstance(web.get(key), str) or not web[key].strip():
            raise ValueError(f"GOOGLE_OIDC_JSON is missing web.{key}")
        if any(char in web[key] for char in "\r\n\0"):
            raise ValueError(f"GOOGLE_OIDC_JSON web.{key} contains a control character")
    uris = web.get("redirect_uris")
    if not isinstance(uris, list) or not all(isinstance(uri, str) for uri in uris):
        raise ValueError("GOOGLE_OIDC_JSON needs a list of web.redirect_uris")
    candidates = set()
    for uri in uris:
        try:
            parsed = urlsplit(uri)
            if (
                parsed.scheme == "https"
                and parsed.netloc == hostname
                and not parsed.query
                and not parsed.fragment
            ):
                candidates.add(parsed.path)
        except ValueError:
            continue
    selected = _optional(values, "OIDC_CALLBACK_PATH")
    if selected is None and len(candidates) == 1:
        selected = next(iter(candidates))
    if selected is None:
        raise ValueError(
            "Register one HTTPS redirect URI for HOSTNAME in Google, or select "
            "a registered path with OIDC_CALLBACK_PATH when there are several"
        )
    if selected not in candidates:
        raise ValueError("OIDC_CALLBACK_PATH must match a registered HTTPS redirect for HOSTNAME")
    return web["client_id"], web["client_secret"], selected


def load_config(path: str | Path, *, action="check", host=None) -> Config:
    """Read the deployment identity and validate only the selected step's inputs."""
    path = Path(path).resolve()
    values = runpy.run_path(str(path))
    name = _text(values, "NAME")
    if not re.fullmatch(r"[a-z][a-z0-9-]{0,18}[a-z0-9]|[a-z]", name):
        raise ValueError(
            "NAME must use 1–20 lowercase letters, digits or hyphens, starting "
            "with a letter and ending with a letter or digit"
        )
    if name in {"root", "ubuntu", "caddy", "postgres", "nobody", "daemon"}:
        raise ValueError("NAME must not collide with an existing system account")
    standalone = action in {"create-certs", "renew-certs"} or (
        host is not None and action in {"verify-db", "init-db", "import-catalog"}
    )
    region = "" if standalone else _text(values, "REGION")
    if region and not re.fullmatch(r"[a-z]{2}(?:-[a-z]+)+-\d+", region):
        raise ValueError("REGION must be an AWS region name, for example us-west-2")
    hostname = "" if standalone else _text(values, "HOSTNAME").lower()
    labels = hostname.split(".")
    if hostname and (
        len(hostname) > 253
        or len(labels) < 2
        or any(not re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", label) for label in labels)
    ):
        raise ValueError("HOSTNAME must be a DNS hostname without a scheme, port or path")
    options = {}
    if action in {"check", "open-ports"}:
        raw_cidrs = values.get("SSH_CIDRS")
        if not isinstance(raw_cidrs, (list, tuple)) or not raw_cidrs:
            raise ValueError("SSH_CIDRS must be a nonempty list of administrator IPv4 CIDRs")
        try:
            networks = [ip_network(value, strict=True) for value in raw_cidrs]
        except (TypeError, ValueError):
            raise ValueError("SSH_CIDRS contains an invalid network; use your public IPv4 /32")
        if any(network.version != 4 for network in networks):
            raise ValueError("SSH_CIDRS must contain IPv4 networks; this deployment uses IPv4 only")
        options["ssh_cidrs"] = tuple(str(network) for network in networks)
    if action == "check" and values.get("HOSTED_ZONE_ID") is not None:
        raise ValueError("HOSTED_ZONE_ID is no longer supported; remove it and manage DNS manually")
    if action in {"check", "create-instance"}:
        zone = _optional(values, "AVAILABILITY_ZONE")
        if zone and not re.fullmatch(re.escape(region) + r"[a-z]", zone):
            raise ValueError("AVAILABILITY_ZONE must belong to REGION")
        blueprint = _text(values, "BLUEPRINT_ID", Config.blueprint_id)
        if blueprint != Config.blueprint_id:
            raise ValueError("This host installer supports BLUEPRINT_ID ubuntu_24_04 only")
        bundle = _text(values, "BUNDLE_ID", Config.bundle_id)
        if not re.fullmatch(r"[A-Za-z0-9_]+", bundle):
            raise ValueError("BUNDLE_ID must be a Lightsail bundle identifier")
        options.update(availability_zone=zone, blueprint_id=blueprint, bundle_id=bundle)
    if action in {"check", "setup"}:
        for key in ("uv_version", "node_version"):
            version = _text(values, key.upper(), getattr(Config, key))
            if not re.fullmatch(r"\d+\.\d+\.\d+", version):
                raise ValueError("UV_VERSION and NODE_VERSION must be exact numeric x.y.z versions")
            options[key] = version
        client_id, client_secret, callback_path = _oidc(values, hostname)
        options.update(
            oidc_client_id=client_id, oidc_client_secret=client_secret, callback_path=callback_path,
        )
    if action in {"check", "import-catalog"}:
        options.update(
            catalog_csv=_path(values, "CATALOG_CSV", path.parent),
            image_root=_path(values, "IMAGE_ROOT", path.parent),
        )
    if action in {
        "check", "create-key", "create-instance", "open-ports", "allocate-ip", "attach-ip",
        "trust-host", "open-db-port", "cloud-status", "delete-instance", "release-ip", "delete-key",
    }:
        options["aws_profile"] = _optional(values, "AWS_PROFILE")
    return Config(
        name=name,
        region=region,
        hostname=hostname,
        state_dir=_path(values, "STATE_DIR", path.parent, Path(__file__).parent / "private" / name),
        **options,
    )
