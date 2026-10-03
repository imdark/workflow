"""NotesGraph implementation of the deployment registry.

Publishes the fleet into a NotesGraph workspace's device inventory, so
registered machines and folders show up on your account rather than only in
a local config file. Backed by the `inventory` plugin's REST API
(`/api/inventory/...`), authenticated with a Personal Access Token the same
way NotesGraph's own notes CLI authenticates.

Configuration:

    deploy:
      registry: local          # authoritative source of truth
      mirrors: [notesgraph]
      notesgraph:
        url: https://app.notesgraph.com
        workspace: <workspace id>

The token is kept in the Keychain (`wf deploy notes-login`), never in
config.yaml. Never authoritative: NotesGraph is where the fleet is
published, not where it is decided, so an outage degrades to a warning
rather than blocking a deploy.
"""

import json
import urllib.error
import urllib.request
from typing import Optional

from workflow.deploy.registry.base import (
    Device,
    DeviceStatus,
    DeploymentRegistry,
)

KEYCHAIN_SERVICE = "workflow-notesgraph"
KEYCHAIN_USER = "pat"
DEFAULT_URL = "https://app.notesgraph.com"


# ── token storage ────────────────────────────────────────────────────────────

def load_token() -> Optional[str]:
    try:
        import keyring
        return keyring.get_password(KEYCHAIN_SERVICE, KEYCHAIN_USER)
    except Exception:
        return None


def save_token(token: str) -> bool:
    try:
        import keyring
        keyring.set_password(KEYCHAIN_SERVICE, KEYCHAIN_USER, token)
        return True
    except Exception:
        return False


def delete_token() -> bool:
    try:
        import keyring
        keyring.delete_password(KEYCHAIN_SERVICE, KEYCHAIN_USER)
        return True
    except Exception:
        return False


class NotesGraphError(RuntimeError):
    pass


class NotesGraphClient:
    """Thin REST client for the NotesGraph inventory plugin."""

    def __init__(self, url: str = DEFAULT_URL, token: Optional[str] = None,
                 timeout: float = 15.0):
        self.url = (url or DEFAULT_URL).rstrip("/")
        self.token = token or load_token()
        self.timeout = timeout

    def _request(self, method: str, path: str, payload: Optional[dict] = None) -> dict:
        if not self.token:
            raise NotesGraphError(
                "No NotesGraph token. Run 'wf deploy notes-login <token>' "
                "with a Personal Access Token from your account settings."
            )
        body = json.dumps(payload).encode("utf-8") if payload is not None else None
        request = urllib.request.Request(
            f"{self.url}{path}", data=body, method=method,
            headers={
                "Authorization": f"Bearer {self.token}",
                "Content-Type": "application/json",
                "Accept": "application/json",
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                raw = response.read().decode("utf-8")
                return json.loads(raw) if raw.strip() else {}
        except urllib.error.HTTPError as e:
            detail = ""
            try:
                detail = e.read().decode("utf-8")[:300]
            except Exception:
                pass
            if e.code in (401, 403):
                raise NotesGraphError(
                    f"NotesGraph rejected the token ({e.code}). "
                    "Re-run 'wf deploy notes-login <token>'."
                ) from e
            raise NotesGraphError(f"NotesGraph {method} {path} failed: {e.code} {detail}") from e
        except (urllib.error.URLError, OSError) as e:
            raise NotesGraphError(f"NotesGraph unreachable at {self.url}: {e}") from e
        except json.JSONDecodeError as e:
            raise NotesGraphError(f"NotesGraph returned invalid JSON: {e}") from e

    def session(self) -> dict:
        """Validate the token; returns the user and their workspace ids."""
        return self._request("GET", "/api/inventory/session")

    def list_devices(self, workspace: str) -> list:
        return self._request("GET", f"/api/inventory/workspaces/{workspace}/devices").get("devices", [])

    def upsert_device(self, workspace: str, payload: dict) -> dict:
        return self._request("POST", f"/api/inventory/workspaces/{workspace}/devices", payload).get("device", {})

    def delete_device(self, workspace: str, key: str) -> bool:
        result = self._request("DELETE", f"/api/inventory/workspaces/{workspace}/devices/{key}")
        return bool(result.get("ok"))

    def push_status(self, workspace: str, key: str, payload: dict) -> dict:
        return self._request(
            "POST", f"/api/inventory/workspaces/{workspace}/devices/{key}/status", payload
        ).get("device", {})


# ── wire format ──────────────────────────────────────────────────────────────

def device_to_payload(device: Device) -> dict:
    """wf Device -> inventory API body.

    `key` is the wf-side id and the upsert key; the inventory assigns its
    own uuid, which comes back as `id` and is kept as `external_id`.
    """
    return {
        "key": device.id,
        "name": device.name or device.id,
        "kind": device.kind,
        "host": device.host,
        "user": device.user,
        "port": device.port,
        "parentKey": device.parent,
        "path": device.path,
        "recipe": device.recipe,
        "repo": device.repo,
        "branch": device.branch,
        "channel": device.channel,
        "pin": device.pin,
        "agentTarget": device.agent_target,
        "labels": device.labels or {},
    }


def payload_to_device(payload: dict) -> Device:
    device = Device(
        id=payload.get("key") or payload.get("id", ""),
        name=payload.get("name", ""),
        kind=payload.get("kind", "machine"),
        host=payload.get("host", ""),
        user=payload.get("user", ""),
        port=int(payload.get("port") or 22),
        parent=payload.get("parentKey"),
        path=payload.get("path"),
        recipe=payload.get("recipe", "generic"),
        repo=payload.get("repo"),
        branch=payload.get("branch", "main"),
        channel=payload.get("channel", "stable"),
        pin=payload.get("pin"),
        agent_target=bool(payload.get("agentTarget")),
        labels=payload.get("labels") or {},
        external_id=payload.get("id"),
    )
    device.status = DeviceStatus(
        state=payload.get("state", "unknown"),
        checked_at=float(payload.get("checkedAt") or 0),
        version=payload.get("version"),
        detail=payload.get("statusDetail", "") or "",
        checks=payload.get("checks") or [],
    )
    return device


class NotesGraphRegistry(DeploymentRegistry):
    name = "notesgraph"
    authoritative = False

    def __init__(self, url: str = DEFAULT_URL, workspace: str = "",
                 token: Optional[str] = None, timeout: float = 15.0):
        if not workspace:
            raise ValueError(
                "notesgraph registry needs a workspace id "
                "(deploy.notesgraph.workspace); 'wf deploy notes-login' lists yours"
            )
        self.workspace = workspace
        self.client = NotesGraphClient(url=url, token=token, timeout=timeout)

    def register(self, device: Device) -> Device:
        stored = self.client.upsert_device(self.workspace, device_to_payload(device))
        if stored.get("id"):
            device.external_id = stored["id"]
        return device

    def deregister(self, device_id: str) -> bool:
        return self.client.delete_device(self.workspace, device_id)

    def get(self, device_id: str) -> Optional[Device]:
        return next((d for d in self.list() if d.id == device_id), None)

    def list(self, kind: Optional[str] = None,
             agent_target: Optional[bool] = None) -> list:
        devices = [payload_to_device(p) for p in self.client.list_devices(self.workspace)]
        if kind:
            devices = [d for d in devices if d.kind == kind]
        if agent_target is not None:
            devices = [d for d in devices if d.agent_target == agent_target]
        return sorted(devices, key=lambda d: d.id)

    def record_status(self, device_id: str, status: DeviceStatus) -> None:
        self.client.push_status(self.workspace, device_id, {
            "state": status.state,
            "version": status.version,
            "statusDetail": status.detail,
            "checkedAt": status.checked_at,
            "checks": status.checks,
        })
