"""Install this application's host on a dedicated Ubuntu 24.04 x86-64 server.

Run as root with a private host.json produced by the workstation controller.
This script provisions the OS and services; database/schema initialization and
release activation are separate, explicit operations. It never resets data.
"""

from __future__ import annotations

import hashlib
import json
import os
import platform
import re
import shlex
import shutil
import subprocess
import sys
import tarfile
import tempfile
import urllib.request
from pathlib import Path
from urllib.parse import unquote, urlsplit

NAME = re.compile(r"[a-z][a-z0-9-]{0,19}")
VERSION = re.compile(r"[0-9]+\.[0-9]+\.[0-9]+")
CADDY_CONFIG = Path("/etc/caddy/caddy.json")
CADDY_DROPIN = Path("/etc/systemd/system/caddy.service.d/config.conf")
UV = Path("/usr/local/bin/uv")


def run(*args, **kwargs):
    return subprocess.run([str(arg) for arg in args], check=True, **kwargs)


def atomic_write(path: Path, content: bytes, mode=0o644) -> bool:
    """Keep complete files visible, including when provisioning is interrupted."""
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.is_symlink():
        raise RuntimeError(f"Refusing a symbolic-link configuration file: {path}")
    if path.exists() and path.read_bytes() == content:
        path.chmod(mode)
        return False
    descriptor, temporary = tempfile.mkstemp(prefix=".setup-", dir=path.parent)
    try:
        os.chmod(temporary, mode)
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(content)
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)
    return True


def environment_text(values: dict[str, str]) -> str:
    lines = []
    for name, value in sorted(values.items()):
        if not re.fullmatch(r"[A-Z][A-Z0-9_]*", name):
            raise ValueError("Application environment contains an invalid variable name")
        if not isinstance(value, str) or any(ord(c) < 32 for c in value):
            raise ValueError("Application environment values must be single-line strings")
        escaped = value.replace("\\", "\\\\").replace('"', '\\"')
        lines.append(f'{name}="{escaped}"')
    return "\n".join(lines) + "\n"


def read_environment(path: Path) -> dict[str, str]:
    result = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line or line.startswith("#"):
            continue
        name, value = line.split("=", 1)
        parts = shlex.split(value, posix=True)
        if len(parts) != 1:
            raise ValueError("Invalid saved application environment")
        result[name] = parts[0]
    return result


def validate(config: dict) -> dict:
    name = config.get("name", "")
    if not isinstance(name, str) or not NAME.fullmatch(name):
        raise ValueError("Invalid application name")
    if name in {"root", "ubuntu", "caddy", "postgres", "nobody", "daemon"}:
        raise ValueError("Application name conflicts with an operating-system account")
    hostname = config.get("hostname", "")
    if (
        not isinstance(hostname, str)
        or len(hostname) > 253
        or not re.fullmatch(r"[a-z0-9](?:[a-z0-9.-]*[a-z0-9])?", hostname)
        or "." not in hostname
    ):
        raise ValueError("A DNS hostname is required")
    for label in hostname.split("."):
        if not 1 <= len(label) <= 63 or label.startswith("-") or label.endswith("-"):
            raise ValueError("Invalid hostname label")
    callback = config.get("callback_path", "")
    reserved = (
        "/api",
        "/images",
        "/assets",
        "/ready",
        "/healthz",
        "/docs",
        "/redoc",
        "/openapi.json",
        "/auth/login",
        "/auth/logout",
        "/auth/me",
        "/auth/test",
    )
    if (
        not isinstance(callback, str)
        or not re.fullmatch(r"/[A-Za-z0-9._~-]+(?:/[A-Za-z0-9._~-]+)*", callback)
        or any(part in {".", ".."} for part in callback.split("/"))
        or callback == "/auth"
        or any(callback == path or callback.startswith(path + "/") for path in reserved)
    ):
        raise ValueError("OIDC callback must be a non-conflicting root-relative path")
    for field in ("uv_version", "node_version"):
        if not isinstance(config.get(field), str) or not VERSION.fullmatch(config[field]):
            raise ValueError(f"{field} must be an exact three-part version")
    if int(config["node_version"].split(".")[0]) != 24:
        raise ValueError("This bootstrap supports pinned Node.js 24 LTS releases")
    values = config.get("app_environment")
    if not isinstance(values, dict):
        raise ValueError("app_environment must be an object")
    environment_text(values)
    required = {
        "PUBLIC_BASE_URL": f"https://{hostname}",
        "APP_ENV": "production",
        "COOKIE_SECURE": "true",
        "OIDC_ISSUER": "https://accounts.google.com",
        "OIDC_CALLBACK_PATH": callback,
    }
    if any(values.get(key) != value for key, value in required.items()):
        raise ValueError("Application origin, environment, or OIDC settings are inconsistent")
    if values.get("TEST_LOGIN_ENABLED", "false") != "false":
        raise ValueError("Fixture login must be disabled on this host")
    if len(values.get("SESSION_SECRET", "")) < 32:
        raise ValueError("SESSION_SECRET must contain at least 32 characters")
    if not values.get("OIDC_CLIENT_ID") or not values.get("OIDC_CLIENT_SECRET"):
        raise ValueError("Google OIDC client credentials are required")
    database = urlsplit(values.get("DATABASE_URL", ""))
    password = config.get("gateway_password", unquote(database.password or ""))
    if (
        database.scheme != "postgresql"
        or database.hostname != "127.0.0.1"
        or database.port not in {None, 5432}
        or database.username != "dl_gateway"
        or database.path != "/draft"
        or database.query
        or database.fragment
        or not isinstance(password, str)
        or not password
        or unquote(database.password or "") != password
    ):
        raise ValueError("DATABASE_URL must use the supplied local draft gateway credentials")
    return config


def host_preflight():
    if os.geteuid() != 0:
        raise RuntimeError("Run bootstrap.py through sudo")
    release = dict(
        line.split("=", 1)
        for line in Path("/etc/os-release").read_text().splitlines()
        if "=" in line
    )
    if (
        release.get("ID", "").strip('"') != "ubuntu"
        or release.get("VERSION_ID", "").strip('"') != "24.04"
    ):
        raise RuntimeError("Host setup supports Ubuntu 24.04 only")
    if platform.machine() != "x86_64":
        raise RuntimeError("Host setup supports the Lightsail x86-64 architecture only")


def download(url: str, target: Path):
    with urllib.request.urlopen(url, timeout=120) as source, target.open("wb") as destination:
        shutil.copyfileobj(source, destination)


def verify_checksum(archive: Path, checksums: str):
    expected = None
    for line in checksums.splitlines():
        fields = line.split()
        if len(fields) == 1 and re.fullmatch(r"[0-9a-fA-F]{64}", fields[0]):
            expected = fields[0]
        elif len(fields) == 2 and fields[1].lstrip("*") == archive.name:
            expected = fields[0]
    if expected is None or not re.fullmatch(r"[0-9a-fA-F]{64}", expected):
        raise RuntimeError("Official checksum list did not contain the requested archive")
    with archive.open("rb") as stream:
        actual = hashlib.file_digest(stream, "sha256").hexdigest()
    if actual != expected.lower():
        raise RuntimeError("Downloaded runtime archive failed its SHA-256 check")


def install_packages():
    env = {**os.environ, "DEBIAN_FRONTEND": "noninteractive"}
    apt = ("apt-get", "-o", "DPkg::Lock::Timeout=300")
    run(*apt, "update", env=env)
    run(
        *apt,
        "install",
        "--yes",
        "--no-upgrade",
        "ca-certificates",
        "curl",
        "git",
        "gnupg",
        "xz-utils",
        "debian-keyring",
        "debian-archive-keyring",
        "apt-transport-https",
        "postgresql-16",
        "postgresql-client-16",
        env=env,
    )
    with tempfile.TemporaryDirectory(prefix="caddy-install-") as temporary:
        temporary = Path(temporary)
        download("https://dl.cloudsmith.io/public/caddy/stable/gpg.key", temporary / "key.asc")
        run(
            "gpg",
            "--batch",
            "--yes",
            "--dearmor",
            "--output",
            temporary / "key.gpg",
            temporary / "key.asc",
        )
        atomic_write(
            Path("/usr/share/keyrings/caddy-stable-archive-keyring.gpg"),
            (temporary / "key.gpg").read_bytes(),
        )
        download(
            "https://dl.cloudsmith.io/public/caddy/stable/debian.deb.txt", temporary / "caddy.list"
        )
        atomic_write(
            Path("/etc/apt/sources.list.d/caddy-stable.list"),
            (temporary / "caddy.list").read_bytes(),
        )
    run(*apt, "update", env=env)
    run(*apt, "install", "--yes", "--no-upgrade", "caddy", env=env)


def install_uv(version: str):
    if UV.exists():
        installed = run(UV, "--version", capture_output=True, text=True).stdout.split()
        if len(installed) >= 2 and installed[1] == version:
            return
    filename = "uv-x86_64-unknown-linux-gnu.tar.gz"
    base = f"https://github.com/astral-sh/uv/releases/download/{version}/"
    with tempfile.TemporaryDirectory(prefix="uv-install-") as temporary:
        temporary = Path(temporary)
        archive = temporary / filename
        download(base + filename, archive)
        download(base + filename + ".sha256", temporary / "checksum")
        verify_checksum(archive, (temporary / "checksum").read_text())
        with tarfile.open(archive) as source:
            source.extractall(temporary / "unpacked", filter="data")
        executable = temporary / "unpacked" / "uv-x86_64-unknown-linux-gnu" / "uv"
        atomic_write(UV, executable.read_bytes(), 0o755)
    installed = run(UV, "--version", capture_output=True, text=True).stdout.split()
    if len(installed) < 2 or installed[1] != version:
        raise RuntimeError("Installed uv version does not match its requested pin")


def install_node(version: str):
    dirname = f"node-v{version}-linux-x64"
    root = Path("/opt") / dirname
    if not root.exists():
        with tempfile.TemporaryDirectory(prefix=".node-install-", dir="/opt") as temporary:
            temporary = Path(temporary)
            filename = dirname + ".tar.xz"
            base = f"https://nodejs.org/dist/v{version}/"
            download(base + filename, temporary / filename)
            download(base + "SHASUMS256.txt", temporary / "checksums")
            verify_checksum(temporary / filename, (temporary / "checksums").read_text())
            with tarfile.open(temporary / filename) as source:
                source.extractall(temporary / "unpacked", filter="data")
            (temporary / "unpacked" / dirname).rename(root)
    if (
        run(root / "bin/node", "--version", capture_output=True, text=True).stdout.strip()
        != f"v{version}"
    ):
        raise RuntimeError("Installed Node.js version does not match its requested pin")
    for command in ("node", "npm", "npx"):
        target = root / "bin" / command
        if not target.is_file():
            raise RuntimeError("Node.js installation is incomplete; inspect /opt before retrying")
        link = Path("/usr/local/bin") / command
        if link.exists() and not link.is_symlink():
            raise RuntimeError(f"Refusing to replace an unmanaged runtime executable: {link}")
        temporary_link = link.with_name(f".{command}-setup-{os.getpid()}")
        try:
            temporary_link.symlink_to(target)
            os.replace(temporary_link, link)
        finally:
            temporary_link.unlink(missing_ok=True)


def ensure_accounts(name: str) -> bool:
    import grp
    import pwd

    group = f"{name}-web"
    try:
        grp.getgrnam(group)
    except KeyError:
        run("groupadd", "--system", group)
    try:
        account = pwd.getpwnam(name)
    except KeyError:
        run(
            "useradd",
            "--system",
            "--user-group",
            "--create-home",
            "--home-dir",
            f"/var/lib/{name}",
            "--shell",
            "/usr/sbin/nologin",
            name,
        )
        account = pwd.getpwnam(name)
    if account.pw_dir != f"/var/lib/{name}" or account.pw_shell != "/usr/sbin/nologin":
        raise RuntimeError("Existing application account is not owned by this deployment")
    # PostgreSQL administration executes the app's prepared interpreter. Only
    # the app owns its home; other users need traversal to its uv Python runtime.
    Path(account.pw_dir).chmod(0o755)
    caddy = pwd.getpwnam("caddy")
    web = grp.getgrnam(group)
    needs_restart = "caddy" not in web.gr_mem and caddy.pw_gid != web.gr_gid
    if needs_restart:
        run("usermod", "--append", "--groups", group, "caddy")
    return needs_restart


def install_layout(config: dict, source: Path):
    name = config["name"]
    for path in (
        f"/srv/{name}",
        f"/srv/{name}/releases",
        f"/srv/{name}/assets",
        f"/srv/{name}/imports",
        f"/usr/local/lib/{name}",
    ):
        run("install", "-d", "-m", "0755", path)
    repository = Path(f"/srv/{name}/repo.git")
    if not repository.exists():
        run("install", "-d", "-m", "0755", "-o", "ubuntu", "-g", "ubuntu", repository)
    if not (repository / "HEAD").exists():
        if any(repository.iterdir()):
            raise RuntimeError("Bare repository directory contains unexpected files")
        run("runuser", "-u", "ubuntu", "--", "git", "init", "--bare", repository)
    if (
        run(
            "runuser",
            "-u",
            "ubuntu",
            "--",
            "git",
            f"--git-dir={repository}",
            "rev-parse",
            "--is-bare-repository",
            capture_output=True,
            text=True,
        ).stdout.strip()
        != "true"
    ):
        raise RuntimeError("Existing deployment repository is not bare")
    for filename in ("release.py", "database.py", "start"):
        content = (source / filename).read_bytes().replace(b"\r\n", b"\n")
        atomic_write(Path(f"/usr/local/lib/{name}") / filename, content, 0o755)


def save_configuration(config: dict, directory: Path):
    directory.mkdir(parents=True, exist_ok=True)
    directory.chmod(0o700)
    stored = {key: config[key] for key in ("name", "hostname", "callback_path")}
    stored["repo_owner"] = "ubuntu"
    config_path = directory / "deploy.json"
    if config_path.exists():
        existing = json.loads(config_path.read_text())
        if any(existing.get(key) != value for key, value in stored.items()):
            raise RuntimeError("Existing host identity differs; use a fresh dedicated instance")
    environment = directory / "app.env"
    if environment.exists():
        saved = read_environment(environment)
        for key in ("DATABASE_URL", "SESSION_SECRET"):
            if saved.get(key) != config["app_environment"].get(key):
                raise RuntimeError(
                    "Saved credentials differ; restore the workstation secrets before retrying"
                )
    atomic_write(environment, environment_text(config["app_environment"]).encode(), 0o600)
    atomic_write(config_path, (json.dumps(stored, indent=2) + "\n").encode(), 0o600)


def initial_caddy(hostname: str) -> dict:
    return {
        "admin": {"listen": "127.0.0.1:2019"},
        "apps": {
            "http": {
                "servers": {
                    "public": {
                        "listen": [":443"],
                        "routes": [
                            {
                                "match": [{"host": [hostname]}],
                                "handle": [
                                    {
                                        "@id": "app",
                                        "handler": "static_response",
                                        "status_code": 503,
                                        "body": "No release deployed yet\n",
                                    }
                                ],
                                "terminal": True,
                            }
                        ],
                    }
                }
            }
        },
    }


def ensure_caddy_config(hostname: str, path: Path = CADDY_CONFIG) -> bool:
    if path.exists():
        config = json.loads(path.read_text())
        route = config["apps"]["http"]["servers"]["public"]["routes"][0]
        if route["match"] != [{"host": [hostname]}]:
            raise RuntimeError("Existing Caddy configuration belongs to another hostname")
        return False
    return atomic_write(path, (json.dumps(initial_caddy(hostname), indent=2) + "\n").encode())


def service_unit(name: str) -> str:
    return f"""[Unit]
Description=Draft application release %i
After=network.target postgresql@16-main.service
StartLimitIntervalSec=0

[Service]
Type=exec
User={name}
Group={name}-web
WorkingDirectory=/srv/{name}/releases/%i/draft_backend
Environment=PYTHONUNBUFFERED=1
EnvironmentFile=/etc/{name}/app.env
RuntimeDirectory={name}-%i
RuntimeDirectoryMode=0750
UMask=0007
ExecStart=/usr/local/lib/{name}/start
Restart=always
RestartSec=2
TimeoutStopSec=10

[Install]
WantedBy=multi-user.target
"""


def configure_services(config: dict, caddy_restart: bool):
    name = config["name"]
    pg_directory = Path("/etc/postgresql/16/main")
    if not (pg_directory / "postgresql.conf").is_file():
        raise RuntimeError(
            "Expected PostgreSQL 16/main cluster is missing; no replacement was initialized"
        )
    changed = atomic_write(
        pg_directory / "conf.d" / f"99-{name}.conf",
        (
            "# Managed by this dedicated application deployment.\n"
            "listen_addresses = '127.0.0.1'\n"
            "password_encryption = 'scram-sha-256'\n"
        ).encode(),
    )
    run("systemctl", "enable", "postgresql")
    run("systemctl", "restart" if changed else "start", "postgresql@16-main.service")
    atomic_write(Path(f"/etc/systemd/system/{name}@.service"), service_unit(name).encode())
    caddy_restart |= ensure_caddy_config(config["hostname"])
    caddy_restart |= atomic_write(
        CADDY_DROPIN,
        b"""[Service]
ExecStart=
ExecStart=/usr/bin/caddy run --environ --config /etc/caddy/caddy.json
ExecReload=
ExecReload=/usr/bin/caddy reload --config /etc/caddy/caddy.json
""",
    )
    run("/usr/bin/caddy", "validate", "--config", CADDY_CONFIG)
    run("systemctl", "daemon-reload")
    run("systemctl", "enable", "caddy")
    run("systemctl", "restart" if caddy_restart else "start", "caddy")


def bootstrap(config: dict, source: Path):
    validate(config)
    host_preflight()
    # Check ownership and persisted secrets before changing OS state.
    save_configuration(config, Path(f"/etc/{config['name']}"))
    for filename in ("release.py", "database.py", "start"):
        if not (source / filename).is_file():
            raise RuntimeError("Upload bootstrap.py, release.py, database.py, and start together")
    install_packages()
    restart_caddy = ensure_accounts(config["name"])
    install_uv(config["uv_version"])
    install_node(config["node_version"])
    install_layout(config, source)
    run(UV, "python", "install", "3.12")
    run("runuser", "-u", config["name"], "--", UV, "python", "install", "3.12")
    configure_services(config, restart_caddy)
    print("Host prepared. Database initialization and release activation remain explicit steps.")


def main():
    if len(sys.argv) != 2:
        raise SystemExit("Usage: sudo python3 bootstrap.py HOST_CONFIG.json")
    config = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
    validate(config)
    host_preflight()
    import fcntl

    with open(f"/run/{config['name']}-deploy.lock", "w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        bootstrap(config, Path(__file__).resolve().parent)


if __name__ == "__main__":
    main()
