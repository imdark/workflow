"""SSH transport for deployment targets.

Every remote command goes through here. Connections are multiplexed with
OpenSSH's ControlMaster so a recipe of eight steps pays for one handshake,
which matters over a slow link to a rig in the field.

Nothing in here interprets what it runs: the steps come from recipes, the
transport just carries them and reports faithfully.
"""

import os
import shlex
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

CONTROL_DIR = Path.home() / ".wf" / "state" / "ssh"

# Fail fast on an unreachable rig rather than hanging a deploy.
CONNECT_TIMEOUT = 10
DEFAULT_TIMEOUT = 600


@dataclass
class Result:
    """The outcome of one remote command."""

    ok: bool
    exit_code: int
    stdout: str = ""
    stderr: str = ""
    duration_ms: int = 0
    command: str = ""

    @property
    def output(self) -> str:
        """stdout when there is any, else stderr -- what a human wants to see."""
        return (self.stdout or self.stderr).strip()

    def first_line(self) -> str:
        return self.output.splitlines()[0].strip() if self.output else ""


class SshError(RuntimeError):
    pass


class SshRunner:
    """Runs commands on one device."""

    def __init__(self, device, control_master: bool = True, extra_args=None):
        self.device = device
        self.control_master = control_master
        self.extra_args = list(extra_args or [])
        CONTROL_DIR.mkdir(parents=True, exist_ok=True)

    # ── connection plumbing ───────────────────────────────────────────────

    @property
    def _control_path(self) -> str:
        # Keep it short: a unix socket path is capped near 104 bytes, and the
        # usual %r@%h:%p template blows that for long hostnames.
        import hashlib
        digest = hashlib.sha256(
            f"{self.device.user}@{self.device.host}:{self.device.port}".encode()
        ).hexdigest()[:12]
        return str(CONTROL_DIR / f"cm-{digest}")

    def _base_args(self) -> list:
        args = [
            "ssh",
            "-o", f"ConnectTimeout={CONNECT_TIMEOUT}",
            "-o", "BatchMode=yes",           # never block on a password prompt
            "-o", "StrictHostKeyChecking=accept-new",
            "-p", str(self.device.port),
        ]
        if self.control_master:
            args += [
                "-o", "ControlMaster=auto",
                "-o", f"ControlPath={self._control_path}",
                "-o", "ControlPersist=60",
            ]
        args += self.extra_args
        return args

    # ── running ───────────────────────────────────────────────────────────

    def run(self, command: str, timeout: int = DEFAULT_TIMEOUT,
            cwd: Optional[str] = None) -> Result:
        """Run one shell command on the device.

        The command is handed to the remote login shell as a single string,
        so pipes and && work as written in a recipe. It is quoted on the way
        out so the local shell never interprets it.
        """
        remote = f"cd {shlex.quote(cwd)} && {command}" if cwd else command
        args = self._base_args() + [self.device.ssh_destination, remote]

        started = time.time()
        try:
            completed = subprocess.run(
                args, capture_output=True, text=True, timeout=timeout,
            )
        except subprocess.TimeoutExpired:
            return Result(ok=False, exit_code=124, command=command,
                          stderr=f"timed out after {timeout}s",
                          duration_ms=int((time.time() - started) * 1000))
        except FileNotFoundError:
            raise SshError("ssh is not on PATH")

        return Result(
            ok=completed.returncode == 0,
            exit_code=completed.returncode,
            stdout=completed.stdout or "",
            stderr=completed.stderr or "",
            duration_ms=int((time.time() - started) * 1000),
            command=command,
        )

    def reachable(self) -> Result:
        """Cheapest possible round trip, used as the first health probe."""
        return self.run("true", timeout=CONNECT_TIMEOUT + 5)

    def copy(self, local: str, remote: str) -> Result:
        """Copy a local file to the device."""
        args = ["scp", "-o", f"ConnectTimeout={CONNECT_TIMEOUT}",
                "-o", "StrictHostKeyChecking=accept-new",
                "-P", str(self.device.port), local,
                f"{self.device.ssh_destination}:{remote}"]
        started = time.time()
        completed = subprocess.run(args, capture_output=True, text=True, timeout=300)
        return Result(ok=completed.returncode == 0, exit_code=completed.returncode,
                      stdout=completed.stdout or "", stderr=completed.stderr or "",
                      duration_ms=int((time.time() - started) * 1000),
                      command=f"scp {local} -> {remote}")

    def close(self) -> None:
        """Tear down the multiplexed connection, if one is open."""
        if not self.control_master:
            return
        subprocess.run(
            self._base_args() + ["-O", "exit", self.device.ssh_destination],
            capture_output=True, text=True,
        )


def install_key(device, public_key: Optional[str] = None) -> Result:
    """Append this machine's public key to the device's authorized_keys.

    Runs with BatchMode off -- this is the one call that is *allowed* to
    prompt for a password, because it is how the password gets retired.
    """
    key_path = Path(public_key).expanduser() if public_key else _default_public_key()
    if not key_path or not key_path.exists():
        raise SshError(
            "No SSH public key found. Generate one with 'ssh-keygen -t ed25519'."
        )

    key = key_path.read_text().strip()
    # Idempotent: adding the same key twice is a no-op, and the file keeps
    # the permissions sshd insists on.
    remote = (
        "umask 077 && mkdir -p ~/.ssh && touch ~/.ssh/authorized_keys && "
        f"grep -qxF {shlex.quote(key)} ~/.ssh/authorized_keys || "
        f"echo {shlex.quote(key)} >> ~/.ssh/authorized_keys"
    )
    args = [
        "ssh", "-o", f"ConnectTimeout={CONNECT_TIMEOUT}",
        "-o", "StrictHostKeyChecking=accept-new",
        "-p", str(device.port), device.ssh_destination, remote,
    ]
    started = time.time()
    completed = subprocess.run(args, text=True, capture_output=True, timeout=120)
    return Result(ok=completed.returncode == 0, exit_code=completed.returncode,
                  stdout=completed.stdout or "", stderr=completed.stderr or "",
                  duration_ms=int((time.time() - started) * 1000),
                  command="install public key")


def _default_public_key() -> Optional[Path]:
    for name in ("id_ed25519.pub", "id_rsa.pub", "id_ecdsa.pub"):
        candidate = Path.home() / ".ssh" / name
        if candidate.exists():
            return candidate
    return None


# ── local transport ──────────────────────────────────────────────────────────

# Names that mean "the machine wf is running on".
_LOOPBACK = {"localhost", "127.0.0.1", "::1", "", "-"}


def is_local_host(host: str) -> bool:
    """Whether `host` refers to this machine.

    Matches the loopback names and this machine's own hostname, with or
    without a trailing `.local` -- `hostname` reports
    `Michaels-MacBook-Pro.local` while a user would register `michaels-macbook-pro`.
    """
    import socket

    candidate = (host or "").strip().lower()
    if candidate in _LOOPBACK:
        return True

    names = {socket.gethostname().lower()}
    try:
        names.add(socket.getfqdn().lower())
    except OSError:
        pass
    names |= {n[: -len(".local")] for n in list(names) if n.endswith(".local")}
    return candidate in names or candidate.removesuffix(".local") in names


class LocalRunner:
    """Runs commands on this machine, with SshRunner's interface.

    Registering the machine you are sitting at is a normal thing to want --
    it is where your checkouts are, and it is a perfectly good agent target
    -- and it should not require SSHing to yourself. Remote Login is off by
    default on macOS, so insisting on SSH would make the local case the
    hardest one.
    """

    def __init__(self, device, **_ignored):
        self.device = device

    def run(self, command: str, timeout: int = DEFAULT_TIMEOUT,
            cwd: Optional[str] = None) -> Result:
        started = time.time()
        # shell=True so a recipe's pipes and && behave as they would over ssh.
        try:
            completed = subprocess.run(
                command, shell=True, capture_output=True, text=True,
                timeout=timeout,
                cwd=str(Path(cwd).expanduser()) if cwd else None,
            )
        except subprocess.TimeoutExpired:
            return Result(ok=False, exit_code=124, command=command,
                          stderr=f"timed out after {timeout}s",
                          duration_ms=int((time.time() - started) * 1000))
        except (FileNotFoundError, NotADirectoryError) as e:
            return Result(ok=False, exit_code=127, command=command, stderr=str(e),
                          duration_ms=int((time.time() - started) * 1000))

        return Result(
            ok=completed.returncode == 0, exit_code=completed.returncode,
            stdout=completed.stdout or "", stderr=completed.stderr or "",
            duration_ms=int((time.time() - started) * 1000), command=command,
        )

    def reachable(self) -> Result:
        return Result(ok=True, exit_code=0, stdout="local", command="true")

    def copy(self, local: str, remote: str) -> Result:
        import shutil
        started = time.time()
        try:
            shutil.copy(str(Path(local).expanduser()), str(Path(remote).expanduser()))
            ok, code, err = True, 0, ""
        except OSError as e:
            ok, code, err = False, 1, str(e)
        return Result(ok=ok, exit_code=code, stderr=err,
                      duration_ms=int((time.time() - started) * 1000),
                      command=f"copy {local} -> {remote}")

    def close(self) -> None:
        return None


def runner_for(device, **kwargs):
    """Pick the transport for a device: local execution, or SSH."""
    if is_local_host(device.host):
        return LocalRunner(device)
    return SshRunner(device, **kwargs)
