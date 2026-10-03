"""Deployment toolkit: a registry of targets, and the machinery to act on them.

`registry` is the concept -- where the fleet is written down, with `local`
as the source of truth and implementations like `notesgraph` publishing it
somewhere shared.
"""

from workflow.deploy.registry import Device, DeviceStatus, get_registry

__all__ = ["Device", "DeviceStatus", "get_registry"]
