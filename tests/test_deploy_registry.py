"""Tests for the deployment registry concept and its implementations.

The registry is where the fleet is written down. `local` is the source of
truth; `notesgraph` publishes into a NotesGraph device inventory. These
tests cover the concept (records, status rollup, ids), the local
implementation against a throwaway config, and the NotesGraph wire mapping
against a fake HTTP layer -- nothing here reaches a network.
"""

import json
import pytest
import yaml

from workflow.deploy.registry import (
    Check,
    Device,
    DeviceStatus,
    MultiRegistry,
    STATE_DEGRADED,
    STATE_OFFLINE,
    STATE_ONLINE,
    STATE_UNKNOWN,
    build_registry,
    folder_id,
    get_registry,
    slugify,
)
from workflow.deploy.registry.local import LocalRegistry


@pytest.fixture
def config_file(tmp_path, monkeypatch):
    """Point load_config/save_config at a throwaway config.yaml.

    Patches the path rather than the functions: several modules bind
    load_config at import time, so replacing it leaks past the revert.
    """
    cfg_path = tmp_path / "config.yaml"
    cfg_path.write_text(yaml.dump({"projects": {}, "task_backend": "markdown"}))
    monkeypatch.setattr("workflow.config.CFG", cfg_path)
    return cfg_path


def machine(**overrides) -> Device:
    base = dict(id="gem5", name="gem5", host="gem5.local", user="pantheon",
                recipe="pantheon", repo="~/code/robots_realtime", agent_target=True)
    base.update(overrides)
    return Device(**base)


# ---------------------------------------------------------------------------
# the concept
# ---------------------------------------------------------------------------

def test_ssh_destination_and_target_ref():
    device = machine(branch="main")
    assert device.ssh_destination == "pantheon@gem5.local"
    assert device.target_ref == "main"
    # a pin is an exact revision and outranks the branch
    device.pin = "v1.2.3"
    assert device.target_ref == "v1.2.3"


def test_device_survives_a_dict_roundtrip():
    device = machine(labels={"site": "hq"})
    device.status = DeviceStatus(state=STATE_ONLINE, version="abc123")
    restored = Device.from_dict(device.to_dict())

    assert restored == device
    assert restored.status.version == "abc123"


def test_from_dict_ignores_unknown_fields():
    """A registry that grew a field must not break an older client."""
    restored = Device.from_dict({"id": "x", "host": "h", "future_field": 1})
    assert restored.id == "x" and restored.host == "h"


def test_status_rolls_checks_up():
    passing = [Check("ssh", True), Check("rig", True)]
    assert DeviceStatus.from_checks(passing).state == STATE_ONLINE

    mixed = [Check("ssh", True), Check("rig", False, "gripper offline")]
    rolled = DeviceStatus.from_checks(mixed)
    assert rolled.state == STATE_DEGRADED
    assert "gripper offline" in rolled.detail


def test_no_checks_means_offline_not_online():
    """A device that could not be reached ran no probes; that is not health."""
    assert DeviceStatus.from_checks([]).state == STATE_OFFLINE


def test_unchecked_device_is_unknown():
    assert Device(id="x").status.state == STATE_UNKNOWN


def test_ids_are_url_safe_and_namespaced():
    assert slugify("GEM5.local") == "gem5-local"
    assert slugify("!!!") == "target"          # never empty
    assert folder_id("gem5", "/home/p/code/robots_realtime") == "gem5:robots_realtime"


# ---------------------------------------------------------------------------
# local registry
# ---------------------------------------------------------------------------

def test_register_then_read_back(config_file):
    registry = LocalRegistry()
    registry.register(machine())

    stored = registry.get("gem5")
    assert stored.host == "gem5.local"
    assert stored.agent_target is True
    assert stored.recipe == "pantheon"


def test_registration_is_persisted_to_config(config_file):
    LocalRegistry().register(machine())
    written = yaml.safe_load(config_file.read_text())
    assert written["deploy"]["targets"]["gem5"]["host"] == "gem5.local"
    # the id is the key, not a duplicated field
    assert "id" not in written["deploy"]["targets"]["gem5"]


def test_re_registering_updates_and_keeps_registration_time(config_file):
    registry = LocalRegistry()
    first = registry.register(machine())
    registry.register(machine(host="gem5.new"))

    stored = registry.get("gem5")
    assert stored.host == "gem5.new"
    assert stored.registered_at == first.registered_at


def test_re_registering_keeps_a_recorded_status(config_file):
    """Editing a target must not silently mark it unknown again."""
    registry = LocalRegistry()
    registry.register(machine())
    registry.record_status("gem5", DeviceStatus.from_checks([Check("ssh", True)]))

    registry.register(machine(host="gem5.new"))
    assert registry.get("gem5").status.state == STATE_ONLINE


def test_listing_filters_by_kind_and_agent_target(config_file):
    registry = LocalRegistry()
    registry.register(machine())
    registry.register(Device(id="build01", host="build01", user="ci", agent_target=False))
    registry.register(Device(id="gem5:rr", kind="folder", parent="gem5",
                             path="/home/p/rr", agent_target=True))

    assert {d.id for d in registry.list()} == {"gem5", "build01", "gem5:rr"}
    assert {d.id for d in registry.list(kind="folder")} == {"gem5:rr"}
    assert {d.id for d in registry.list(agent_target=True)} == {"gem5", "gem5:rr"}


def test_removing_a_machine_removes_its_folders(config_file):
    """A folder target is meaningless without the machine it sits on."""
    registry = LocalRegistry()
    registry.register(machine())
    registry.register(Device(id="gem5:rr", kind="folder", parent="gem5", path="/x"))

    assert registry.deregister("gem5") is True
    assert registry.list() == []


def test_deregistering_something_absent_reports_false(config_file):
    assert LocalRegistry().deregister("ghost") is False


def test_status_for_an_unknown_device_is_ignored(config_file):
    """A health run against a device removed mid-run must not resurrect it."""
    registry = LocalRegistry()
    registry.record_status("ghost", DeviceStatus(state=STATE_ONLINE))
    assert registry.get("ghost") is None


# ---------------------------------------------------------------------------
# factory and fan-out
# ---------------------------------------------------------------------------

def test_factory_rejects_an_unknown_registry():
    with pytest.raises(ValueError, match="Unknown deployment registry"):
        build_registry("redis", {})


def test_notesgraph_cannot_be_the_primary(config_file):
    """It publishes the fleet; it does not decide it."""
    cfg = {"deploy": {"registry": "notesgraph",
                      "notesgraph": {"workspace": "ws-1"}}}
    with pytest.raises(ValueError, match="cannot be the primary"):
        get_registry(cfg)


def test_a_failing_mirror_does_not_lose_the_primary_write(config_file, capsys):
    class Exploding(LocalRegistry):
        name = "exploding"
        def register(self, device):
            raise RuntimeError("inventory unreachable")

    registry = MultiRegistry(LocalRegistry(), [Exploding()])
    registry.register(machine())

    assert registry.get("gem5").host == "gem5.local"
    assert "inventory unreachable" in capsys.readouterr().out


def test_reads_come_from_the_primary_only(config_file):
    class Lying(LocalRegistry):
        name = "lying"
        def list(self, kind=None, agent_target=None):
            raise AssertionError("mirrors must never be read")

    registry = MultiRegistry(LocalRegistry(), [Lying()])
    registry.register(machine())
    assert [d.id for d in registry.list()] == ["gem5"]


# ---------------------------------------------------------------------------
# notesgraph wire mapping
# ---------------------------------------------------------------------------

def test_device_maps_onto_the_inventory_payload():
    from workflow.deploy.registry.notesgraph import device_to_payload

    payload = device_to_payload(machine(labels={"site": "hq"}))
    assert payload["key"] == "gem5"              # wf id is the upsert key
    assert payload["agentTarget"] is True        # camelCase on the wire
    assert payload["parentKey"] is None
    assert payload["labels"] == {"site": "hq"}


def test_inventory_payload_maps_back_onto_a_device():
    from workflow.deploy.registry.notesgraph import payload_to_device

    device = payload_to_device({
        "id": "uuid-9", "key": "gem5", "name": "gem5", "kind": "machine",
        "host": "gem5.local", "user": "pantheon", "port": 22,
        "agentTarget": True, "state": "degraded", "statusDetail": "rig: down",
        "checkedAt": 1700000000, "version": "abc123", "checks": [],
    })

    assert device.id == "gem5"
    # the inventory's own uuid is kept so a later update addresses the same row
    assert device.external_id == "uuid-9"
    assert device.agent_target is True
    assert device.status.state == STATE_DEGRADED
    assert device.status.version == "abc123"


def test_notesgraph_registry_requires_a_workspace():
    from workflow.deploy.registry.notesgraph import NotesGraphRegistry
    with pytest.raises(ValueError, match="workspace"):
        NotesGraphRegistry(workspace="")


def test_notesgraph_register_posts_and_keeps_the_assigned_id(monkeypatch):
    from workflow.deploy.registry import notesgraph as ng

    calls = []

    def fake_request(self, method, path, payload=None):
        calls.append((method, path, payload))
        return {"device": {"id": "uuid-9", "key": payload["key"]}}

    monkeypatch.setattr(ng.NotesGraphClient, "_request", fake_request)
    registry = ng.NotesGraphRegistry(workspace="ws-1", token="pat-x")
    device = registry.register(machine())

    method, path, payload = calls[0]
    assert method == "POST"
    assert path == "/api/inventory/workspaces/ws-1/devices"
    assert payload["key"] == "gem5"
    assert device.external_id == "uuid-9"


def test_notesgraph_status_posts_to_the_status_endpoint(monkeypatch):
    from workflow.deploy.registry import notesgraph as ng

    calls = []
    monkeypatch.setattr(
        ng.NotesGraphClient, "_request",
        lambda self, method, path, payload=None: calls.append((method, path, payload)) or {"device": {}},
    )
    registry = ng.NotesGraphRegistry(workspace="ws-1", token="pat-x")
    registry.record_status("gem5", DeviceStatus.from_checks([Check("ssh", True)], version="abc"))

    method, path, payload = calls[0]
    assert method == "POST"
    assert path == "/api/inventory/workspaces/ws-1/devices/gem5/status"
    assert payload["state"] == STATE_ONLINE
    assert payload["version"] == "abc"


def test_a_missing_token_is_a_clear_error(monkeypatch):
    from workflow.deploy.registry import notesgraph as ng

    monkeypatch.setattr(ng, "load_token", lambda: None)
    client = ng.NotesGraphClient(url="https://app.notesgraph.com", token=None)
    with pytest.raises(ng.NotesGraphError, match="notes-login"):
        client.session()
