"""Reverse tunnels: let a deployed target reach back to this machine.

A rig in the field has no credentials and no route to your laptop. An
`ssh -R` tunnel gives it one port: the capture proxy. With
ANTHROPIC_BASE_URL pointed down that tunnel, an agent running on the robot
authenticates as your account, has its conversation captured into your
store, and is tagged with the task you have open -- and no key ever lands
on the machine.
"""

import shlex
import subprocess
import time
from contextlib import contextmanager
from typing import Optional

from workflow.deploy.ssh import CONNECT_TIMEOUT


def remote_env(remote_port: int) -> dict:
    """Environment an agent on the target needs to use the tunnel."""
    base = f"http://127.0.0.1:{remote_port}"
    return {"ANTHROPIC_BASE_URL": base, "OPENAI_BASE_URL": f"{base}/v1"}


def export_line(remote_port: int) -> str:
    return " ".join(f"{k}={shlex.quote(v)}" for k, v in remote_env(remote_port).items())


def _tunnel_args(device, remote_port: int, local_port: int) -> list:
    return [
        "ssh", "-N",
        "-o", f"ConnectTimeout={CONNECT_TIMEOUT}",
        "-o", "BatchMode=yes",
        "-o", "StrictHostKeyChecking=accept-new",
        # Drop a tunnel that has silently died rather than leaving the rig
        # pointed at a black hole.
        "-o", "ServerAliveInterval=15",
        "-o", "ServerAliveCountMax=3",
        "-o", "ExitOnForwardFailure=yes",
        "-p", str(device.port),
        "-R", f"{remote_port}:127.0.0.1:{local_port}",
        device.ssh_destination,
    ]


def open_tunnel(device, remote_port: int = 8099, local_port: Optional[int] = None):
    """Start a reverse tunnel and return the Popen handle.

    Raises RuntimeError if ssh exits immediately, which is what happens when
    the remote port is already bound -- `ExitOnForwardFailure` turns that
    into a real error instead of a tunnel that silently forwards nothing.
    """
    if local_port is None:
        from workflow.proxy import daemon
        local_port = int(daemon.base_url().rsplit(":", 1)[-1])

    process = subprocess.Popen(
        _tunnel_args(device, remote_port, local_port),
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    )
    time.sleep(1.0)
    if process.poll() is not None:
        _, stderr = process.communicate(timeout=5)
        raise RuntimeError(
            f"tunnel to {device.ssh_destination} failed: {(stderr or '').strip() or 'ssh exited'}"
        )
    return process


@contextmanager
def reverse_tunnel(device, remote_port: int = 8099, local_port: Optional[int] = None):
    """Hold a reverse tunnel open for the duration of the block."""
    process = open_tunnel(device, remote_port, local_port)
    try:
        yield process
    finally:
        process.terminate()
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()
