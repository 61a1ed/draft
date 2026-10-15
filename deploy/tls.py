"""Deployment-local PostgreSQL certificates for address-independent verify-ca TLS."""

import os
import subprocess
import tempfile
from datetime import UTC, datetime, timedelta
from pathlib import Path

from cryptography import x509
from cryptography.exceptions import InvalidSignature, UnsupportedAlgorithm
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

from config import Config

IDENTITIES = ("ca", "server", "postgres", "dl_importer")
FILES = tuple(f"{name}.{suffix}" for name in IDENTITIES for suffix in ("key", "crt"))
CA_LIFETIME = timedelta(days=3650)
CERTIFICATE_LIFETIME = timedelta(days=365)


def _now() -> datetime:
    return datetime.now(UTC)


def _is_link(path: Path) -> bool:
    return path.is_symlink() or path.is_junction()


def _protect_directory(directory: Path) -> None:
    """Restrict the directory and its regular files to this operating-system user."""
    if _is_link(directory):
        raise RuntimeError("The TLS directory must not be a symbolic link or junction")
    if os.name == "nt":
        # A fresh DACL also removes explicit grants, unlike /inheritance:r alone.
        # Pass the path as data so spaces and quotes cannot become PowerShell code.
        script = """$ErrorActionPreference = 'Stop'
$identity = [System.Security.Principal.WindowsIdentity]::GetCurrent().User
$directoryAcl = [System.Security.AccessControl.DirectorySecurity]::new()
$directoryAcl.SetAccessRuleProtection($true, $false)
$directoryAcl.AddAccessRule([System.Security.AccessControl.FileSystemAccessRule]::new(
    $identity, 'FullControl', 'ContainerInherit,ObjectInherit', 'None', 'Allow'))
[System.IO.Directory]::SetAccessControl($env:DRAFT_TLS_DIRECTORY, $directoryAcl)
$fileAcl = [System.Security.AccessControl.FileSecurity]::new()
$fileAcl.SetAccessRuleProtection($true, $false)
$fileAcl.AddAccessRule([System.Security.AccessControl.FileSystemAccessRule]::new(
    $identity, 'FullControl', 'Allow'))
foreach ($file in [System.IO.Directory]::GetFiles($env:DRAFT_TLS_DIRECTORY)) {
    if (-not ([System.IO.File]::GetAttributes($file) -band
        [System.IO.FileAttributes]::ReparsePoint)) {
        [System.IO.File]::SetAccessControl($file, $fileAcl)
    }
}
"""
        try:
            subprocess.run(
                ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", script],
                check=True, capture_output=True, text=True, timeout=30,
                env={**os.environ, "DRAFT_TLS_DIRECTORY": str(directory.resolve())},
            )
        except (OSError, subprocess.SubprocessError) as error:
            raise RuntimeError("Cannot restrict local TLS file permissions") from error
    else:
        directory.chmod(0o700)
        for path in directory.iterdir():
            if path.is_file() and not _is_link(path):
                path.chmod(0o600)


def _name(common_name: str) -> x509.Name:
    return x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, common_name)])


def _common_name(config: Config, identity: str) -> str:
    if identity == "ca":
        return f"{config.name} database CA"
    if identity == "server":
        return f"{config.name} database server"
    return identity


def _usage(ca: bool) -> x509.KeyUsage:
    return x509.KeyUsage(
        digital_signature=not ca, content_commitment=False, key_encipherment=not ca,
        data_encipherment=False, key_agreement=False, key_cert_sign=ca, crl_sign=ca,
        encipher_only=False, decipher_only=False,
    )


def _issue(common_name, key, issuer_key, issuer, *, ca=False, server=False):
    now = _now()
    before = now - timedelta(minutes=5)
    after = now + (CA_LIFETIME if ca else CERTIFICATE_LIFETIME)
    if issuer is not None:
        before = max(before, issuer.not_valid_before_utc)
        after = min(after, issuer.not_valid_after_utc)
    subject = _name(common_name)
    builder = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(issuer.subject if issuer is not None else subject)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(before)
        .not_valid_after(after)
        .add_extension(x509.BasicConstraints(ca=ca, path_length=0 if ca else None), critical=True)
        .add_extension(_usage(ca), critical=True)
        .add_extension(x509.SubjectKeyIdentifier.from_public_key(key.public_key()), critical=False)
        .add_extension(
            x509.AuthorityKeyIdentifier.from_issuer_public_key(issuer_key.public_key()),
            critical=False,
        )
    )
    if not ca:
        purpose = ExtendedKeyUsageOID.SERVER_AUTH if server else ExtendedKeyUsageOID.CLIENT_AUTH
        builder = builder.add_extension(x509.ExtendedKeyUsage([purpose]), critical=False)
    return builder.sign(issuer_key, hashes.SHA256())


def _new_key():
    return rsa.generate_private_key(public_exponent=65537, key_size=3072)


def _write(path: Path, data: bytes) -> None:
    with os.fdopen(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "wb") as handle:
        handle.write(data)


def _key_bytes(key) -> bytes:
    return key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.TraditionalOpenSSL,
        serialization.NoEncryption(),
    )


def _cert_bytes(cert) -> bytes:
    return cert.public_bytes(serialization.Encoding.PEM)


def _pin_ca(config, certificate, *, create=False) -> None:
    """Keep a public CA fingerprint outside tls/ to detect a lost or replaced bundle."""
    path = config.state_dir / "tls-ca.sha256"
    fingerprint = certificate.fingerprint(hashes.SHA256()).hex()
    if _is_link(path):
        raise RuntimeError("The TLS CA fingerprint must not be a symbolic link or junction")
    if not path.exists() and not create:
        return
    try:
        if create and not path.exists():
            try:
                with path.open("x", encoding="ascii") as handle:
                    handle.write(fingerprint + "\n")
            except FileExistsError:
                pass
        if path.read_text(encoding="ascii").strip() != fingerprint:
            raise RuntimeError("TLS CA fingerprint mismatch; restore the original deployment CA")
    except (OSError, UnicodeError) as error:
        raise RuntimeError("Cannot read or record the deployment TLS CA fingerprint") from error


def _require_files(directory: Path, names) -> None:
    if _is_link(directory) or not directory.is_dir():
        raise RuntimeError("Missing or invalid local TLS directory; run create-certs first")
    missing = [name for name in names if not (directory / name).is_file()]
    if missing:
        raise RuntimeError(
            "Incomplete TLS certificate set; restore the missing files: " + ", ".join(missing)
        )
    if any(_is_link(directory / name) for name in names):
        raise RuntimeError("TLS certificate files must not be symbolic links or junctions")


def _load_cert(path: Path):
    try:
        return x509.load_pem_x509_certificate(path.read_bytes())
    except (ValueError, OSError, UnsupportedAlgorithm) as error:
        raise RuntimeError(f"Cannot read valid TLS certificate {path.name}") from error


def _load_key(path: Path, cert):
    try:
        key = serialization.load_pem_private_key(path.read_bytes(), password=None)
        if not isinstance(key, rsa.RSAPrivateKey) or key.key_size < 2048:
            raise ValueError("Expected an RSA key of at least 2048 bits")
        if key.public_key().public_numbers() != cert.public_key().public_numbers():
            raise ValueError("Certificate and private key do not match")
        return key
    except (ValueError, TypeError, OSError, UnsupportedAlgorithm) as error:
        raise RuntimeError(f"Invalid or mismatched TLS private key {path.name}") from error


def _validate_cert(config, identity, cert, issuer, *, allow_expired=False):
    try:
        if cert.subject != _name(_common_name(config, identity)):
            raise ValueError("Certificate identity does not match this deployment and role")
        if not isinstance(cert.public_key(), rsa.RSAPublicKey) or cert.public_key().key_size < 2048:
            raise ValueError("Expected an RSA certificate of at least 2048 bits")
        cert.verify_directly_issued_by(issuer)
        ca = identity == "ca"
        expected = x509.BasicConstraints(ca=ca, path_length=0 if ca else None)
        constraints = cert.extensions.get_extension_for_class(x509.BasicConstraints)
        usage = cert.extensions.get_extension_for_class(x509.KeyUsage)
        if constraints.value != expected or not constraints.critical:
            raise ValueError("Incorrect certificate CA constraints")
        if usage.value != _usage(ca) or not usage.critical:
            raise ValueError("Incorrect certificate key usage")
        if not ca:
            purpose = (
                ExtendedKeyUsageOID.SERVER_AUTH if identity == "server"
                else ExtendedKeyUsageOID.CLIENT_AUTH
            )
            if cert.extensions.get_extension_for_class(x509.ExtendedKeyUsage).value != (
                x509.ExtendedKeyUsage([purpose])
            ):
                raise ValueError("Incorrect certificate authentication purpose")
        now = _now()
        if cert.not_valid_before_utc > now:
            raise ValueError("Certificate is not yet valid; check the workstation clock")
        if not allow_expired and cert.not_valid_after_utc <= now:
            if ca:
                raise ValueError("Deployment CA has expired; a separate CA trust update is needed")
            raise ValueError("Certificate has expired; explicitly renew the leaf certificates")
    except (
        ValueError, TypeError, InvalidSignature, UnsupportedAlgorithm,
        x509.ExtensionNotFound, x509.DuplicateExtension,
    ) as error:
        raise RuntimeError(f"Invalid TLS certificate {identity}.crt: {error}") from error


def _load_bundle(config, *, allow_expired=False):
    directory = config.state_dir / "tls"
    _require_files(directory, FILES)
    _protect_directory(directory)
    certificates = {identity: _load_cert(directory / f"{identity}.crt") for identity in IDENTITIES}
    keys = {}
    for identity, cert in certificates.items():
        _validate_cert(
            config, identity, cert, certificates["ca"],
            allow_expired=allow_expired and identity != "ca",
        )
        keys[identity] = _load_key(directory / f"{identity}.key", cert)
    _pin_ca(config, certificates["ca"], create=True)
    return keys, certificates


def validate_certificates(config: Config) -> Path:
    """Require a complete valid bundle; never create or replace a CA implicitly."""
    _load_bundle(config)
    return config.state_dir / "tls"


def validate_client_certificates(config: Config, role: str = "dl_importer") -> Path:
    """Validate the public CA and selected local client without server/CA private keys."""
    if role not in {"postgres", "dl_importer"}:
        raise ValueError("The database certificate role must be postgres or dl_importer")
    return _validate_leaf(config, role)


def validate_server_certificates(config: Config) -> Path:
    """Require only the server certificate/key and public CA for host installation."""
    return _validate_leaf(config, "server")


def _validate_leaf(config: Config, role: str) -> Path:
    directory = config.state_dir / "tls"
    _require_files(directory, ("ca.crt", f"{role}.crt", f"{role}.key"))
    _protect_directory(directory)
    ca = _load_cert(directory / "ca.crt")
    _validate_cert(config, "ca", ca, ca)
    _pin_ca(config, ca)
    cert = _load_cert(directory / f"{role}.crt")
    _validate_cert(config, role, cert, ca)
    _load_key(directory / f"{role}.key", cert)
    return directory


def ensure_certificates(config: Config, *, renew: bool = False) -> Path:
    """Create once, reuse after validation, or explicitly renew leaves under the same CA."""
    directory = config.state_dir / "tls"
    if directory.exists() or _is_link(directory):
        keys, certificates = _load_bundle(config, allow_expired=renew)
        if renew:
            # Retain keys, so an interrupted renewal leaves every certificate/key pair valid.
            renewed = {
                identity: _issue(
                    _common_name(config, identity), keys[identity], keys["ca"], certificates["ca"],
                    server=identity == "server",
                )
                for identity in IDENTITIES if identity != "ca"
            }
            for identity, cert in renewed.items():
                temporary = directory / f".{identity}-{cert.serial_number}.crt.tmp"
                _write(temporary, _cert_bytes(cert))
                temporary.replace(directory / f"{identity}.crt")
        return directory
    if renew:
        raise RuntimeError("Cannot renew missing TLS certificates; restore the existing CA first")
    if (config.state_dir / "tls-ca.sha256").exists() or _is_link(
        config.state_dir / "tls-ca.sha256"
    ):
        raise RuntimeError("TLS directory is missing; restore the original deployment CA and files")
    config.state_dir.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".tls-", dir=config.state_dir) as temporary:
        stage = Path(temporary) / "tls"
        stage.mkdir(mode=0o700)
        _protect_directory(stage)
        keys = {identity: _new_key() for identity in IDENTITIES}
        ca = _issue(_common_name(config, "ca"), keys["ca"], keys["ca"], None, ca=True)
        certificates = {"ca": ca}
        certificates.update({
            identity: _issue(
                _common_name(config, identity), keys[identity], keys["ca"], ca,
                server=identity == "server",
            )
            for identity in IDENTITIES if identity != "ca"
        })
        for identity in IDENTITIES:
            _write(stage / f"{identity}.key", _key_bytes(keys[identity]))
            _write(stage / f"{identity}.crt", _cert_bytes(certificates[identity]))
        # A concurrent initial setup must never replace another run's CA.
        stage.rename(directory)
    _pin_ca(config, ca, create=True)
    return directory


def create_untrusted_client(directory: Path) -> tuple[Path, Path]:
    """Create a disposable self-signed postgres client for a negative authentication test."""
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    _protect_directory(directory)
    key = _new_key()
    certificate = _issue("postgres", key, key, None)
    certificate_path, key_path = directory / "untrusted.crt", directory / "untrusted.key"
    _write(certificate_path, _cert_bytes(certificate))
    _write(key_path, _key_bytes(key))
    return certificate_path, key_path
