import base64
from copy import deepcopy
from dataclasses import replace
from types import SimpleNamespace

import pytest
from botocore.exceptions import ClientError

import cloud
from config import Config


class FakeLightsail:
    def __init__(self):
        self.resources = {"key_pair": {}, "instance": {}, "static_ip": {}}
        self.created = []
        self.deleted = []
        self.fail_firewall_once = False
        self.firewall = None
        self.public_key = None
        self.bundle_ram = 2

    def _get(self, kind, name, response_key):
        if name not in self.resources[kind]:
            raise ClientError({"Error": {"Code": "NotFoundException", "Message": "missing"}}, "Get")
        return {response_key: deepcopy(self.resources[kind][name])}

    def get_instance(self, instanceName):
        return self._get("instance", instanceName, "instance")

    def get_key_pair(self, keyPairName):
        return self._get("key_pair", keyPairName, "keyPair")

    def get_static_ip(self, staticIpName):
        return self._get("static_ip", staticIpName, "staticIp")

    def get_blueprints(self, **kwargs):
        return {"blueprints": [{"blueprintId": "ubuntu_24_04", "platform": "LINUX_UNIX"}]}

    def get_bundles(self, **kwargs):
        return {
            "bundles": [
                {
                    "bundleId": "small_3_0",
                    "isActive": True,
                    "supportedPlatforms": ["LINUX_UNIX"],
                    "ramSizeInGb": self.bundle_ram,
                    "publicIpv4AddressCount": 1,
                }
            ]
        }

    def get_regions(self, **kwargs):
        return {
            "regions": [
                {
                    "name": "eu-west-1",
                    "availabilityZones": [{"zoneName": "eu-west-1a", "state": "available"}],
                }
            ]
        }

    def _create(self, kind, name, **extra):
        assert name not in self.resources[kind]
        self.created.append(kind)
        self.resources[kind][name] = {"name": name, "arn": f"arn:fake:{kind}:{name}", **extra}
        return {"operations": [{"id": f"create-{kind}", "status": "Succeeded"}]}

    def get_operation(self, operationId):
        kind = operationId.removeprefix("create-")
        resource = next(iter(self.resources[kind].values()))
        return {
            "operation": {
                "id": operationId,
                "resourceName": resource["name"],
                "status": "Succeeded",
            }
        }

    def import_key_pair(self, keyPairName, publicKeyBase64):
        self.public_key = base64.b64decode(publicKeyBase64)
        assert self.public_key.startswith(b"ssh-rsa ")
        return self._create("key_pair", keyPairName)

    def create_instances(self, **kwargs):
        assert kwargs["ipAddressType"] == "ipv4"
        return self._create(
            "instance",
            kwargs["instanceNames"][0],
            sshKeyName=kwargs["keyPairName"],
            tags=kwargs["tags"],
            state={"name": "running"},
        )

    def put_instance_public_ports(self, **kwargs):
        if self.fail_firewall_once:
            self.fail_firewall_once = False
            raise RuntimeError("injected interruption")
        self.firewall = kwargs["portInfos"]
        return {}

    def allocate_static_ip(self, staticIpName):
        return self._create("static_ip", staticIpName, ipAddress="192.0.2.42", isAttached=False)

    def attach_static_ip(self, staticIpName, instanceName):
        self.resources["static_ip"][staticIpName].update(isAttached=True, attachedTo=instanceName)
        return {}

    def detach_static_ip(self, staticIpName):
        self.resources["static_ip"][staticIpName].update(isAttached=False)
        return {}

    def _delete(self, kind, name):
        self.deleted.append(kind)
        del self.resources[kind][name]
        return {}

    def delete_instance(self, instanceName):
        for resource in self.resources["static_ip"].values():
            if resource.get("attachedTo") == instanceName:
                resource.update(isAttached=False)
        return self._delete("instance", instanceName)

    def release_static_ip(self, staticIpName):
        return self._delete("static_ip", staticIpName)

    def delete_key_pair(self, keyPairName):
        return self._delete("key_pair", keyPairName)

    def get_instance_access_details(self, **kwargs):
        return {
            "accessDetails": {
                "ipAddress": "192.0.2.42",
                "privateKey": "DO-NOT-PERSIST",
                "hostKeys": [
                    {"algorithm": "ssh-rsa", "publicKey": self.public_key.decode().strip()}
                ],
            }
        }


@pytest.fixture
def environment(tmp_path, monkeypatch):
    config = Config(
        name="example",
        region="eu-west-1",
        hostname="draft.example.com",
        ssh_cidrs=("192.0.2.1/32",),
        catalog_csv=tmp_path / "catalog.csv",
        image_root=tmp_path / "images",
        state_dir=tmp_path / "private",
        oidc_client_id="fake-client",
        oidc_client_secret="fake-secret",
        callback_path="/callback",
    )
    lightsail = FakeLightsail()
    requested_clients = []
    account = {"Account": "123456789012"}
    clients = {
        "lightsail": lightsail,
        "sts": SimpleNamespace(get_caller_identity=lambda: account),
    }

    def client(name):
        requested_clients.append(name)
        return clients[name]

    monkeypatch.setattr(cloud, "_session", lambda config: SimpleNamespace(client=client))
    # Keep test temporary directories accessible to pytest cleanup on Windows.
    monkeypatch.setattr(cloud, "protect_ssh_key", lambda *args, **kwargs: None)
    return config, lightsail, requested_clients, account


def test_completed_provision_retry_preserves_key_and_updates_firewall(environment):
    config, lightsail, requested_clients, _ = environment
    cloud.provision(config)
    key_before = (config.state_dir / "id_rsa").read_bytes()
    state = cloud.provision(replace(config, ssh_cidrs=("192.0.2.2/32",)))
    assert state["ip"] == "192.0.2.42"
    assert lightsail.created == ["key_pair", "instance", "static_ip"]
    assert (config.state_dir / "id_rsa").read_bytes() == key_before
    assert lightsail.firewall[0]["cidrs"] == ["192.0.2.2/32"]
    assert {port["fromPort"] for port in lightsail.firewall} == {22, 80, 443}
    assert "fake-secret" not in (config.state_dir / "state.json").read_text()
    assert set(requested_clients) == {"sts", "lightsail"}
    assert all(set(entry) == {"name", "arn"} for entry in state["resources"].values())


def test_partial_provision_requires_cleanup_before_retry(environment):
    config, lightsail, requested_clients, _ = environment
    lightsail.fail_firewall_once = True
    with pytest.raises(RuntimeError, match="Provisioning is incomplete"):
        cloud.provision(config)
    key_before = (config.state_dir / "id_rsa").read_bytes()
    calls_before = len(requested_clients)
    with pytest.raises(RuntimeError, match="destroy --confirm NAME"):
        cloud.provision(config)
    assert len(requested_clients) == calls_before
    assert lightsail.created == ["key_pair", "instance"]
    assert (config.state_dir / "id_rsa").read_bytes() == key_before
    cloud.destroy(config, config.name)
    assert lightsail.deleted == ["instance", "key_pair"]
    assert cloud.provision(config)["ip"] == "192.0.2.42"
    assert (config.state_dir / "id_rsa").read_bytes() == key_before


def test_existing_unowned_instance_is_not_repurposed(environment):
    config, lightsail, _, _ = environment
    lightsail._create("instance", config.name, tags=[])
    with pytest.raises(RuntimeError, match="unowned"):
        cloud.provision(config)
    assert lightsail.firewall is None
    assert lightsail.deleted == []


def test_destroy_refuses_account_mismatch_and_replaced_resource(environment):
    config, lightsail, _, account = environment
    state = cloud.provision(config)
    account["Account"] = "999999999999"
    with pytest.raises(RuntimeError, match="account"):
        cloud.destroy(config, config.name)
    account["Account"] = "123456789012"
    lightsail.resources["instance"][state["instance_name"]]["arn"] = "arn:replacement"
    with pytest.raises(RuntimeError, match="replaced"):
        cloud.destroy(config, config.name)
    assert lightsail.deleted == []


def test_teardown_ignores_legacy_dns_and_reuses_local_key(environment):
    config, lightsail, requested_clients, _ = environment
    state = cloud.provision(config)
    state["dns"] = {"zone_id": "legacy-zone", "phase": "active", "previous": "legacy-record"}
    cloud._save(config, state)
    assert "dns" not in cloud.status(config)
    key_before = (config.state_dir / "id_rsa").read_bytes()
    cloud.destroy(config, config.name)
    assert lightsail.deleted == ["instance", "static_ip", "key_pair"]
    assert cloud.load_state(config)["destroyed"]
    cloud.provision(config)
    assert (config.state_dir / "id_rsa").read_bytes() == key_before
    assert set(requested_clients) == {"sts", "lightsail"}


def test_moved_static_ip_blocks_teardown_without_deleting_host(environment):
    config, lightsail, _, _ = environment
    state = cloud.provision(config)
    lightsail.resources["static_ip"][state["static_ip_name"]]["attachedTo"] = "another-instance"
    with pytest.raises(RuntimeError, match="Static IP moved"):
        cloud.destroy(config, config.name)
    assert lightsail.deleted == []


def test_key_is_never_overwritten_on_corruption(environment):
    config, _, _, _ = environment
    cloud.ensure_ssh_key(config)
    path = config.state_dir / "id_rsa"
    path.write_bytes(b"do not replace")
    with pytest.raises(RuntimeError, match="not been overwritten"):
        cloud.ensure_ssh_key(config)
    assert path.read_bytes() == b"do not replace"


def test_small_or_unavailable_bundle_fails_before_resource_creation(environment):
    config, lightsail, _, _ = environment
    lightsail.bundle_ram = 1
    with pytest.raises(RuntimeError, match="at least 2 GB"):
        cloud.provision(config)
    assert lightsail.created == []


def test_confirmation_required_before_any_cloud_call(environment, monkeypatch):
    config, _, _, _ = environment
    monkeypatch.setattr(cloud, "_session", lambda config: pytest.fail("must not contact AWS"))
    with pytest.raises(ValueError, match="exact deployment NAME"):
        cloud.destroy(config, "wrong-name")


def test_host_keys_come_from_aws_and_temporary_private_key_is_not_saved(environment):
    config, _, _, _ = environment
    cloud.provision(config)
    path = cloud.trust_host(config)
    assert path.read_text().startswith("192.0.2.42 ssh-rsa ")
    assert "DO-NOT-PERSIST" not in path.read_text()


def test_state_prevents_region_switch(environment):
    config, _, _, _ = environment
    cloud.provision(config)
    with pytest.raises(RuntimeError, match="state does not match"):
        cloud.load_state(replace(config, region="us-east-1"))


@pytest.mark.parametrize("kind", ["key_pair", "instance", "static_ip"])
def test_interrupted_creation_needs_manual_deletion_before_teardown(environment, monkeypatch, kind):
    config, lightsail, _, _ = environment
    original = cloud._wait_resource

    def interrupted(client, resource_kind, name, **kwargs):
        if resource_kind == kind:
            raise RuntimeError("interrupted before ARN captured")
        return original(client, resource_kind, name, **kwargs)

    monkeypatch.setattr(cloud, "_wait_resource", interrupted)
    with pytest.raises(RuntimeError, match="Manually delete resources whose ARN was not saved"):
        cloud.provision(config)
    state = cloud.load_state(config)
    entry = state["resources"][kind]
    assert set(entry) == {"name"}
    created_before = lightsail.created.copy()
    with pytest.raises(RuntimeError, match="Provisioning is incomplete"):
        cloud.provision(config)
    assert lightsail.created == created_before
    with pytest.raises(RuntimeError, match="Cannot prove ownership"):
        cloud.destroy(config, config.name)
    assert lightsail.deleted == []

    # An operator inspects and deletes the ambiguous resource in AWS first.
    lightsail._delete(kind, entry["name"])
    monkeypatch.setattr(cloud, "_wait_resource", original)
    cloud.destroy(config, config.name)
    assert all(not resources for resources in lightsail.resources.values())
    assert cloud.provision(config)["ip"] == "192.0.2.42"


def test_lost_create_response_is_not_retried_or_adopted(environment, monkeypatch):
    config, lightsail, _, _ = environment
    original = lightsail.import_key_pair

    def lost_response(**kwargs):
        original(**kwargs)
        raise TimeoutError("response lost")

    monkeypatch.setattr(lightsail, "import_key_pair", lost_response)
    with pytest.raises(RuntimeError, match="TimeoutError.*Provisioning is incomplete"):
        cloud.provision(config)
    with pytest.raises(RuntimeError, match="Provisioning is incomplete"):
        cloud.provision(config)
    with pytest.raises(RuntimeError, match="Cannot prove ownership"):
        cloud.destroy(config, config.name)
    assert lightsail.created == ["key_pair"]
    assert lightsail.deleted == []


def test_lost_private_key_is_not_replaced_for_an_existing_host(environment):
    config, _, _, _ = environment
    cloud.provision(config)
    path = config.state_dir / "id_rsa"
    path.unlink()
    with pytest.raises(RuntimeError, match="private key is missing"):
        cloud.provision(config)
    assert not path.exists()


def test_host_key_wait_retries_only_transient_failure(environment, monkeypatch):
    config, lightsail, _, _ = environment
    cloud.provision(config)
    original = lightsail.get_instance_access_details
    calls = []

    def temporary(**kwargs):
        calls.append(kwargs)
        if len(calls) == 1:
            raise ClientError({"Error": {"Code": "OperationFailureException"}}, "GetAccessDetails")
        return original(**kwargs)

    monkeypatch.setattr(lightsail, "get_instance_access_details", temporary)
    monkeypatch.setattr(cloud.time, "sleep", lambda seconds: None)
    assert cloud.trust_host(config).exists()
    assert len(calls) == 2


def test_failed_create_does_not_claim_a_concurrent_name_collision(environment, monkeypatch):
    config, lightsail, _, _ = environment

    def collision(**kwargs):
        name = kwargs["instanceNames"][0]
        lightsail._create("instance", name, tags=[])
        return {
            "operations": [
                {"id": "failed-create", "status": "Failed", "errorCode": "AlreadyExists"}
            ]
        }

    monkeypatch.setattr(lightsail, "create_instances", collision)
    with pytest.raises(RuntimeError, match="AlreadyExists"):
        cloud.provision(config)
    assert "arn" not in cloud.load_state(config)["resources"]["instance"]
    with pytest.raises(RuntimeError, match="Cannot prove ownership"):
        cloud.destroy(config, config.name)
    assert lightsail.deleted == []
