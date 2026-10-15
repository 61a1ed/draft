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
import ssl
import stat
import subprocess
import sys
import tarfile
import tempfile
import time
import urllib.request
from pathlib import Path
from urllib.parse import unquote, urlsplit

NAME = re.compile(r"[a-z][a-z0-9-]{0,19}")
VERSION = re.compile(r"[0-9]+\.[0-9]+\.[0-9]+")
CADDY_CONFIG = Path("/etc/caddy/caddy.json")
CADDY_DROPIN = Path("/etc/systemd/system/caddy.service.d/config.conf")
UV = Path("/usr/local/bin/uv")
ETC = Path("/etc")
PG_DIRECTORY = Path("/etc/postgresql/16/main")
DATABASE_PORT = 5432
TLS_FILES = ("server.key", "server.crt", "ca.crt")
PG_HBA = b"""# Managed by this dedicated application deployment.
local   all             postgres                    peer
host    draft           dl_gateway  127.0.0.1/32     scram-sha-256
hostssl postgres,draft  postgres    0.0.0.0/0        cert
hostssl draft           dl_importer 0.0.0.0/0        cert
host    all             all         0.0.0.0/0        reject
host    all             all         ::/0             reject
"""


def run(*args, **kwargs):
    command = [str(arg) for arg in args]
    print("+ " + shlex.join(command), file=sys.stderr, flush=True)
    return subprocess.run(command, check=True, **kwargs)


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


def validate_name(name: str):
    if not isinstance(name, str) or not NAME.fullmatch(name):
        raise ValueError("Invalid application name")
    if name in {"root", "ubuntu", "caddy", "postgres", "nobody", "daemon"}:
        raise ValueError("Application name conflicts with an operating-system account")


def validate(config: dict) -> dict:
    validate_name(config.get("name", ""))
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
    for field in ("uv_version", "node_version"):
        if not isinstance(config.get(field), str) or not VERSION.fullmatch(config[field]):
            raise ValueError(f"{field} must be an exact three-part version")
    if int(config["node_version"].split(".")[0]) != 24:
        raise ValueError("This bootstrap supports pinned Node.js 24 LTS releases")
    values = config.get("app_environment")
    if not isinstance(values, dict):
        raise ValueError("app_environment must be an object")
    environment_text(values)
    database = urlsplit(values.get("DATABASE_URL", ""))
    password = config.get("gateway_password", unquote(database.password or ""))
    if (
        database.scheme != "postgresql"
        or database.hostname != "127.0.0.1"
        or database.port not in {None, DATABASE_PORT}
        or database.username != "dl_gateway"
        or database.path != "/draft"
        or database.query
        or database.fragment
        or not isinstance(password, str)
        or not password
        or unquote(database.password or "") != password
    ):
        raise ValueError(
            "DATABASE_URL must use the supplied local draft gateway credentials on port 5432"
        )
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
        installed = run(UV, "--version", stdout=subprocess.PIPE, text=True).stdout.split()
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
    installed = run(UV, "--version", stdout=subprocess.PIPE, text=True).stdout.split()
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
        run(root / "bin/node", "--version", stdout=subprocess.PIPE, text=True).stdout.strip()
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
            stdout=subprocess.PIPE,
            text=True,
        ).stdout.strip()
        != "true"
    ):
        raise RuntimeError("Existing deployment repository is not bare")
    for filename in ("bootstrap.py", "release.py", "start"):
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


def validate_tls_source(directory: Path) -> dict[str, bytes]:
    """Check the uploaded server material without trusting hostnames or using the network."""
    if directory.is_symlink() or not directory.is_dir():
        raise RuntimeError("Upload the server TLS files in the tls directory")
    if {path.name for path in directory.iterdir()} != set(TLS_FILES):
        raise RuntimeError("The tls directory must contain only server.key, server.crt, and ca.crt")
    for filename in TLS_FILES:
        path = directory / filename
        if path.is_symlink() or not path.is_file():
            raise RuntimeError("Server TLS inputs must be regular files")
    try:
        server = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        server.load_cert_chain(
            directory / "server.crt", directory / "server.key", password=lambda: b""
        )
        client = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        client.check_hostname = False
        client.load_verify_locations(cafile=directory / "ca.crt")
        server_input, server_output = ssl.MemoryBIO(), ssl.MemoryBIO()
        client_input, client_output = ssl.MemoryBIO(), ssl.MemoryBIO()
        server_connection = server.wrap_bio(server_input, server_output, server_side=True)
        client_connection = client.wrap_bio(client_input, client_output)
        completed = set()
        # An in-memory handshake checks trust, expiry, server usage and the key
        # pair, while deliberately performing the same verify-ca policy as libpq.
        for _ in range(20):
            for connection, output, peer_input in (
                (client_connection, client_output, server_input),
                (server_connection, server_output, client_input),
            ):
                if connection not in completed:
                    try:
                        connection.do_handshake()
                        completed.add(connection)
                    except ssl.SSLWantReadError:
                        pass
                peer_input.write(output.read())
            if len(completed) == 2:
                return {filename: (directory / filename).read_bytes() for filename in TLS_FILES}
        raise RuntimeError("Server TLS validation did not finish")
    except (OSError, ValueError) as error:
        raise RuntimeError(
            "Server TLS files failed certificate, trust, or private-key validation"
        ) from error


def postgres_settings(name: str) -> dict[str, str]:
    directory = ETC / "postgresql" / f"{name}-tls"
    return {
        "listen_addresses": "0.0.0.0",
        "port": str(DATABASE_PORT),
        "ssl": "on",
        "ssl_cert_file": str(directory / "server.crt"),
        "ssl_key_file": str(directory / "server.key"),
        "ssl_ca_file": str(directory / "ca.crt"),
        "hba_file": str(PG_DIRECTORY / "pg_hba.conf"),
        "password_encryption": "scram-sha-256",
    }


def validate_existing_ca(name: str, tls: dict[str, bytes]):
    path = ETC / "postgresql" / f"{name}-tls" / "ca.crt"
    if path.is_symlink():
        raise RuntimeError("Refusing a symbolic-link PostgreSQL CA certificate")
    if path.exists() and path.read_bytes() != tls["ca.crt"]:
        raise RuntimeError(
            "The uploaded CA differs from this host's trusted CA; restore the original "
            "workstation TLS files. Host commands do not rotate the deployment CA."
        )


def postgres_state(*, reload=False) -> dict:
    query = """SELECT json_build_object(
        'settings', (SELECT json_object_agg(name, setting) FROM pg_settings
            WHERE name IN ('listen_addresses', 'port', 'ssl', 'ssl_cert_file',
                'ssl_key_file', 'ssl_ca_file', 'hba_file', 'password_encryption')),
        'pending_restart', (SELECT bool_or(pending_restart) FROM pg_settings),
        'hba_errors', (SELECT count(*) FROM pg_hba_file_rules WHERE error IS NOT NULL),
        'loaded_at', pg_conf_load_time()::text,
        'reload', RELOAD_EXPRESSION);"""
    result = run(
        "runuser", "-u", "postgres", "--", "psql", "-X", "--no-password",
        "--host=/var/run/postgresql", f"--port={DATABASE_PORT}", "--dbname=postgres",
        "--username=postgres",
        "--tuples-only", "--no-align", "--set=ON_ERROR_STOP=1", "--command",
        query.replace("RELOAD_EXPRESSION", "pg_reload_conf()" if reload else "false"),
        stdout=subprocess.PIPE, text=True, cwd="/",
    )
    return json.loads(result.stdout)


def check_database_tls(name: str):
    """Assert the running cluster is safe before opening the public database port."""
    validate_name(name)
    directory = ETC / "postgresql" / f"{name}-tls"
    validate_tls_source(directory)
    import grp

    key = (directory / "server.key").stat()
    if (
        key.st_uid != 0
        or key.st_gid != grp.getgrnam("postgres").gr_gid
        or stat.S_IMODE(key.st_mode) != 0o640
    ):
        raise RuntimeError("PostgreSQL server.key must be root:postgres with mode 0640")
    hba = PG_DIRECTORY / "pg_hba.conf"
    if hba.is_symlink() or hba.read_bytes() != PG_HBA:
        raise RuntimeError("PostgreSQL authentication rules differ from the required mTLS policy")
    state = postgres_state()

    def check_state(current):
        if current["settings"] != postgres_settings(name) or current["pending_restart"]:
            raise RuntimeError("Running PostgreSQL settings differ; run install-db-certs again")
        if current["hba_errors"]:
            raise RuntimeError("PostgreSQL authentication configuration contains errors")

    check_state(state)
    # pg_hba_file_rules describes the file on disk. Explicitly reload and wait
    # for PostgreSQL to process it before allowing the firewall to be opened.
    if not postgres_state(reload=True)["reload"]:
        raise RuntimeError("PostgreSQL could not reload its authentication rules")
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        current = postgres_state()
        check_state(current)
        if current["loaded_at"] != state["loaded_at"]:
            print("PostgreSQL mTLS configuration verified on port 5432.", flush=True)
            return
        time.sleep(0.1)
    raise RuntimeError("PostgreSQL did not finish reloading its authentication rules")


def configure_postgres(config: dict, tls: dict[str, bytes]):
    name = config["name"]
    validate_existing_ca(name, tls)
    if not (PG_DIRECTORY / "postgresql.conf").is_file():
        raise RuntimeError(
            "Expected PostgreSQL 16/main cluster is missing; no replacement was initialized"
        )
    directory = ETC / "postgresql" / f"{name}-tls"
    if directory.is_symlink():
        raise RuntimeError("Refusing a symbolic-link TLS directory")
    directory.mkdir(parents=True, exist_ok=True)
    run("chown", "root:postgres", directory)
    directory.chmod(0o750)
    settings = "# Managed by this dedicated application deployment.\n" + "".join(
        f"{key} = '{value}'\n" for key, value in postgres_settings(name).items()
    )
    files = {
        PG_DIRECTORY / "conf.d" / f"99-{name}.conf": (settings.encode(), 0o644),
        PG_DIRECTORY / "pg_hba.conf": (PG_HBA, 0o644),
        **{
            directory / filename: (tls[filename], 0o640 if filename == "server.key" else 0o644)
            for filename in TLS_FILES
        },
    }
    marker = ETC / name / "postgresql-restart-required"
    for path, (content, _) in files.items():
        if path.is_symlink():
            raise RuntimeError(f"Refusing a symbolic-link PostgreSQL configuration file: {path}")
        if not path.exists() or path.read_bytes() != content:
            atomic_write(marker, b"configuration changed\n", 0o600)
    for path, (content, mode) in files.items():
        atomic_write(path, content, mode)
        run("chown", "root:postgres", path)
    run("systemctl", "enable", "postgresql")
    run("systemctl", "restart" if marker.exists() else "start", "postgresql@16-main.service")
    check_database_tls(name)
    marker.unlink(missing_ok=True)


def configure_services(config: dict, caddy_restart: bool, tls: dict[str, bytes]):
    name = config["name"]
    configure_postgres(config, tls)
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


def install_db_certs(name: str, directory: Path):
    """Renew server material for an existing deployment without redoing host setup."""
    validate_name(name)
    tls = validate_tls_source(directory)
    if not (ETC / "postgresql" / f"{name}-tls" / "ca.crt").is_file():
        raise RuntimeError("Host CA is missing; run setup before installing renewed certificates")
    validate_existing_ca(name, tls)
    configure_postgres({"name": name}, tls)
    print("PostgreSQL server certificates installed and verified.", flush=True)


def bootstrap(config: dict, source: Path):
    print("Checking host configuration and saved credentials...", flush=True)
    validate(config)
    host_preflight()
    tls = validate_tls_source(source / "tls")
    validate_existing_ca(config["name"], tls)
    for filename in ("bootstrap.py", "release.py", "start"):
        if not (source / filename).is_file():
            raise RuntimeError("Upload bootstrap.py, release.py, and start together")
    # Check ownership and persisted secrets before changing OS state.
    save_configuration(config, ETC / config["name"])
    print("Installing operating system packages (PostgreSQL and Caddy)...", flush=True)
    install_packages()
    print("Preparing application accounts...", flush=True)
    restart_caddy = ensure_accounts(config["name"])
    print("Installing uv...", flush=True)
    install_uv(config["uv_version"])
    print("Installing Node.js...", flush=True)
    install_node(config["node_version"])
    print("Preparing application directories and deployment scripts...", flush=True)
    install_layout(config, source)
    print("Installing Python 3.12 for the application account...", flush=True)
    # runuser preserves the caller's cwd, which may be an inaccessible SSH home.
    # Host runtime installation must not discover configuration from that home.
    run(
        "runuser", "-u", config["name"], "--",
        UV, "--no-config", "python", "install", "3.12",
        cwd=f"/var/lib/{config['name']}",
    )
    print("Configuring and starting PostgreSQL and Caddy services...", flush=True)
    configure_services(config, restart_caddy, tls)
    print(
        "Host prepared. Database initialization and release activation remain explicit steps.",
        flush=True,
    )


def main():
    if (
        len(sys.argv) == 3 and sys.argv[1] == "--check-database-tls"
        or len(sys.argv) == 4 and sys.argv[1] == "--install-db-certs"
    ):
        host_preflight()
        name = sys.argv[2]
        validate_name(name)
        import fcntl

        with open(f"/run/{name}-deploy.lock", "w") as lock:
            print("Waiting for the host deployment lock...", flush=True)
            fcntl.flock(lock, fcntl.LOCK_EX)
            if sys.argv[1] == "--install-db-certs":
                install_db_certs(name, Path(sys.argv[3]))
            else:
                check_database_tls(name)
        return
    if len(sys.argv) != 2:
        raise SystemExit(
            "Usage: sudo python3 bootstrap.py HOST_CONFIG.json | --check-database-tls NAME "
            "| --install-db-certs NAME TLS_DIR"
        )
    config = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
    validate(config)
    host_preflight()
    import fcntl

    with open(f"/run/{config['name']}-deploy.lock", "w") as lock:
        print("Waiting for the host deployment lock...", flush=True)
        fcntl.flock(lock, fcntl.LOCK_EX)
        bootstrap(config, Path(__file__).resolve().parent)


if __name__ == "__main__":
    main()
