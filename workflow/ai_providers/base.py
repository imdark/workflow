import os
from abc import ABC, abstractmethod


class AIProvider(ABC):
    @abstractmethod
    def run(self, context: str, issue=None) -> str:
        pass


def apply_proxy_env(env=None) -> dict:
    """Route an agent launch through the capture proxy.

    Sets ANTHROPIC_BASE_URL / OPENAI_BASE_URL in `env` (defaulting to this
    process's environment, which subprocesses inherit) so the agent's model
    traffic is recorded. Controlled by config:

        proxy:
          enabled: true      # default: true
          auto_start: true   # start the daemon on demand (default: true)

    A base URL the user has already exported wins -- we never override an
    explicit choice. Returns the variables that were applied, empty if
    capture is off or the proxy could not be reached.
    """
    if env is None:
        env = os.environ

    try:
        from workflow.config import load_effective_config
        from workflow.proxy import daemon
    except ImportError:
        return {}

    config = load_effective_config()
    proxy_cfg = config.get("proxy") or {}
    if proxy_cfg.get("enabled") is False:
        return {}

    info = daemon.status()
    if not info.get("running"):
        if proxy_cfg.get("auto_start", True) is False:
            return {}
        info = daemon.start(config)
        if not info.get("running"):
            print("⚠️  Capture proxy unavailable; running without conversation capture")
            return {}

    applied = {}
    for key, value in daemon.env_for_agents(config).items():
        if env.get(key):
            continue  # respect an explicitly exported endpoint
        env[key] = value
        applied[key] = value
    return applied
