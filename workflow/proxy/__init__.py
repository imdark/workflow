"""Capturing proxy for agent model traffic.

`workflow.proxy.server` runs the HTTP listener; `protocols` normalizes the
wire formats; `identity` ties a session to a workflow task; `sanitize`
prepares transcripts for summarization without touching what is stored.
"""

from workflow.proxy.server import CaptureProxy, serve

__all__ = ["CaptureProxy", "serve"]
