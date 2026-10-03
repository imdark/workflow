"""Config-driven factory for deployment registries.

    deploy:
      registry: local          # authoritative -- reads come from here
      mirrors: [notesgraph]    # published to as well
      notesgraph:
        url: https://app.notesgraph.com
        workspace: <workspace id>

The primary must be authoritative (today: `local`). Mirrors receive every
write and are allowed to fail: publishing the fleet somewhere shared is
useful, but it must never be the reason a deploy or a health check stops.
"""

from workflow.deploy.registry.base import (
    Check,
    Device,
    DeviceStatus,
    DeploymentRegistry,
    KIND_FOLDER,
    KIND_MACHINE,
    KINDS,
    STATE_DEGRADED,
    STATE_OFFLINE,
    STATE_ONLINE,
    STATE_UNKNOWN,
    folder_id,
    slugify,
)

REGISTRY_TYPES = ("local", "notesgraph")

__all__ = [
    "Check", "Device", "DeviceStatus", "DeploymentRegistry",
    "KIND_MACHINE", "KIND_FOLDER", "KINDS",
    "STATE_ONLINE", "STATE_DEGRADED", "STATE_OFFLINE", "STATE_UNKNOWN",
    "folder_id", "slugify",
    "REGISTRY_TYPES", "build_registry", "get_registry", "MultiRegistry",
]


def build_registry(kind: str, options: dict) -> DeploymentRegistry:
    """Construct one registry by name. Raises ValueError on an unknown kind."""
    options = options or {}
    if kind == "local":
        from workflow.deploy.registry.local import LocalRegistry
        return LocalRegistry()
    if kind == "notesgraph":
        from workflow.deploy.registry.notesgraph import NotesGraphRegistry
        return NotesGraphRegistry(
            url=options.get("url", ""),
            workspace=options.get("workspace", ""),
            timeout=float(options.get("timeout", 15)),
        )
    raise ValueError(
        f"Unknown deployment registry '{kind}'. Supported: {', '.join(REGISTRY_TYPES)}"
    )


class MultiRegistry(DeploymentRegistry):
    """Write to the primary and every mirror; read from the primary."""

    name = "multi"
    authoritative = True

    def __init__(self, primary: DeploymentRegistry, mirrors=None):
        self.primary = primary
        self.mirrors = list(mirrors or [])

    @property
    def all(self) -> list:
        return [self.primary] + self.mirrors

    def _fanout(self, method: str, *args, **kwargs):
        result = getattr(self.primary, method)(*args, **kwargs)
        for mirror in self.mirrors:
            try:
                getattr(mirror, method)(*args, **kwargs)
            except Exception as e:
                # A registry you publish to is not one you depend on.
                print(f"⚠️  registry mirror '{mirror.name}' failed on {method}: {e}")
        return result

    def register(self, device): return self._fanout("register", device)
    def deregister(self, device_id): return self._fanout("deregister", device_id)
    def record_status(self, device_id, status): return self._fanout("record_status", device_id, status)

    def get(self, device_id): return self.primary.get(device_id)
    def list(self, kind=None, agent_target=None): return self.primary.list(kind, agent_target)

    def close(self):
        for registry in self.all:
            try:
                registry.close()
            except Exception:
                pass


def get_registry(cfg=None) -> DeploymentRegistry:
    """Build the configured registry plus mirrors."""
    if cfg is None:
        from workflow.config import load_effective_config
        cfg = load_effective_config()

    deploy_cfg = cfg.get("deploy") or {}
    kind = deploy_cfg.get("registry", "local")
    primary = build_registry(kind, deploy_cfg.get(kind, {}))
    if not primary.authoritative:
        raise ValueError(
            f"Registry '{kind}' cannot be the primary -- it is not a source of "
            f"truth. Set deploy.registry to 'local' and put '{kind}' in mirrors."
        )

    mirrors = []
    for mirror_kind in deploy_cfg.get("mirrors") or []:
        if mirror_kind == kind:
            continue
        try:
            mirrors.append(build_registry(mirror_kind, deploy_cfg.get(mirror_kind, {})))
        except Exception as e:
            print(f"⚠️  Skipping registry mirror '{mirror_kind}': {e}")

    return MultiRegistry(primary, mirrors) if mirrors else primary
