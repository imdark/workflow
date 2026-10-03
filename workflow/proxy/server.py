"""Capturing reverse proxy for agent model traffic.

Point an agent's base URL here and every request is forwarded upstream
untouched while the conversation is recorded locally:

    ANTHROPIC_BASE_URL=http://127.0.0.1:8099          (Claude Code)
    OPENAI_BASE_URL=http://127.0.0.1:8099/v1          (Codex, opencode)

Design rules:
  - Forwarding is transparent. Capture failures are logged, never raised:
    a broken store must not break the user's agent session.
  - Credentials are forwarded and never stored. Authorization, x-api-key
    and cookies are excluded from every record and log line.
  - Streaming is preserved byte-for-byte to the client; the assembler sees
    a copy of the same bytes.
"""

import asyncio
import json
import time
from typing import Optional

from aiohttp import ClientSession, ClientTimeout, web
from aiohttp.client_exceptions import ClientConnectionResetError

# Typed key for app state, as aiohttp prefers over a bare string.
_IDLE_TASK = web.AppKey("wf_idle_task", asyncio.Task)

from workflow.conversation_store import SessionMeta, get_store, reconcile
from workflow.proxy import identity, protocols

# Never forwarded upstream: hop-by-hop or recomputed by the client library.
_DROP_REQUEST_HEADERS = {
    "host", "content-length", "connection", "keep-alive", "transfer-encoding",
    "upgrade", "proxy-authorization", "proxy-connection", "te", "trailer",
    "accept-encoding",
}

# Never copied back to the client: we hand back decoded bytes, so any
# content-encoding or length from upstream would be a lie.
_DROP_RESPONSE_HEADERS = {
    "content-encoding", "content-length", "transfer-encoding", "connection",
    "keep-alive",
}

# Redacted everywhere, including debug logging.
_SECRET_HEADERS = {"authorization", "x-api-key", "cookie", "set-cookie", "proxy-authorization"}

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8099
DEFAULT_UPSTREAM = {
    "anthropic": "https://api.anthropic.com",
    "openai": "https://api.openai.com",
}


class CaptureProxy:
    def __init__(self, config: Optional[dict] = None):
        if config is None:
            from workflow.config import load_effective_config
            config = load_effective_config()
        self.config = config
        proxy_cfg = config.get("proxy") or {}
        self.host = proxy_cfg.get("host", DEFAULT_HOST)
        self.port = int(proxy_cfg.get("port", DEFAULT_PORT))
        self.upstream = {**DEFAULT_UPSTREAM, **(proxy_cfg.get("upstream") or {})}
        self.idle_seconds = int(proxy_cfg.get("idle_seconds", 300))
        self.verbose = bool(proxy_cfg.get("verbose", False))

        self.store = get_store(config)
        self._client: Optional[ClientSession] = None
        # session_id -> last activity epoch, for idle-based finalization
        self._active = {}
        self._finalized = set()
        # Sessions whose system prompt is already on record.
        self._system_captured = set()
        self._lock = asyncio.Lock()

    # ── lifecycle ─────────────────────────────────────────────────────────

    async def _on_startup(self, app):
        # No total timeout: agent turns legitimately run for many minutes.
        self._client = ClientSession(timeout=ClientTimeout(total=None, sock_connect=30))
        app[_IDLE_TASK] = asyncio.create_task(self._idle_loop())

    async def _on_cleanup(self, app):
        task = app.get(_IDLE_TASK)
        if task:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        if self._client:
            await self._client.close()
        # Summarize anything still open so a Ctrl-C does not lose the ladder.
        for session_id in list(self._active):
            await self._finalize(session_id)
        self.store.close()

    def build_app(self) -> web.Application:
        app = web.Application(client_max_size=1024 ** 3)
        app.on_startup.append(self._on_startup)
        app.on_cleanup.append(self._on_cleanup)
        app.router.add_get("/_wf/health", self._health)
        app.router.add_get("/_wf/sessions", self._sessions)
        app.router.add_route("*", "/{tail:.*}", self._handle)
        return app

    def run(self) -> None:
        web.run_app(self.build_app(), host=self.host, port=self.port, print=None,
                    access_log=None)

    # ── introspection endpoints ───────────────────────────────────────────

    async def _health(self, request):
        return web.json_response({
            "status": "ok",
            "store": self.store.name,
            "active_sessions": len(self._active),
            "upstream": self.upstream,
        })

    async def _sessions(self, request):
        task_key = request.query.get("task")
        sessions = self.store.list_sessions(task_key=task_key, limit=50)
        return web.json_response([s.to_dict() for s in sessions])

    # ── the proxy itself ──────────────────────────────────────────────────

    def _upstream_base(self, protocol: Optional[str], path: str) -> str:
        if protocol == protocols.ANTHROPIC:
            return self.upstream["anthropic"]
        if protocol in (protocols.OPENAI_CHAT, protocols.OPENAI_RESPONSES):
            return self.upstream["openai"]
        # Unknown route: guess from the path shape so auxiliary endpoints
        # (count_tokens, embeddings, models) still reach the right vendor.
        return self.upstream["anthropic"] if "/messages" in path else self.upstream["openai"]

    @staticmethod
    def _strip_prefix(path: str) -> str:
        """Remove an agent-name prefix so upstream sees a canonical path.

        `/claude-code/v1/messages` -> `/v1/messages`. A path already starting
        with /v1 is left alone.
        """
        if path.startswith("/v1/") or path == "/v1":
            return path
        parts = path.lstrip("/").split("/", 1)
        if len(parts) == 2 and parts[1].startswith("v1/"):
            return "/" + parts[1]
        return path

    async def _handle(self, request: web.Request) -> web.StreamResponse:
        path = request.path
        protocol = protocols.detect_protocol(path)
        upstream_path = self._strip_prefix(path)
        url = self._upstream_base(protocol, path).rstrip("/") + upstream_path
        if request.query_string:
            url += "?" + request.query_string

        body = await request.read()
        headers = {
            k: v for k, v in request.headers.items()
            if k.lower() not in _DROP_REQUEST_HEADERS
        }

        capture = None
        if protocol and request.method == "POST" and body:
            try:
                capture = self._begin_capture(protocol, path, request.headers, body)
            except Exception as e:
                self._log(f"capture setup failed: {e}")

        try:
            upstream = await self._client.request(
                request.method, url, headers=headers, data=body or None,
                allow_redirects=False,
            )
        except Exception as e:
            self._log(f"upstream error for {upstream_path}: {e}")
            return web.json_response(
                {"error": {"type": "wf_proxy_upstream_error", "message": str(e)}},
                status=502,
            )

        response = web.StreamResponse(
            status=upstream.status,
            headers={k: v for k, v in upstream.headers.items()
                     if k.lower() not in _DROP_RESPONSE_HEADERS},
        )
        await response.prepare(request)

        assembler = capture["assembler"] if capture else None
        client_gone = False
        try:
            async for chunk in upstream.content.iter_any():
                if not client_gone:
                    try:
                        await response.write(chunk)
                    except (ConnectionResetError, ClientConnectionResetError) as e:
                        # The user interrupted the agent (Esc, Ctrl-C) or the
                        # client died. Their half of the exchange is over, but
                        # ours is not: keep draining upstream so the assembler
                        # sees the whole reply and the turn is still captured.
                        self._log(f"client disconnected mid-stream: {e}")
                        client_gone = True
                if assembler is not None:
                    try:
                        assembler.feed(chunk)
                    except Exception as e:
                        self._log(f"assembler error: {e}")
                        assembler = None
        except (ConnectionResetError, ClientConnectionResetError) as e:
            self._log(f"upstream stream reset: {e}")
        finally:
            upstream.release()

        if not client_gone:
            try:
                await response.write_eof()
            except (ConnectionResetError, ClientConnectionResetError):
                pass

        if capture and upstream.status < 400:
            try:
                await self._finish_capture(capture)
            except Exception as e:
                self._log(f"capture write failed: {e}")

        return response

    # ── capture ───────────────────────────────────────────────────────────

    def _begin_capture(self, protocol: str, path: str, headers, body: bytes) -> Optional[dict]:
        payload = json.loads(body)
        if not isinstance(payload, dict):
            return None

        agent = identity.resolve_agent(path, headers) or "unknown"
        request_data = protocols.normalize_request(payload, protocol)
        session_id = identity.resolve_session_id(headers, request_data.turns, agent)
        for turn in request_data.turns:
            turn.session_id = session_id

        return {
            "session_id": session_id,
            "protocol": protocol,
            "agent": agent,
            "model": request_data.model,
            "turns": request_data.turns,
            "assembler": protocols.ResponseAssembler(protocol, session_id),
        }

    async def _finish_capture(self, capture: dict) -> None:
        assembler = capture["assembler"]
        assembler.finish()
        session_id = capture["session_id"]
        # Two different kinds of turn, handled differently below: the request
        # body is history the client resent and must be deduplicated, while
        # the response just arrived and is new by definition.
        history_turns = list(capture["turns"])
        response_turns = assembler.turns()

        context = identity.resolve_workflow_context()
        meta = SessionMeta(
            session_id=session_id,
            agent=capture["agent"],
            protocol=capture["protocol"],
            model=capture["model"],
            task_key=context["task_key"],
            project=context["project"],
            repo=context["repo"],
        )

        # Storage is synchronous; keep it off the event loop so a slow disk
        # or a mirror never stalls the next streamed chunk.
        async with self._lock:
            written = await asyncio.to_thread(
                self._write, meta, session_id, history_turns, response_turns
            )
        self._active[session_id] = time.time()
        self._finalized.discard(session_id)
        if written:
            self._log(f"captured {written} turn(s) → {session_id} "
                      f"[{meta.task_key or 'no task'}]")

    def _write(self, meta: SessionMeta, session_id: str,
               history_turns: list, response_turns: list) -> int:
        """Append this exchange, deduplicating only the resent history.

        The response is never reconciled. An assistant can legitimately
        reply with the same words twice -- "done", or an identical tool call
        -- and matching it against the transcript would silently drop the
        second one. Only the history the client echoed back needs matching,
        and that is exactly what reconciliation is for.
        """
        self.store.ensure_session(meta)
        history_turns = self._drop_repeat_system(session_id, history_turns)
        stored = self.store.fingerprints(session_id)
        new_turns = reconcile(stored, history_turns) + response_turns
        return self.store.append_turns(session_id, new_turns) if new_turns else 0

    def _drop_repeat_system(self, session_id: str, turns: list) -> list:
        """Keep one system prompt per session.

        Clients stamp per-request data into the system prompt -- Claude Code
        puts a build id in it -- so its fingerprint changes on every request
        and it would otherwise append a fresh near-identical copy forever.
        The first one is the record; later variants carry no durable
        information the conversation does not already hold.
        """
        if session_id not in self._system_captured:
            # Cheap probe: the system prompt is the head of the stream, so a
            # handful of turns is enough to know whether one is stored. Costs
            # one small query per session, not per request.
            head = self.store.get_turns(session_id, limit=4)
            if any(t.role == "system" for t in head):
                self._system_captured.add(session_id)
            elif any(t.role == "system" for t in turns):
                # This request carries the first one; let it through, and
                # suppress system turns from here on.
                self._system_captured.add(session_id)
                return turns

        if session_id not in self._system_captured:
            return turns
        return [t for t in turns if t.role != "system"]

    # ── idle finalization ─────────────────────────────────────────────────

    async def _idle_loop(self) -> None:
        """Summarize sessions that have gone quiet.

        There is no end-of-session signal in these protocols, so idleness is
        the proxy's proxy for "the user walked away".
        """
        try:
            while True:
                await asyncio.sleep(30)
                cutoff = time.time() - self.idle_seconds
                for session_id, last in list(self._active.items()):
                    if last < cutoff and session_id not in self._finalized:
                        await self._finalize(session_id)
        except asyncio.CancelledError:
            raise

    async def _finalize(self, session_id: str) -> None:
        self._finalized.add(session_id)
        self._active.pop(session_id, None)
        try:
            from workflow.ladder import summarize_session
            await asyncio.to_thread(summarize_session, session_id, self.store, self.config)
        except Exception as e:
            self._log(f"summarize failed for {session_id}: {e}")

    def _log(self, message: str) -> None:
        if self.verbose:
            print(f"[wf-proxy] {message}", flush=True)


def redact_headers(headers) -> dict:
    """Header dict safe to print or persist."""
    return {
        k: ("***" if k.lower() in _SECRET_HEADERS else v)
        for k, v in dict(headers).items()
    }


def serve(config: Optional[dict] = None) -> None:
    CaptureProxy(config).run()
