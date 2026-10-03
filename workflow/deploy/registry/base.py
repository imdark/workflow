"""The deployment registry concept.

A registry is wherever the fleet is written down: which machines exist, how
to reach them, what is deployed on them, whether an agent may execute
there. `LocalRegistry` keeps that in ~/.wf/config.yaml and is always
present; other implementations publish the same records somewhere a team
can see them -- `NotesGraphRegistry` into a NotesGraph inventory, and
whatever comes next behind the same interface.

Two record types, because a deployment target is not always a whole box:

  machine   a host reachable over SSH
  folder    a checkout on a machine, registered in its own right so an
            agent can be pointed at it without owning the host

A folder's `parent` is the machine it lives on, so a registry that can only
model devices can still flatten folders into labelled children.
"""

import re
import time
from dataclasses import asdict, dataclass, field
from typing import Optional

KIND_MACHINE = "machine"
KIND_FOLDER = "folder"
KINDS = (KIND_MACHINE, KIND_FOLDER)

# Health states a probe may report. `unknown` is the honest default: a
# device that has never been checked is not the same as one that is down.
STATE_ONLINE = "online"
STATE_DEGRADED = "degraded"
STATE_OFFLINE = "offline"
STATE_UNKNOWN = "unknown"
STATES = (STATE_ONLINE, STATE_DEGRADED, STATE_OFFLINE, STATE_UNKNOWN)


@dataclass
class Check:
    """One health probe's result."""

    name: str
    ok: bool
    detail: str = ""
    duration_ms: int = 0

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class DeviceStatus:
    """The outcome of the last health run against a device."""

    state: str = STATE_UNKNOWN
    checked_at: float = 0.0
    version: Optional[str] = None      # deployed ref, as the device reports it
    detail: str = ""
    checks: list = field(default_factory=list)

    @classmethod
    def from_checks(cls, checks: list, version: Optional[str] = None) -> "DeviceStatus":
        """Roll per-probe results into one state.

        Any failure among probes that ran is `degraded` rather than
        `offline`: the device answered, something on it is unhappy. Only an
        unreachable device is `offline`, which the caller signals by passing
        no checks at all.
        """
        if not checks:
            return cls(state=STATE_OFFLINE, checked_at=time.time(),
                       detail="unreachable", checks=[])
        failed = [c for c in checks if not c.ok]
        state = STATE_DEGRADED if failed else STATE_ONLINE
        detail = "; ".join(f"{c.name}: {c.detail or 'failed'}" for c in failed)
        return cls(state=state, checked_at=time.time(), version=version,
                   detail=detail, checks=[c.to_dict() for c in checks])

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Optional[dict]) -> "DeviceStatus":
        if not data:
            return cls()
        known = {f for f in cls.__dataclass_fields__}
        return cls(**{k: v for k, v in data.items() if k in known})


@dataclass
class Device:
    """A registered deployment target."""

    id: str
    name: str = ""
    kind: str = KIND_MACHINE
    host: str = ""
    user: str = ""
    port: int = 22

    # Folder targets only: the machine they live on, and the path there.
    parent: Optional[str] = None
    path: Optional[str] = None

    # What `wf deploy` runs here, and which revision it should be on.
    recipe: str = "generic"
    repo: Optional[str] = None
    branch: str = "main"
    channel: str = "stable"
    pin: Optional[str] = None          # exact ref; overrides branch when set

    # Whether agents may execute against this target.
    agent_target: bool = False

    labels: dict = field(default_factory=dict)
    status: DeviceStatus = field(default_factory=DeviceStatus)
    registered_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)

    # Set by a remote registry that assigned its own identifier, so a later
    # update addresses the same record instead of creating a second one.
    external_id: Optional[str] = None

    @property
    def ssh_destination(self) -> str:
        return f"{self.user}@{self.host}" if self.user else self.host

    @property
    def target_ref(self) -> str:
        """The revision this device should be running."""
        return self.pin or self.branch

    def to_dict(self) -> dict:
        data = asdict(self)
        data["status"] = self.status.to_dict()
        return data

    @classmethod
    def from_dict(cls, data: dict) -> "Device":
        data = dict(data)
        status = DeviceStatus.from_dict(data.pop("status", None))
        known = {f for f in cls.__dataclass_fields__}
        device = cls(**{k: v for k, v in data.items() if k in known})
        device.status = status
        return device


def slugify(text: str) -> str:
    """Stable id from a hostname or path. Never empty.

    Underscores survive, so a checkout named `robots_realtime` keeps its
    real spelling; everything else -- dots in a hostname especially --
    collapses to a dash, turning `gem5.local` into the `gem5-local` you
    would actually want to type. The result stays inside the alphabet the
    inventory API accepts for a device key.
    """
    slug = re.sub(r"[^a-zA-Z0-9_]+", "-", str(text).strip().lower()).strip("-_")
    return slug[:64] or "target"


def folder_id(machine_id: str, path: str) -> str:
    """Folder ids are namespaced under their machine so they never collide."""
    return f"{machine_id}:{slugify(path.rstrip('/').split('/')[-1] or path)}"


class DeploymentRegistry:
    """Where the fleet is written down.

    Implementations must tolerate being called for a device they have not
    seen, and must never raise on a status update -- a health run that
    cannot publish its result is still a health run worth keeping locally.
    """

    name = "base"
    # False when the registry cannot be the source of truth (write-only or
    # remote), so the factory knows not to read the fleet from it.
    authoritative = False

    def register(self, device: Device) -> Device:
        """Create or update a device. Returns the stored record."""
        raise NotImplementedError

    def deregister(self, device_id: str) -> bool:
        """Remove a device. Returns whether anything was removed."""
        raise NotImplementedError

    def get(self, device_id: str) -> Optional[Device]:
        raise NotImplementedError

    def list(self, kind: Optional[str] = None,
             agent_target: Optional[bool] = None) -> list:
        raise NotImplementedError

    def record_status(self, device_id: str, status: DeviceStatus) -> None:
        """Attach the result of a health run to a device."""
        raise NotImplementedError

    def close(self) -> None:
        return None
