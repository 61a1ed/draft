import os
from datetime import timedelta
from types import SimpleNamespace

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import serialization
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

import tls


@pytest.fixture
def config(tmp_path):
    # No hostname, address, cloud state, or network access is needed for certificate setup.
    return SimpleNamespace(name="example", state_dir=tmp_path / "private")


@pytest.fixture(autouse=True)
def permissions(monkeypatch):
    monkeypatch.setattr(tls, "_protect_directory", lambda path: None)


def _read(directory):
    return {name: (directory / name).read_bytes() for name in tls.FILES}


def _cert(directory, name):
    return x509.load_pem_x509_certificate((directory / f"{name}.crt").read_bytes())


def test_generate_without_address_or_dns_and_reuse_without_changes(config):
    directory = tls.ensure_certificates(config)
    original = _read(directory)
    ca = _cert(directory, "ca")
    assert set(path.name for path in directory.iterdir()) == set(tls.FILES)
    assert ca.subject.get_attributes_for_oid(NameOID.COMMON_NAME)[0].value == "example database CA"
    assert ca.extensions.get_extension_for_class(x509.BasicConstraints).value == (
        x509.BasicConstraints(ca=True, path_length=0)
    )
    for identity in ("server", "postgres", "dl_importer"):
        certificate = _cert(directory, identity)
        certificate.verify_directly_issued_by(ca)
        with pytest.raises(x509.ExtensionNotFound):
            certificate.extensions.get_extension_for_class(x509.SubjectAlternativeName)
        purpose = (
            ExtendedKeyUsageOID.SERVER_AUTH if identity == "server"
            else ExtendedKeyUsageOID.CLIENT_AUTH
        )
        assert certificate.extensions.get_extension_for_class(x509.ExtendedKeyUsage).value == (
            x509.ExtendedKeyUsage([purpose])
        )
        assert not certificate.extensions.get_extension_for_class(x509.BasicConstraints).value.ca
        assert certificate.public_key().key_size == 3072
    assert tls.validate_certificates(config) == directory
    assert tls.ensure_certificates(config) == directory
    assert _read(directory) == original


@pytest.mark.parametrize("renew", [False, True])
def test_partial_bundle_never_replaces_the_ca(config, renew):
    directory = tls.ensure_certificates(config)
    original = _read(directory)
    (directory / "server.key").unlink()
    with pytest.raises(RuntimeError, match="Incomplete.*server.key"):
        tls.ensure_certificates(config, renew=renew)
    assert (directory / "ca.key").read_bytes() == original["ca.key"]
    assert (directory / "ca.crt").read_bytes() == original["ca.crt"]
    assert not (directory / "server.key").exists()


def test_empty_directory_requires_explicit_recovery(config):
    (config.state_dir / "tls").mkdir(parents=True)
    with pytest.raises(RuntimeError, match="Incomplete"):
        tls.ensure_certificates(config)


def test_missing_entire_bundle_never_replaces_the_pinned_ca(config):
    directory = tls.ensure_certificates(config)
    fingerprint = (config.state_dir / "tls-ca.sha256").read_bytes()
    directory.rename(config.state_dir / "saved-tls")
    with pytest.raises(RuntimeError, match="restore the original deployment CA"):
        tls.ensure_certificates(config)
    assert not directory.exists()
    assert (config.state_dir / "tls-ca.sha256").read_bytes() == fingerprint


def test_replaced_complete_bundle_is_rejected_by_ca_pin(config, tmp_path):
    directory = tls.ensure_certificates(config)
    other = SimpleNamespace(name=config.name, state_dir=tmp_path / "other")
    replacement = tls.ensure_certificates(other)
    for name in tls.FILES:
        (directory / name).write_bytes((replacement / name).read_bytes())
    with pytest.raises(RuntimeError, match="fingerprint mismatch"):
        tls.ensure_certificates(config, renew=True)
    with pytest.raises(RuntimeError, match="fingerprint mismatch"):
        tls.validate_client_certificates(config)


def test_full_validation_pins_existing_bundle_and_client_validation_does_not(config):
    directory = tls.ensure_certificates(config)
    pin = config.state_dir / "tls-ca.sha256"
    original = pin.read_bytes()
    pin.unlink()
    assert tls.validate_client_certificates(config) == directory
    assert not pin.exists()
    tls.validate_certificates(config)
    assert pin.read_bytes() == original


def test_validation_and_renewal_never_create_missing_certificates(config):
    with pytest.raises(RuntimeError, match="create-certs"):
        tls.validate_certificates(config)
    with pytest.raises(RuntimeError, match="restore the existing CA"):
        tls.ensure_certificates(config, renew=True)
    assert not config.state_dir.exists()


def test_renew_expired_leaves_keeps_ca_and_private_keys(config, monkeypatch):
    directory = tls.ensure_certificates(config)
    original = _read(directory)
    later = tls._now() + timedelta(days=366)
    monkeypatch.setattr(tls, "_now", lambda: later)
    with pytest.raises(RuntimeError, match="server.crt.*expired"):
        tls.ensure_certificates(config)
    tls.ensure_certificates(config, renew=True)
    assert tls.validate_certificates(config) == directory
    renewed = _read(directory)
    for name in tls.FILES:
        if name.startswith("ca.") or name.endswith(".key"):
            assert renewed[name] == original[name]
        else:
            assert renewed[name] != original[name]
            assert _cert(directory, name.removesuffix(".crt")).not_valid_after_utc > later


def test_expired_ca_cannot_be_implicitly_replaced_or_renewed(config, monkeypatch):
    directory = tls.ensure_certificates(config)
    original = _read(directory)
    later = tls._now() + timedelta(days=3651)
    monkeypatch.setattr(tls, "_now", lambda: later)
    with pytest.raises(RuntimeError, match="ca.crt.*expired"):
        tls.ensure_certificates(config, renew=True)
    assert _read(directory) == original


def test_mismatched_key_fails_before_any_replacement(config):
    directory = tls.ensure_certificates(config)
    (directory / "server.key").write_bytes((directory / "dl_importer.key").read_bytes())
    original = _read(directory)
    with pytest.raises(RuntimeError, match="mismatched.*server.key"):
        tls.ensure_certificates(config, renew=True)
    assert _read(directory) == original


def test_client_with_wrong_role_is_rejected(config):
    directory = tls.ensure_certificates(config)
    for suffix in ("crt", "key"):
        (directory / f"postgres.{suffix}").write_bytes(
            (directory / f"dl_importer.{suffix}").read_bytes()
        )
    with pytest.raises(RuntimeError, match="postgres.crt.*identity"):
        tls.validate_client_certificates(config, "postgres")


def test_untrusted_client_has_correct_role_but_wrong_signer(config, tmp_path):
    directory = tls.ensure_certificates(config)
    cert_path, key_path = tls.create_untrusted_client(tmp_path / "untrusted")
    cert = x509.load_pem_x509_certificate(cert_path.read_bytes())
    assert cert.subject == tls._name("postgres")
    assert cert.extensions.get_extension_for_class(x509.ExtendedKeyUsage).value == (
        x509.ExtendedKeyUsage([ExtendedKeyUsageOID.CLIENT_AUTH])
    )
    key = serialization.load_pem_private_key(key_path.read_bytes(), password=None)
    assert key.public_key().public_numbers() == cert.public_key().public_numbers()
    (directory / "postgres.crt").write_bytes(cert_path.read_bytes())
    (directory / "postgres.key").write_bytes(key_path.read_bytes())
    with pytest.raises(RuntimeError, match="Invalid TLS certificate postgres.crt"):
        tls.validate_client_certificates(config, "postgres")


def test_client_validation_requires_only_public_ca_and_selected_client(config):
    directory = tls.ensure_certificates(config)
    for name in tls.FILES:
        if name not in {"ca.crt", "dl_importer.crt", "dl_importer.key"}:
            (directory / name).unlink()
    assert tls.validate_client_certificates(config) == directory
    with pytest.raises(ValueError, match="role"):
        tls.validate_client_certificates(config, "dl_gateway")


def test_certificate_from_another_deployment_is_rejected(config):
    tls.ensure_certificates(config)
    config.name = "another"
    with pytest.raises(RuntimeError, match="ca.crt.*identity"):
        tls.validate_certificates(config)


def test_future_certificates_are_rejected(config, monkeypatch):
    tls.ensure_certificates(config)
    earlier = tls._now() - timedelta(days=1)
    monkeypatch.setattr(tls, "_now", lambda: earlier)
    with pytest.raises(RuntimeError, match="not yet valid"):
        tls.validate_certificates(config)


def test_client_certificate_cannot_be_used_as_ca(config):
    directory = tls.ensure_certificates(config)
    ca_key = serialization.load_pem_private_key((directory / "ca.key").read_bytes(), password=None)
    invalid = tls._issue("example database CA", ca_key, ca_key, None)
    (directory / "ca.crt").write_bytes(tls._cert_bytes(invalid))
    with pytest.raises(RuntimeError, match="CA constraints"):
        tls.validate_certificates(config)


def test_interrupted_initial_generation_leaves_no_partial_bundle(config, monkeypatch):
    def fail(*args, **kwargs):
        raise OSError("disk failure")

    monkeypatch.setattr(tls, "_write", fail)
    with pytest.raises(OSError, match="disk failure"):
        tls.ensure_certificates(config)
    assert not (config.state_dir / "tls").exists()
    assert not list(config.state_dir.iterdir())


def test_permissions_are_applied_before_writing_private_material(config, monkeypatch):
    calls = []
    monkeypatch.setattr(tls, "_protect_directory", lambda path: calls.append(path))
    write = tls._write

    def checked_write(path, data):
        assert path.parent in calls
        return write(path, data)

    monkeypatch.setattr(tls, "_write", checked_write)
    tls.ensure_certificates(config)


# Save the real function before the autouse permission fixture replaces it.
protect_directory = tls._protect_directory


def test_private_directory_permissions(tmp_path, monkeypatch):
    directory = tmp_path / "private"
    directory.mkdir()
    key_path = directory / "ca.key"
    key_path.write_bytes(b"placeholder")
    if os.name == "nt":
        calls = []
        monkeypatch.setattr(tls.subprocess, "run", lambda command, **kwargs: calls.append(
            (command, kwargs)
        ))
        protect_directory(directory)
        command, kwargs = calls[0]
        assert command[:3] == ["powershell.exe", "-NoProfile", "-NonInteractive"]
        assert "SetAccessRuleProtection($true, $false)" in command[-1]
        assert "DirectorySecurity" in command[-1]
        assert "FileSecurity" in command[-1]
        assert kwargs["env"]["DRAFT_TLS_DIRECTORY"] == str(directory.resolve())
        assert kwargs["check"] is True
    else:
        protect_directory(directory)
        assert directory.stat().st_mode & 0o777 == 0o700
        assert key_path.stat().st_mode & 0o777 == 0o600


@pytest.mark.skipif(os.name == "nt", reason="Windows symlink creation needs extra privileges")
def test_key_symlinks_are_rejected(config, tmp_path):
    directory = tls.ensure_certificates(config)
    path = directory / "server.key"
    target = tmp_path / "key"
    path.rename(target)
    path.symlink_to(target)
    with pytest.raises(RuntimeError, match="symbolic links"):
        tls.validate_certificates(config)
