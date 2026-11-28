"""How to reach a machine over SSH: its key, user, port and options.

`wf deploy ssh prod` should not need `-i ~/.ssh/some.pem ubuntu@1.2.3.4`
typed out each time. The answers are written down once, per project, with
a global fallback:

    projects:
      personal:
        ssh:
          default: prod                 # the machine when none is named
          user: ubuntu                  # defaults for every machine
          key: ~/.ssh/project.pem
          port: 22
          options: [ServerAliveInterval=30]
          hosts:
            prod: {host: 18.0.0.1}      # may override user/key/port/options
    ssh:                                # same shape, for every project
      key: ~/.ssh/id_ed25519

A name is looked up in the project's hosts, then the global ones, then the
deployment registry (`wf deploy add`). Each setting comes from the first
place that has it: the host's own entry, the project's defaults, the global
defaults. A registered target's user and non-default port count as its own.

These live under `ssh`, not `deploy`: the registry's records are published
to its mirrors, and a key path has no business there.
"""

import os
import shlex
from dataclasses import dataclass, field, replace
from typing import Optional

from workflow.deploy.registry.base import KIND_FOLDER, Device

SETTINGS = ("user", "key", "port", "options")

# ssh's own options that take a value (`ssh -L 80:x:80`), from its usage
# line; every other single-letter option stands alone.
SSH_VALUE_FLAGS = set("BbcDEeFIiJLlmOoPpQRSWw")


@dataclass
class SshTarget:
    """One machine, with everything needed to connect resolved."""

    name: str
    host: str
    user: str = ""
    key: Optional[str] = None
    port: int = 22
    options: list = field(default_factory=list)
    # Folder targets land in their folder.
    path: Optional[str] = None
    # Where each setting came from, for `wf deploy ssh --dry-run` and `ssh-config`.
    sources: dict = field(default_factory=dict)

    @property
    def destination(self) -> str:
        return f"{self.user}@{self.host}" if self.user else self.host


def sections(project: Optional[str] = None, cfg: Optional[dict] = None) -> list:
    """[(label, section)] in the order settings are taken from: the
    project's `ssh`, then the global one."""
    from workflow.config import load_config
    cfg = load_config() if cfg is None else cfg
    found = []
    if project:
        section = ((cfg.get("projects") or {}).get(project) or {}).get("ssh")
        if isinstance(section, dict):
            found.append((f"project {project}", section))
    if isinstance(cfg.get("ssh"), dict):
        found.append(("global", cfg["ssh"]))
    return found


def default_name(project: Optional[str] = None, cfg: Optional[dict] = None) -> Optional[str]:
    """The machine `wf deploy ssh` connects to when none is named."""
    for _, section in sections(project, cfg):
        if section.get("default"):
            return str(section["default"])
    return None


def host_names(project: Optional[str] = None, cfg: Optional[dict] = None) -> list:
    names = []
    for _, section in sections(project, cfg):
        names += [n for n in (section.get("hosts") or {}) if n not in names]
    return names


def resolve(name: str, project: Optional[str] = None, cfg: Optional[dict] = None,
            registry=None) -> Optional[SshTarget]:
    """The machine called `name`, or None when nothing knows it."""
    found = sections(project, cfg)
    layers = []                     # (label, settings), most specific first
    host = path = None
    for label, section in found:
        entry = (section.get("hosts") or {}).get(name)
        if isinstance(entry, str):
            entry = {"host": entry}
        if isinstance(entry, dict) and entry.get("host"):
            host = str(entry["host"])
            layers.append((f"{label}, host {name}", entry))
            break
    if host is None:
        device = registry.get(name) if registry else None
        if not device:
            return None
        host, path = device.host, device.path if device.kind == KIND_FOLDER else None
        # 22 is the registry's default, so it says nothing about the machine.
        own = {"user": device.user or None, "port": device.port if device.port != 22 else None}
        layers.append((f"target {name}", own))
    layers += [(f"{label} defaults", section) for label, section in found]

    target = SshTarget(name=name, host=host, path=path)
    for setting in SETTINGS:
        for label, values in layers:
            value = values.get(setting)
            if value in (None, "", []):
                continue
            if setting == "port":
                value = int(value)
            elif setting == "options":
                value = [str(v) for v in (value if isinstance(value, list) else [value])]
            else:
                value = str(value)
            setattr(target, setting, value)
            target.sources[setting] = label
            break
    return target


def for_device(device: Device, project: Optional[str] = None, cfg: Optional[dict] = None) -> SshTarget:
    """The settings the other `wf deploy` commands connect to a registered
    target with: its own, then the project's and the global defaults."""

    class _Only:
        def get(self, _name):
            return device

    return resolve(device.id, project, cfg, registry=_Only()) or SshTarget(name=device.id, host=device.host)


def connection_args(target: SshTarget, skip: str = "") -> list:
    """ssh's arguments for the key and options (not the port or the
    destination). `skip` holds option letters the caller already passed."""
    args = []
    if target.key and "i" not in skip:
        # IdentitiesOnly: offer this key, not every one the agent holds; a
        # server drops the connection after a few wrong ones.
        args += ["-i", os.path.expanduser(target.key), "-o", "IdentitiesOnly=yes"]
    for option in target.options:
        args += ["-o", option]
    return args


def split_args(args: list) -> tuple:
    """(ssh options, remote command) from what followed the machine's name,
    read the way ssh reads its own command line: options first, a value
    after those that take one, and the command from the first other word."""
    args = list(args)
    i = 0
    while i < len(args):
        word = args[i]
        if word == "--":
            return args[:i], args[i + 1:]
        if not word.startswith("-") or word == "-":
            break
        # `-L 80:x:80` takes the next word; `-L80:x:80` and `-vvv` don't.
        i += 2 if len(word) == 2 and word[1] in SSH_VALUE_FLAGS else 1
    return args[:i], args[i:]


def _letters(options: list) -> str:
    """The option letters a forwarded command line already sets."""
    return "".join(word[1] for word in options if len(word) >= 2 and word.startswith("-"))


def ssh_argv(target: SshTarget, options: Optional[list] = None, command: Optional[list] = None) -> list:
    """The ssh command line for `target`.

    Forwarded options go first and win: ssh keeps the first value it is
    given for most settings, so a `-p` or `-l` on the command line replaces
    the configured one rather than being appended to it.
    """
    options, command = list(options or []), list(command or [])
    given = _letters(options)
    argv = ["ssh", *options, *connection_args(target, skip=given)]
    if target.port != 22 and "p" not in given:
        argv += ["-p", str(target.port)]
    argv.append(target.host if "l" in given else target.destination)
    remote = " ".join(command)
    if target.path:
        # A folder target is a place on a machine: land in it.
        where = f"cd {shlex.quote(target.path)}"
        if remote:
            remote = f"{where} && {remote}"
        else:
            # A shell that stays needs a terminal; -t comes before the host.
            argv.insert(len(argv) - 1, "-t")
            remote = f"{where} && exec \"${{SHELL:-sh}}\" -l"
        return argv + [remote]
    return argv + command


def device_with_settings(device: Device, target: SshTarget) -> Device:
    """`device` as the transport should see it: the resolved user and port."""
    return replace(device, user=target.user or device.user, port=target.port)


def current_project() -> Optional[str]:
    """The project whose defaults apply: the folder's, else the current one."""
    try:
        from workflow.projects import get_current_project, project_for_path
        return project_for_path() or get_current_project()
    except Exception:                                       # noqa: BLE001
        return None
