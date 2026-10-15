import json
import traceback
from copy import deepcopy
from dataclasses import replace
from types import SimpleNamespace

import pytest
from botocore.exceptions import ClientError

import cloud
from config import Config


class FakeLightsail:
    """Only the explicit runbook API; discovery or reconciliation calls fail tests."""

    def __init__(self):
        self.resources = {"key_pair": {}, "instance": {}, "static_ip": {}}
        self.mutations = []
        self.public_key = None
        self.ports = []

    def _get(self, kind, name, result):
        if name not in self.resources[kind]:
            raise ClientError({"Error": {"Code": "NotFoundException", "Message": "missing"}}, "Get")
        return {result: deepcopy(self.resources[kind][name])}

    def get_instance(self, instanceName):
        return self._get("instance", instanceName, "instance")

    def get_key_pair(self, keyPairName):
        return self._get("key_pair", keyPairName, "keyPair")

    def get_static_ip(self, staticIpName):
        return self._get("static_ip", staticIpName, "staticIp")

    def _create(self, kind, name, **extra):
        assert name not in self.resources[kind]
        self.resources[kind][name] = {"name": name, "arn": f"arn:fake:{kind}:{name}", **extra}
        return {"operations": [{"id": f"create-{kind}", "status": "Succeeded"}]}

    def import_key_pair(self, **kwargs):
        self.mutations.append(("import_key_pair", kwargs))
        self.public_key = kwargs["publicKeyBase64"]
        assert self.public_key.startswith("ssh-rsa ")
        return self._create("key_pair", kwargs["keyPairName"])

    def create_instances(self, **kwargs):
        self.mutations.append(("create_instances", kwargs))
        return self._create("instance", kwargs["instanceNames"][0],
                            sshKeyName=kwargs["keyPairName"], state={"name": "running"})

    def put_instance_public_ports(self, **kwargs):
        self.mutations.append(("put_instance_public_ports", kwargs))
        self.ports = deepcopy(kwargs["portInfos"])
        return {}

    def open_instance_public_ports(self, **kwargs):
        self.mutations.append(("open_instance_public_ports", kwargs))
        self.ports.append(deepcopy(kwargs["portInfo"]))
        return {}

    def get_instance_port_states(self, instanceName):
        assert instanceName in self.resources["instance"]
        return {"portStates": [{"state": "open", **deepcopy(rule)} for rule in self.ports]}

    def allocate_static_ip(self, **kwargs):
        self.mutations.append(("allocate_static_ip", kwargs))
        return self._create("static_ip", kwargs["staticIpName"],
                            ipAddress="192.0.2.42", isAttached=False)

    def attach_static_ip(self, **kwargs):
        self.mutations.append(("attach_static_ip", kwargs))
        self.resources["static_ip"][kwargs["staticIpName"]].update(
            isAttached=True, attachedTo=kwargs["instanceName"]
        )
        return {}

    def delete_instance(self, **kwargs):
        self.mutations.append(("delete_instance", kwargs))
        del self.resources["instance"][kwargs["instanceName"]]
        for address in self.resources["static_ip"].values():
            if address.get("attachedTo") == kwargs["instanceName"]:
                address.update(isAttached=False)
                address.pop("attachedTo", None)
        return {}

    def release_static_ip(self, **kwargs):
        self.mutations.append(("release_static_ip", kwargs))
        del self.resources["static_ip"][kwargs["staticIpName"]]
        return {}

    def delete_key_pair(self, **kwargs):
        self.mutations.append(("delete_key_pair", kwargs))
        del self.resources["key_pair"][kwargs["keyPairName"]]
        return {}

    def get_instance_access_details(self, **kwargs):
        return {"accessDetails": {
            "ipAddress": "192.0.2.42", "privateKey": "DO-NOT-PERSIST",
            "hostKeys": [{"algorithm": "ssh-rsa", "publicKey": self.public_key}],
        }}


@pytest.fixture
def environment(tmp_path, monkeypatch):
    config = Config(
        name="example", region="eu-west-1", hostname="draft.example.com",
        ssh_cidrs=("192.0.2.1/32",), catalog_csv=tmp_path / "catalog.csv",
        image_root=tmp_path / "images", state_dir=tmp_path / "private",
        oidc_client_id="fake-client", oidc_client_secret="fake-secret", callback_path="/callback",
    )
    lightsail = FakeLightsail()
    requested_clients = []
    account = {"Account": "123456789012"}
    clients = {"lightsail": lightsail, "sts": SimpleNamespace(get_caller_identity=lambda: account)}

    def client(name, **kwargs):
        requested_clients.append((name, kwargs["config"]))
        return clients[name]

    monkeypatch.setattr(cloud, "_session", lambda config: SimpleNamespace(client=client))
    # Restrictive real Windows ACLs would prevent pytest from cleaning its temporary keys.
    monkeypatch.setattr(cloud, "protect_ssh_key", lambda path: None)
    return config, lightsail, requested_clients, account


def complete_steps(config):
    cloud.create_key(config)
    cloud.create_instance(config)
    cloud.open_ports(config)
    cloud.allocate_ip(config)
    return cloud.attach_ip(config)


def test_each_step_performs_only_its_named_mutation(environment, capsys):
    config, lightsail, clients, _ = environment
    expected = ["import_key_pair", "create_instances", "put_instance_public_ports",
                "allocate_static_ip", "attach_static_ip"]
    actions = [cloud.create_key, cloud.create_instance, cloud.open_ports,
               cloud.allocate_ip, cloud.attach_ip]
    for index, action in enumerate(actions):
        state = action(config)
        assert [method for method, _ in lightsail.mutations] == expected[:index + 1]
        assert ("ip" in state) is (action is cloud.attach_ip)
    assert state["ip"] == "192.0.2.42"
    assert all(set(entry) == {"name", "arn"} for entry in state["resources"].values())
    instance = lightsail.mutations[1][1]
    assert instance == {
        "instanceNames": ["example"], "availabilityZone": "eu-west-1a",
        "blueprintId": "ubuntu_24_04", "bundleId": "small_3_0",
        "keyPairName": "example-key", "ipAddressType": "ipv4",
    }
    assert lightsail.mutations[2][1]["portInfos"][0]["cidrs"] == ["192.0.2.1/32"]
    output = capsys.readouterr().out
    for method in expected:
        assert method.replace("_", "-") in output
    assert "Completed AWS" in output
    assert "fake-secret" not in output + (config.state_dir / "state.json").read_text()
    assert {name for name, _ in clients} == {"sts", "lightsail"}
    assert all(options.retries == {"total_max_attempts": 1} for _, options in clients)


@pytest.mark.parametrize("action", [cloud.create_key, cloud.create_instance, cloud.allocate_ip])
def test_create_refuses_existing_resource_without_replaying_a_step(environment, action):
    config, lightsail, _, _ = environment
    complete_steps(config)
    previous = list(lightsail.mutations)
    key = (config.state_dir / "id_rsa").read_bytes()
    with pytest.raises(RuntimeError, match="already exists"):
        action(config)
    assert lightsail.mutations == previous
    assert (config.state_dir / "id_rsa").read_bytes() == key


def test_unrecorded_instance_conflict_is_not_adopted_or_modified(environment):
    config, lightsail, _, _ = environment
    cloud.create_key(config)
    lightsail._create("instance", config.name)
    with pytest.raises(RuntimeError, match="already exists"):
        cloud.create_instance(config)
    assert "instance" not in cloud.load_state(config)["resources"]
    assert [method for method, _ in lightsail.mutations] == ["import_key_pair"]


def test_legacy_receipt_load_is_local_and_does_not_rewrite_it(environment, monkeypatch):
    config, lightsail, _, _ = environment
    state = complete_steps(config)
    state.update(deployment_id="deadbeef1234", spec={"bundle": "old-bundle"},
                 dns={"phase": "legacy", "zone_id": "unused"})
    for kind, field in (("key_pair", "key_pair_name"), ("static_ip", "static_ip_name")):
        old_name = state[field]
        name = old_name + "-deadbeef"
        state[field] = name
        state["resources"][kind]["name"] = name
        resource = lightsail.resources[kind].pop(old_name)
        resource["name"] = name
        lightsail.resources[kind][name] = resource
    cloud._save(config, state)
    previous = (config.state_dir / "state.json").read_bytes()
    with monkeypatch.context() as patch:
        patch.setattr(cloud, "_session", lambda config: pytest.fail("load must stay local"))
        assert cloud.load_state(config) == state
    assert (config.state_dir / "state.json").read_bytes() == previous
    cloud.delete_key(config, config.name)
    assert lightsail.mutations[-1] == ("delete_key_pair", {"keyPairName": "example-key-deadbeef"})
    cloud.delete_instance(config, config.name)
    cloud.release_ip(config, config.name)
    assert lightsail.mutations[-1] == ("release_static_ip", {"staticIpName": "example-ip-deadbeef"})


def test_open_ports_only_updates_firewall_and_preserves_other_receipt_fields(environment):
    config, lightsail, _, _ = environment
    state = complete_steps(config)
    changed = replace(config, ssh_cidrs=("192.0.2.2/32",), bundle_id="different_bundle")
    previous = len(lightsail.mutations)
    assert cloud.open_ports(changed) == state
    assert len(lightsail.mutations) == previous + 1
    assert lightsail.mutations[-1][0] == "put_instance_public_ports"
    assert lightsail.mutations[-1][1]["portInfos"][0]["cidrs"] == ["192.0.2.2/32"]


def test_database_port_opens_only_after_verified_host_policy(environment, monkeypatch):
    import remote

    config, lightsail, _, _ = environment
    complete_steps(config)
    checks = []
    monkeypatch.setattr(
        remote, "Remote",
        lambda *a: SimpleNamespace(check_database_tls=lambda: checks.append(True)),
    )
    cloud.open_database_port(config)
    assert checks == [True]
    assert lightsail.mutations[-1] == ("open_instance_public_ports", {
        "instanceName": "example", "portInfo": {
            "fromPort": 5432, "toPort": 5432, "protocol": "tcp", "cidrs": ["0.0.0.0/0"],
            "ipv6Cidrs": [], "cidrListAliases": [],
        },
    })
    assert "database_port_open" not in cloud.load_state(config)
    cloud.open_ports(config)
    assert any(p["fromPort"] == 5432 for p in lightsail.mutations[-1][1]["portInfos"])


def test_changed_ssh_cidrs_need_no_ssh_and_preserve_unrelated_live_rules(environment, monkeypatch):
    import remote

    config, lightsail, _, _ = environment
    state = complete_steps(config)
    unrelated = [
        {"fromPort": 5432, "toPort": 5432, "protocol": "tcp", "cidrs": ["0.0.0.0/0"],
         "ipv6Cidrs": [], "cidrListAliases": []},
        {"fromPort": 9000, "toPort": 9010, "protocol": "tcp", "cidrs": [],
         "ipv6Cidrs": ["2001:db8::/64"], "cidrListAliases": []},
        {"fromPort": 3389, "toPort": 3389, "protocol": "tcp", "cidrs": [],
         "ipv6Cidrs": [], "cidrListAliases": ["lightsail-connect"]},
        {"fromPort": 22, "toPort": 443, "protocol": "udp", "cidrs": ["192.0.2.9/32"],
         "ipv6Cidrs": [], "cidrListAliases": []},
        {"fromPort": 8, "toPort": -1, "protocol": "icmp", "cidrs": ["0.0.0.0/0"],
         "ipv6Cidrs": [], "cidrListAliases": []},
    ]
    lightsail.ports.extend(deepcopy(unrelated))
    # Default/legacy SSH can also have IPv6 and browser-console access. Both
    # must be removed rather than leaving an alternative path past SSH_CIDRS.
    lightsail.ports[0].update(ipv6Cidrs=["::/0"], cidrListAliases=["lightsail-connect"])
    state["database_port_open"] = True  # Old receipts must not trigger an SSH check.
    cloud._save(config, state)
    monkeypatch.setattr(remote, "Remote", lambda *a: pytest.fail("must not use SSH"))
    previous = len(lightsail.mutations)
    cloud.open_ports(replace(config, ssh_cidrs=("192.0.2.2/32",)))
    assert len(lightsail.mutations) == previous + 1
    assert lightsail.ports[0] == {
        "fromPort": 22, "toPort": 22, "protocol": "tcp", "cidrs": ["192.0.2.2/32"],
        "ipv6Cidrs": [], "cidrListAliases": [],
    }
    assert lightsail.ports[3:] == unrelated
    assert cloud.load_state(config) == state


def test_stale_receipt_flag_does_not_reopen_database_port(environment, monkeypatch):
    import remote

    config, lightsail, _, _ = environment
    state = complete_steps(config)
    state["database_port_open"] = True
    cloud._save(config, state)
    monkeypatch.setattr(remote, "Remote", lambda *a: pytest.fail("must not use SSH"))
    cloud.open_ports(config)
    assert [rule["fromPort"] for rule in lightsail.ports] == [22, 80, 443]


@pytest.mark.parametrize("protocol,start,end", [
    ("tcp", 5432, 5432), ("tcp", 20, 23), ("all", -1, -1),
])
def test_closed_firewall_rules_are_not_reopened_or_treated_as_live_overlaps(
    environment, protocol, start, end,
):
    config, lightsail, _, _ = environment
    complete_steps(config)
    lightsail.ports.append({
        "fromPort": start, "toPort": end, "protocol": protocol,
        "cidrs": ["0.0.0.0/0"], "state": "closed",
    })
    cloud.open_ports(config)
    assert [rule["fromPort"] for rule in lightsail.ports] == [22, 80, 443]


@pytest.mark.parametrize("protocol,start,end", [
    ("tcp", 20, 23), ("tcp", 79, 81), ("tcp", 443, 444), ("all", -1, -1),
])
def test_overlapping_firewall_ranges_require_manual_inspection(
    environment, protocol, start, end,
):
    config, lightsail, _, _ = environment
    complete_steps(config)
    lightsail.ports.append({"fromPort": start, "toPort": end, "protocol": protocol,
                           "cidrs": ["0.0.0.0/0"]})
    previous = list(lightsail.mutations)
    with pytest.raises(RuntimeError, match="split that rule.*Nothing changed"):
        cloud.open_ports(config)
    assert lightsail.mutations == previous


def test_database_port_stays_closed_when_tls_check_fails(environment, monkeypatch):
    import remote

    config, lightsail, _, _ = environment
    complete_steps(config)
    previous = list(lightsail.mutations)

    def fail():
        raise RuntimeError("Unsafe authentication rules")

    monkeypatch.setattr(remote, "Remote", lambda *a: SimpleNamespace(check_database_tls=fail))
    with pytest.raises(RuntimeError, match="Unsafe"):
        cloud.open_database_port(config)
    assert lightsail.mutations == previous
    assert "database_port_open" not in cloud.load_state(config)


def test_mutation_refuses_changed_account_and_replaced_instance(environment):
    config, lightsail, _, account = environment
    state = complete_steps(config)
    previous = list(lightsail.mutations)
    account["Account"] = "999999999999"
    with pytest.raises(RuntimeError, match="account"):
        cloud.open_ports(config)
    account["Account"] = "123456789012"
    lightsail.resources["instance"][state["instance_name"]]["arn"] = "arn:replacement"
    with pytest.raises(RuntimeError, match="replaced"):
        cloud.delete_instance(config, config.name)
    assert lightsail.mutations == previous


def test_rejected_aws_call_keeps_original_exception_and_traceback(environment, monkeypatch, capsys):
    config, lightsail, _, _ = environment
    cloud.create_key(config)
    error = ClientError(
        {"Error": {"Code": "InvalidInputException", "Message": "Invalid bundle small_3_0"}},
        "CreateInstances",
    )

    def rejected_create(**kwargs):
        raise error

    monkeypatch.setattr(lightsail, "create_instances", rejected_create)
    with pytest.raises(ClientError) as caught:
        cloud.create_instance(config)
    assert caught.value is error
    rendered = "".join(traceback.format_exception(error))
    assert "rejected_create" in rendered
    assert "InvalidInputException" in rendered
    assert "Invalid bundle small_3_0" in rendered
    assert not getattr(error, "__notes__", [])
    assert cloud.load_state(config)["resources"]["instance"] == {"name": "example"}
    assert "Run cloud-status" in capsys.readouterr().err


def test_lost_creation_response_is_never_retried_adopted_or_deleted(environment, monkeypatch):
    config, lightsail, _, _ = environment
    original = lightsail.import_key_pair

    def lost_response(**kwargs):
        original(**kwargs)
        raise TimeoutError("response lost")

    monkeypatch.setattr(lightsail, "import_key_pair", lost_response)
    with pytest.raises(TimeoutError, match="response lost"):
        cloud.create_key(config)
    key = (config.state_dir / "id_rsa").read_bytes()
    with pytest.raises(RuntimeError, match="already exists"):
        cloud.create_key(config)
    with pytest.raises(RuntimeError, match="No recorded identity"):
        cloud.delete_key(config, config.name)
    assert len(lightsail.mutations) == 1
    assert cloud.load_state(config)["resources"]["key_pair"] == {"name": "example-key"}
    assert (config.state_dir / "id_rsa").read_bytes() == key


def test_explicit_retry_after_rejected_key_import_keeps_local_key(environment, monkeypatch):
    config, lightsail, _, _ = environment
    original = lightsail.import_key_pair

    def rejected_import(**kwargs):
        raise ClientError({"Error": {"Code": "InvalidInputException"}}, "ImportKeyPair")

    monkeypatch.setattr(lightsail, "import_key_pair", rejected_import)
    with pytest.raises(ClientError):
        cloud.create_key(config)
    previous = (config.state_dir / "id_rsa").read_bytes()
    monkeypatch.setattr(lightsail, "import_key_pair", original)
    cloud.create_key(config)
    assert (config.state_dir / "id_rsa").read_bytes() == previous
    assert len(lightsail.mutations) == 1


def test_failed_create_cannot_capture_concurrent_resource_identity(environment, monkeypatch):
    config, lightsail, _, _ = environment
    cloud.create_key(config)

    def concurrent_collision(**kwargs):
        lightsail._create("instance", kwargs["instanceNames"][0])
        return {"operations": [{"id": "failed", "status": "Failed", "errorCode": "AlreadyExists",
                                "errorDetails": "Instance name already in use"}]}

    monkeypatch.setattr(lightsail, "create_instances", concurrent_collision)
    with pytest.raises(RuntimeError, match="AlreadyExists.*Instance name already in use"):
        cloud.create_instance(config)
    assert "arn" not in cloud.load_state(config)["resources"]["instance"]
    with pytest.raises(RuntimeError, match="No recorded identity"):
        cloud.delete_instance(config, config.name)


def test_pending_operation_has_bounded_visible_wait(environment, monkeypatch, capsys):
    config, lightsail, _, _ = environment
    cloud.create_key(config)
    operation = {"id": "create-pending", "status": "Started"}
    clock = [0.0]
    polls = []
    monkeypatch.setattr(cloud, "WAIT_SECONDS", 7)
    monkeypatch.setattr(cloud.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(
        cloud.time, "sleep", lambda seconds: clock.__setitem__(0, clock[0] + seconds)
    )
    monkeypatch.setattr(lightsail, "create_instances", lambda **kwargs: {"operations": [operation]})

    def pending(operationId):
        polls.append(operationId)
        return {"operation": operation}

    monkeypatch.setattr(lightsail, "get_operation", pending, raising=False)
    with pytest.raises(TimeoutError, match="still pending"):
        cloud.create_instance(config)
    assert clock[0] == 7
    assert polls == ["create-pending", "create-pending"]
    assert "AWS operation create-pending: Started" in capsys.readouterr().out
    assert "arn" not in cloud.load_state(config)["resources"]["instance"]


@pytest.mark.parametrize("action", [cloud.delete_instance, cloud.release_ip, cloud.delete_key])
def test_each_deletion_requires_exact_name_before_cloud_access(environment, monkeypatch, action):
    config, _, _, _ = environment
    monkeypatch.setattr(cloud, "_session", lambda config: pytest.fail("must not contact AWS"))
    with pytest.raises(ValueError, match="exact deployment NAME"):
        action(config, "wrong-name")


def test_deletion_is_one_resource_at_a_time_and_preserves_local_secrets(environment):
    config, lightsail, _, _ = environment
    complete_steps(config)
    secret = config.state_dir / "secrets.json"
    secret.write_text('{"session_secret": "keep this"}')
    private_before = (config.state_dir / "id_rsa").read_bytes()
    previous = len(lightsail.mutations)
    with pytest.raises(RuntimeError, match="still attached"):
        cloud.release_ip(config, config.name)
    assert len(lightsail.mutations) == previous
    cloud.delete_instance(config, config.name)
    assert len(lightsail.mutations) == previous + 1
    assert lightsail.mutations[-1][0] == "delete_instance"
    assert set(cloud.load_state(config)["resources"]) == {"key_pair", "static_ip"}
    assert "ip" not in cloud.load_state(config)
    cloud.release_ip(config, config.name)
    cloud.delete_key(config, config.name)
    assert [method for method, _ in lightsail.mutations[previous:]] == [
        "delete_instance", "release_static_ip", "delete_key_pair",
    ]
    assert cloud.load_state(config)["resources"] == {}
    assert (config.state_dir / "id_rsa").read_bytes() == private_before
    assert json.loads(secret.read_text())["session_secret"] == "keep this"


def test_already_attached_ip_is_not_moved_or_silently_accepted(environment):
    config, lightsail, _, _ = environment
    state = complete_steps(config)
    previous = len(lightsail.mutations)
    for attached_to in (state["instance_name"], "another-instance"):
        lightsail.resources["static_ip"][state["static_ip_name"]]["attachedTo"] = attached_to
        with pytest.raises(RuntimeError, match="already attached"):
            cloud.attach_ip(config)
    assert len(lightsail.mutations) == previous


def test_status_reports_pending_identity_without_adopting_it(environment):
    config, lightsail, _, _ = environment
    state = complete_steps(config)
    state["resources"]["instance"].pop("arn")
    cloud._save(config, state)
    previous = (config.state_dir / "state.json").read_bytes()
    mutation_count = len(lightsail.mutations)
    report = cloud.status(config)
    assert report["resources"]["instance"]["exists"]
    assert not report["resources"]["instance"]["matches_receipt"]
    assert (config.state_dir / "state.json").read_bytes() == previous
    assert len(lightsail.mutations) == mutation_count


def test_host_keys_are_authenticated_without_logging_temporary_private_key(environment, capsys):
    config, _, _, _ = environment
    complete_steps(config)
    path = cloud.trust_host(config)
    assert path.read_text().startswith("192.0.2.42 ssh-rsa ")
    captured = capsys.readouterr()
    assert "DO-NOT-PERSIST" not in captured.out + captured.err + path.read_text()
    assert "--query" in captured.err


def test_host_key_failure_preserves_keys_and_requires_explicit_retry(environment, monkeypatch):
    config, lightsail, _, _ = environment
    complete_steps(config)
    path = cloud.trust_host(config)
    previous = path.read_bytes()
    calls = []

    def not_ready(**kwargs):
        calls.append(kwargs)
        return {"accessDetails": {"ipAddress": "192.0.2.42", "hostKeys": []}}

    monkeypatch.setattr(lightsail, "get_instance_access_details", not_ready)
    with pytest.raises(RuntimeError, match="retry trust-host"):
        cloud.trust_host(config)
    assert len(calls) == 1
    assert path.read_bytes() == previous


def test_corrupt_private_key_is_never_overwritten(environment):
    config, _, _, _ = environment
    path = cloud.ensure_ssh_key(config)
    path.write_bytes(b"do not replace")
    with pytest.raises(RuntimeError, match="not been overwritten"):
        cloud.ensure_ssh_key(config)
    assert path.read_bytes() == b"do not replace"


def test_missing_private_key_stops_creation_without_replacement(environment, monkeypatch):
    config, lightsail, _, _ = environment
    cloud.create_key(config)
    path = config.state_dir / "id_rsa"
    path.unlink()
    monkeypatch.setattr(cloud, "_session", lambda config: pytest.fail("must not contact AWS"))
    with pytest.raises(RuntimeError, match="private key is missing"):
        cloud.create_instance(config)
    assert not path.exists()
    assert [method for method, _ in lightsail.mutations] == ["import_key_pair"]


def test_receipt_prevents_region_switch(environment):
    config, _, _, _ = environment
    cloud.create_key(config)
    with pytest.raises(RuntimeError, match="state does not match"):
        cloud.load_state(replace(config, region="us-east-1"))
