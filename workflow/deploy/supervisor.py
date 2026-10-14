"""Run `wf agent serve` as a service: a per-user supervisord that keeps it up.

`wf agent install` sets this up. The pieces, all under the user's home:

- supervisord itself, pip-installed into wf's own environment (pure Python;
  no Homebrew, no root), with its config in ~/.wf/supervisor/.
- a login item that starts it: a launchd agent on macOS, a user systemd unit
  on Linux.
- one program, `wf-agent`, running `wf agent serve --exit-on-update`. When
  wf's code changes (a `git pull` in its checkout) the runner finishes its
  jobs and exits, and supervisord starts it again on the new code.
"""

import os
import platform
import shutil
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional

PROGRAM = "wf-agent"
LAUNCHD_LABEL = "dev.wf.supervisord"
SYSTEMD_UNIT = "wf-supervisord.service"
MAX_SOCKET_PATH = 100


@dataclass(frozen=True)
class Paths:
    home: Path

    @property
    def root(self) -> Path:
        return self.home / ".wf" / "supervisor"

    @property
    def conf(self) -> Path:
        return self.root / "supervisord.conf"

    @property
    def programs(self) -> Path:
        return self.root / "programs"

    @property
    def socket(self) -> Path:
        path = self.root / "supervisor.sock"
        # Unix socket paths are capped near 104 bytes (macOS); a deep home
        # falls back to a short per-user one in the temp folder.
        if len(str(path)) > MAX_SOCKET_PATH:
            return Path(tempfile.gettempdir()) / f"wf-supervisor-{os.getuid()}.sock"
        return path

    @property
    def logs(self) -> Path:
        return self.home / ".wf" / "logs"

    @property
    def launchd_plist(self) -> Path:
        return self.home / "Library" / "LaunchAgents" / f"{LAUNCHD_LABEL}.plist"

    @property
    def systemd_unit(self) -> Path:
        return self.home / ".config" / "systemd" / "user" / SYSTEMD_UNIT

    def program_file(self, name: str = PROGRAM) -> Path:
        return self.programs / f"{name}.ini"


def default_paths() -> Paths:
    return Paths(Path.home())


def bin_dir() -> Path:
    """Where wf's interpreter is, and where pip puts supervisord beside it."""
    return Path(sys.executable).parent


def supervisord_bin() -> Path:
    return bin_dir() / "supervisord"


def supervisorctl_bin() -> Path:
    return bin_dir() / "supervisorctl"


def serve_command(device: Optional[str], max_jobs: int) -> List[str]:
    """The runner, on this very interpreter so it loads this wf."""
    command = [sys.executable, "-m", "workflow.cli", "agent", "serve",
               "--exit-on-update", "--max-jobs", str(max_jobs)]
    if device:
        command += ["--device", device]
    return command


def _quote(arg: str) -> str:
    # supervisord splits `command` shell-style.
    return f'"{arg}"' if any(c.isspace() for c in arg) else arg


def render_supervisord_conf(paths: Paths) -> str:
    return "\n".join([
        "; Written by `wf agent install`; rewritten on each install.",
        "[unix_http_server]",
        f"file={paths.socket}",
        "chmod=0700",
        "",
        "[supervisord]",
        f"logfile={paths.logs / 'supervisord.log'}",
        f"pidfile={paths.root / 'supervisord.pid'}",
        "nodaemon=true",
        "",
        "[rpcinterface:supervisor]",
        "supervisor.rpcinterface_factory = supervisor.rpcinterface:make_main_rpcinterface",
        "",
        "[supervisorctl]",
        f"serverurl=unix://{paths.socket}",
        "",
        "[include]",
        f"files={paths.programs}/*.ini",
        "",
    ])


def render_program(paths: Paths, command: List[str], cwd: Path,
                   env: dict, name: str = PROGRAM) -> str:
    environment = ",".join(f'{key}="{value}"' for key, value in sorted(env.items()))
    return "\n".join([
        "; Written by `wf agent install`.",
        f"[program:{name}]",
        f"command={' '.join(_quote(arg) for arg in command)}",
        f"directory={cwd}",
        f"environment={environment}",
        "autostart=true",
        # Every exit is a restart: --exit-on-update exits cleanly on purpose.
        "autorestart=true",
        "startsecs=5",
        "startretries=10",
        # Ctrl-C is what serve stops on; give running jobs time to wind down.
        "stopsignal=INT",
        "stopwaitsecs=60",
        "redirect_stderr=true",
        f"stdout_logfile={paths.logs / (name + '.log')}",
        "stdout_logfile_maxbytes=10MB",
        "stdout_logfile_backups=3",
        "",
    ])


def render_launchd_plist(paths: Paths, env: dict) -> str:
    env_xml = "".join(
        f"\n      <key>{key}</key><string>{value}</string>"
        for key, value in sorted(env.items())
    )
    args = [str(supervisord_bin()), "-n", "-c", str(paths.conf)]
    args_xml = "".join(f"\n      <string>{arg}</string>" for arg in args)
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
  <dict>
    <key>Label</key><string>{LAUNCHD_LABEL}</string>
    <key>ProgramArguments</key>
    <array>{args_xml}
    </array>
    <key>EnvironmentVariables</key>
    <dict>{env_xml}
    </dict>
    <key>RunAtLoad</key><true/>
    <key>KeepAlive</key><true/>
    <key>StandardOutPath</key><string>{paths.logs / 'supervisord.launchd.log'}</string>
    <key>StandardErrorPath</key><string>{paths.logs / 'supervisord.launchd.log'}</string>
  </dict>
</plist>
"""


def render_systemd_unit(paths: Paths, env: dict) -> str:
    env_lines = "\n".join(f'Environment="{key}={value}"' for key, value in sorted(env.items()))
    return f"""# Written by `wf agent install`.
[Unit]
Description=wf supervisord (runs wf agent serve)

[Service]
ExecStart={supervisord_bin()} -n -c {paths.conf}
{env_lines}
Restart=always

[Install]
WantedBy=default.target
"""


# Which Claude account jobs run as. Without it `claude` falls back to
# ~/.claude, whose login may be a different account or long expired.
CLAUDE_ENV = ("CLAUDE_CONFIG_DIR",)


def service_env() -> dict:
    """What the runner needs from this shell: its PATH (tmux, claude, gh,
    git), home, locale and Claude config dir. Captured at install, as a login
    item gets none."""
    env = {"PATH": os.environ.get("PATH", ""), "HOME": str(Path.home()),
           "PYTHONUNBUFFERED": "1"}
    for key in ("LANG", "LC_ALL", "USER", *CLAUDE_ENV):
        if os.environ.get(key):
            env[key] = os.environ[key]
    return env


def ensure_supervisor(run=subprocess.run) -> None:
    """supervisord in wf's own environment, installed if missing."""
    if supervisord_bin().exists() and supervisorctl_bin().exists():
        return
    run([sys.executable, "-m", "pip", "install", "--quiet", "supervisor"], check=True)


def ctl(paths: Paths, *args: str, run=subprocess.run, check: bool = False):
    return run([str(supervisorctl_bin()), "-c", str(paths.conf), *args],
               capture_output=True, text=True, check=check)


def _wait_for_socket(paths: Paths, timeout: float = 20.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if paths.socket.exists():
            return True
        time.sleep(0.5)
    return False


def start_supervisord(paths: Paths, env: dict, run=subprocess.run) -> str:
    """Install the login item and (re)start supervisord through it."""
    system = platform.system()
    if system == "Darwin":
        plist = paths.launchd_plist
        plist.parent.mkdir(parents=True, exist_ok=True)
        plist.write_text(render_launchd_plist(paths, env))
        domain = f"gui/{os.getuid()}"
        run(["launchctl", "bootout", domain, str(plist)], capture_output=True)
        run(["launchctl", "bootstrap", domain, str(plist)], check=True, capture_output=True)
        return f"launchd agent {LAUNCHD_LABEL} ({plist})"
    if system == "Linux" and shutil.which("systemctl"):
        unit = paths.systemd_unit
        unit.parent.mkdir(parents=True, exist_ok=True)
        unit.write_text(render_systemd_unit(paths, env))
        run(["systemctl", "--user", "daemon-reload"], check=True)
        run(["systemctl", "--user", "enable", "--now", SYSTEMD_UNIT], check=True)
        run(["systemctl", "--user", "restart", SYSTEMD_UNIT], check=True)
        return f"systemd user unit {SYSTEMD_UNIT}"
    # Nothing to start it at login: run it in the background for now.
    subprocess.Popen([str(supervisord_bin()), "-c", str(paths.conf)],
                     start_new_session=True, env={**os.environ, **env})
    return "supervisord in the background (not started at login on this system)"


def install(device: Optional[str], cwd: Path, max_jobs: int,
            paths: Optional[Paths] = None, run=subprocess.run) -> str:
    """Set the service up (or update it) and start it. Returns how it starts."""
    paths = paths or default_paths()
    ensure_supervisor(run)
    env = service_env()
    for directory in (paths.root, paths.programs, paths.logs):
        directory.mkdir(parents=True, exist_ok=True)
    paths.conf.write_text(render_supervisord_conf(paths))
    paths.program_file().write_text(
        render_program(paths, serve_command(device, max_jobs), cwd, env))
    how = start_supervisord(paths, env, run)
    if not _wait_for_socket(paths):
        raise RuntimeError(f"supervisord didn't come up; see {paths.logs / 'supervisord.log'}")
    ctl(paths, "reread", run=run)
    ctl(paths, "update", run=run)
    return how


def uninstall(paths: Optional[Paths] = None, run=subprocess.run) -> None:
    """Stop the runner and remove it; supervisord and its login item go too."""
    paths = paths or default_paths()
    if paths.socket.exists():
        ctl(paths, "stop", PROGRAM, run=run)
    paths.program_file().unlink(missing_ok=True)
    system = platform.system()
    if system == "Darwin" and paths.launchd_plist.exists():
        run(["launchctl", "bootout", f"gui/{os.getuid()}", str(paths.launchd_plist)],
            capture_output=True)
        paths.launchd_plist.unlink()
    elif system == "Linux" and paths.systemd_unit.exists():
        run(["systemctl", "--user", "disable", "--now", SYSTEMD_UNIT])
        paths.systemd_unit.unlink()
    elif paths.socket.exists():
        ctl(paths, "shutdown", run=run)


def status(paths: Optional[Paths] = None, run=subprocess.run) -> str:
    paths = paths or default_paths()
    if not paths.program_file().exists():
        return "not installed (run: wf agent install)"
    if not paths.socket.exists():
        return "installed, but supervisord isn't running"
    result = ctl(paths, "status", PROGRAM, run=run)
    return (result.stdout or result.stderr).strip()
