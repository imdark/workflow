"""What an agent device must have installed before it can take jobs.

Agent jobs run inside tmux so a person can attach and watch one (see
`jobs.run_job`). A device without it is caught here, before a deploy or
`wf agent serve` starts, with the exact command to install it for that
machine -- rather than at the first job, on a machine nobody is looking at.

Checks take any runner with `.run(command)` -- SshRunner for a remote
device, LocalRunner for this one -- so the same probe works for both.
"""

from dataclasses import dataclass
from typing import Optional

# Non-interactive ssh shells often lack the Homebrew prefixes on PATH, so a
# tmux or brew that is installed would look missing. Look there too.
EXTRA_BIN_DIRS = ("/opt/homebrew/bin", "/usr/local/bin", "/home/linuxbrew/.linuxbrew/bin")

# Probed in order; the first one present on the target decides the command.
PACKAGE_MANAGERS = ("brew", "apt-get", "dnf", "yum", "pacman", "apk", "zypper")

INSTALL_COMMANDS = {
    "brew": "brew install tmux",
    "apt-get": "sudo apt-get update && sudo apt-get install -y tmux",
    "dnf": "sudo dnf install -y tmux",
    "yum": "sudo yum install -y tmux",
    "pacman": "sudo pacman -S --noconfirm tmux",
    "apk": "sudo apk add tmux",
    "zypper": "sudo zypper install -y tmux",
}

HOMEBREW_INSTALL = ('/bin/bash -c "$(curl -fsSL '
                    'https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"')

# One round trip: either "have" and tmux's path, or the OS name followed by
# each package manager that exists.
_PROBE = (
    'PATH="$PATH:' + ":".join(EXTRA_BIN_DIRS) + '"; '
    'if t=$(command -v tmux); then echo "have $t"; else '
    'echo "os $(uname -s)"; '
    f'for m in {" ".join(PACKAGE_MANAGERS)}; do '
    'command -v "$m" >/dev/null 2>&1 && echo "pm $m"; done; fi'
)


@dataclass
class TmuxCheck:
    installed: bool
    path: Optional[str] = None
    os_name: Optional[str] = None
    package_manager: Optional[str] = None
    # Set when the probe itself could not run (unreachable host, etc.).
    error: Optional[str] = None

    @property
    def install_steps(self) -> list:
        """The commands to run on the device, in order."""
        if self.package_manager:
            return [INSTALL_COMMANDS[self.package_manager]]
        if self.os_name == "Darwin":
            # A Mac without Homebrew: install it, then tmux.
            return [HOMEBREW_INSTALL, INSTALL_COMMANDS["brew"]]
        return []


def check_tmux(runner) -> TmuxCheck:
    result = runner.run(_PROBE, timeout=30)
    if not result.ok and not result.stdout.strip():
        return TmuxCheck(installed=False, error=result.output or f"exit {result.exit_code}")

    os_name, managers = None, []
    for line in result.stdout.splitlines():
        kind, _, value = line.strip().partition(" ")
        if kind == "have":
            return TmuxCheck(installed=True, path=value)
        if kind == "os":
            os_name = value
        elif kind == "pm":
            managers.append(value)
    return TmuxCheck(installed=False, os_name=os_name,
                     package_manager=managers[0] if managers else None)


def tmux_missing_message(check: TmuxCheck, where: str) -> list:
    """Lines telling a person how to fix a failed check on `where`."""
    if check.error:
        return [f"❌ Couldn't check for tmux on {where}: {check.error}"]
    lines = [
        f"❌ tmux is not installed on {where}.",
        "   Agent jobs run inside tmux so you can attach and watch them "
        "(wf agent attach).",
    ]
    steps = check.install_steps
    if steps:
        lines.append(f"   Install it on {where} with:")
        lines.extend(f"     {step}" for step in steps)
    else:
        lines.append(f"   Install tmux with {where}'s package manager "
                     f"({check.os_name or 'unknown OS'}; none of "
                     f"{', '.join(PACKAGE_MANAGERS)} found).")
    return lines
