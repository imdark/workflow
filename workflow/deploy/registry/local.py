"""The local registry: the fleet as recorded in ~/.wf/config.yaml.

Always present and always authoritative. Remote registries publish what
this one holds; if they are unreachable, deploys still work.

    deploy:
      targets:
        gem5:
          kind: machine
          host: gem5.local
          user: pantheon
          recipe: pantheon
          repo: ~/code/robots_realtime
          agent_target: true
"""

from typing import Optional

from workflow.deploy.registry.base import (
    Device,
    DeviceStatus,
    DeploymentRegistry,
)


class LocalRegistry(DeploymentRegistry):
    name = "local"
    authoritative = True

    def __init__(self, config_key: str = "deploy"):
        self.config_key = config_key

    # ── config plumbing ───────────────────────────────────────────────────

    def _load(self) -> dict:
        from workflow.config import load_config
        return load_config()

    def _targets(self, cfg: dict) -> dict:
        return (cfg.get(self.config_key) or {}).get("targets") or {}

    def _save_targets(self, cfg: dict, targets: dict) -> None:
        from workflow.config import save_config
        section = cfg.setdefault(self.config_key, {})
        section["targets"] = targets
        save_config(cfg)

    # ── interface ─────────────────────────────────────────────────────────

    def register(self, device: Device) -> Device:
        cfg = self._load()
        targets = self._targets(cfg)

        existing = targets.get(device.id)
        if existing:
            # Preserve the original registration time and any status already
            # recorded; re-registering a device is an update, not a reset.
            device.registered_at = existing.get("registered_at", device.registered_at)
            if not device.status.checked_at:
                device.status = DeviceStatus.from_dict(existing.get("status"))

        import time
        device.updated_at = time.time()
        record = device.to_dict()
        record.pop("id", None)  # the key is the id
        targets[device.id] = record
        self._save_targets(cfg, targets)
        return device

    def deregister(self, device_id: str) -> bool:
        cfg = self._load()
        targets = self._targets(cfg)
        removed = targets.pop(device_id, None) is not None

        # A machine's folder targets have no meaning without it.
        for child in [k for k, v in targets.items() if v.get("parent") == device_id]:
            targets.pop(child, None)
            removed = True

        if removed:
            self._save_targets(cfg, targets)
        return removed

    def get(self, device_id: str) -> Optional[Device]:
        record = self._targets(self._load()).get(device_id)
        return Device.from_dict({**record, "id": device_id}) if record else None

    def list(self, kind: Optional[str] = None,
             agent_target: Optional[bool] = None) -> list:
        devices = [
            Device.from_dict({**record, "id": device_id})
            for device_id, record in self._targets(self._load()).items()
        ]
        if kind:
            devices = [d for d in devices if d.kind == kind]
        if agent_target is not None:
            devices = [d for d in devices if d.agent_target == agent_target]
        return sorted(devices, key=lambda d: d.id)

    def record_status(self, device_id: str, status: DeviceStatus) -> None:
        cfg = self._load()
        targets = self._targets(cfg)
        if device_id not in targets:
            return
        targets[device_id]["status"] = status.to_dict()
        self._save_targets(cfg, targets)
