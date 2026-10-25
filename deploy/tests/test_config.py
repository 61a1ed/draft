import json
from pathlib import Path

import pytest

from config import load_config


def settings(tmp_path, **overrides):
    values = {
        "NAME": "example",
        "REGION": "eu-west-1",
        "HOSTNAME": "draft.example.com",
        "SSH_CIDRS": ["192.0.2.10/32"],
        "CATALOG_CSV": "catalog.csv",
        "IMAGE_ROOT": "images",
        "GOOGLE_OIDC_JSON": json.dumps(
            {
                "web": {
                    "client_id": "fake-client",
                    "client_secret": "never-print-this-secret",
                    "redirect_uris": ["https://draft.example.com/oauth2/callback"],
                }
            }
        ),
    }
    values.update(overrides)
    path = tmp_path / "settings.py"
    path.write_text(
        "\n".join(f"{key} = {value!r}" for key, value in values.items()), encoding="utf-8"
    )
    return path


def test_configuration_is_portable_and_hides_credentials(tmp_path):
    config = load_config(settings(tmp_path, STATE_DIR="state", AWS_PROFILE="preproduction"))
    assert config.region == "eu-west-1"
    assert config.callback_path == "/oauth2/callback"
    assert config.oidc_redirect_uri == "https://draft.example.com/oauth2/callback"
    assert config.catalog_csv == tmp_path / "catalog.csv"
    assert config.state_dir == tmp_path / "state"
    assert config.aws_profile == "preproduction"
    assert config.oidc_client_secret == "never-print-this-secret"
    assert config.oidc_client_secret not in repr(config)
    assert config.oidc_client_id not in repr(config)


def test_default_state_stays_under_deploy_private(tmp_path):
    config = load_config(settings(tmp_path))
    assert config.state_dir == Path(__file__).resolve().parents[1] / "private" / "example"


def test_catalog_details_path_is_optional_and_relative_to_settings(tmp_path):
    assert load_config(settings(tmp_path), action="import-catalog").catalog_details is None
    config = load_config(
        settings(tmp_path, CATALOG_DETAILS="details.json"), action="import-catalog"
    )
    assert config.catalog_details == tmp_path / "details.json"


def test_legacy_automated_dns_setting_requires_manual_transition(tmp_path):
    with pytest.raises(ValueError, match="manage DNS manually"):
        load_config(settings(tmp_path, HOSTED_ZONE_ID="ZEXAMPLE"))
    load_config(settings(tmp_path, HOSTED_ZONE_ID=None))


def test_triplequoted_json_accepts_surrounding_newlines(tmp_path):
    document = json.dumps(
        {
            "web": {
                "client_id": "fake-client",
                "client_secret": "fake-secret",
                "redirect_uris": ["https://draft.example.com/callback"],
            }
        }
    )
    config = load_config(settings(tmp_path, GOOGLE_OIDC_JSON="\n  " + document + "\n"))
    assert config.callback_path == "/callback"


@pytest.mark.parametrize(
    "overrides",
    [
        {"REGION": "us-west2"},
        {"NAME": "../other"},
        {"HOSTNAME": "https://example.com"},
        {"SSH_CIDRS": []},
        {"SSH_CIDRS": "192.0.2.0/24"},
        {"SSH_CIDRS": ["2001:db8::/32"]},
        {"AVAILABILITY_ZONE": "us-east-1a"},
        {"UV_VERSION": "latest"},
        {"NAME": "postgres"},
    ],
)
def test_invalid_inputs_fail_before_cloud_calls(tmp_path, overrides):
    with pytest.raises(ValueError):
        load_config(settings(tmp_path, **overrides))


def test_oidc_requires_exact_registered_hostname(tmp_path):
    with pytest.raises(ValueError, match="Register one HTTPS"):
        load_config(settings(tmp_path, HOSTNAME="elsewhere.example.com"))


def test_oidc_selects_registered_callback_when_ambiguous(tmp_path):
    value = json.dumps(
        {
            "web": {
                "client_id": "fake",
                "client_secret": "fake-secret",
                "redirect_uris": ["https://draft.example.com/one", "https://draft.example.com/two"],
            }
        }
    )
    with pytest.raises(ValueError, match="OIDC_CALLBACK_PATH"):
        load_config(settings(tmp_path, GOOGLE_OIDC_JSON=value))
    assert (
        load_config(
            settings(tmp_path, GOOGLE_OIDC_JSON=value, OIDC_CALLBACK_PATH="/two")
        ).callback_path
        == "/two"
    )


def test_settings_exception_preserves_original_message(tmp_path):
    path = tmp_path / "settings.py"
    path.write_text("raise ValueError('secret-credential')", encoding="utf-8")
    with pytest.raises(ValueError, match="secret-credential"):
        load_config(path)


@pytest.mark.parametrize("path", ["/api/custom", "/auth/login", "/assets/callback", "/auth"])
def test_callback_route_policy_is_left_to_the_application(tmp_path, path):
    value = json.dumps(
        {
            "web": {
                "client_id": "fake",
                "client_secret": "fake-secret",
                "redirect_uris": ["https://draft.example.com" + path],
            }
        }
    )
    assert load_config(settings(tmp_path, GOOGLE_OIDC_JSON=value)).callback_path == path


@pytest.mark.parametrize("action", [
    "cloud-status", "delete-instance", "release-ip", "delete-key", "ssh", "logs", "status",
    "install-db-certs", "verify-db",
])
def test_maintenance_does_not_require_application_or_installation_inputs(tmp_path, action):
    config = load_config(settings(
        tmp_path, GOOGLE_OIDC_JSON="broken", SSH_CIDRS=[], CATALOG_CSV=None, IMAGE_ROOT=None,
        UV_VERSION="broken", NODE_VERSION="broken", BLUEPRINT_ID="broken", BUNDLE_ID=None,
        AVAILABILITY_ZONE="wrong-region",
    ), action=action)
    assert config.name == "example"


def test_firewall_update_requires_only_identity_cloud_access_and_ssh_cidrs(tmp_path):
    path = settings(tmp_path, GOOGLE_OIDC_JSON="broken", CATALOG_CSV=None, UV_VERSION="broken")
    assert load_config(path, action="open-ports").ssh_cidrs == ("192.0.2.10/32",)
    with pytest.raises(ValueError, match="SSH_CIDRS"):
        load_config(settings(tmp_path, SSH_CIDRS=[]), action="open-ports")


@pytest.mark.parametrize("action", ["create-certs", "renew-certs", "verify-db", "init-db"])
def test_standalone_certificate_and_database_steps_need_only_local_identity(tmp_path, action):
    path = tmp_path / "settings.py"
    path.write_text("NAME = 'example'\nSTATE_DIR = './state'\n")
    config = load_config(path, action=action, host="192.0.2.1")
    assert config.name == "example"
    assert config.state_dir == tmp_path / "state"


def test_explicit_database_address_ignores_unrelated_cloud_identity(tmp_path):
    path = settings(tmp_path, REGION="broken", HOSTNAME="broken", GOOGLE_OIDC_JSON="broken")
    assert load_config(path, action="verify-db", host="192.0.2.1").name == "example"
