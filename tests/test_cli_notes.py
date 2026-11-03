"""`wf notes` against a fake NotesGraph MCP endpoint (a real local HTTP
server), reached through the NOTESGRAPH_* variables a sidebar terminal sets."""

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
from typer.testing import CliRunner

from workflow.cli_notes import notes_app
from workflow.deploy.terminal_serve import Device, NotesGraphMcp

runner = CliRunner()


class FakeGraph:
    """Notes and the links between them, behind the MCP tools wf notes uses."""

    def __init__(self, notes):
        self.notes = {k: dict(v) for k, v in notes.items()}  # id -> title, markdown
        self.links = {}                                       # id -> [child ids]
        self.calls = []
        self.auth = []
        graph = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                graph.auth.append((self.path, self.headers.get("Authorization")))
                if body["method"] == "tools/list":
                    result = {"tools": [{"name": "keyword_search",
                                         "description": "Fuzzy search. More."}]}
                else:
                    name, args = body["params"]["name"], body["params"]["arguments"]
                    graph.calls.append((name, args))
                    result = graph.tool(name, args)
                raw = json.dumps({"jsonrpc": "2.0", "id": body["id"], "result": result}).encode()
                self.send_response(200)
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_port}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def tool(self, name, args):
        def text(value):
            return {"content": [{"type": "text",
                                 "text": value if isinstance(value, str) else json.dumps(value)}]}

        def titled(ids):
            return [{"docId": i, "title": self.notes[i]["title"]} for i in ids]

        if name == "keyword_search":
            return text("\n".join(json.dumps(h) for h in titled(
                i for i, n in self.notes.items()
                if args["query"].casefold() in (n["title"] + n["markdown"]).casefold())))
        if name == "list_documents":
            return text({"documents": titled(list(self.notes)[: args["limit"]]), "nextCursor": None})
        if name == "read_document":
            return text(self.notes[args["docId"]]["markdown"])
        if name == "create_document":
            doc_id = f"new-{len(self.notes)}"
            self.notes[doc_id] = {"title": args["title"], "markdown": args["content"]}
            return text({"success": True, "docId": doc_id})
        if name == "update_document":
            self.notes[args["docId"]]["markdown"] = args["content"]
            return text({"success": True})
        if name == "update_document_meta":
            self.notes[args["docId"]]["title"] = args["title"]
            return text({"success": True})
        if name == "link_document":
            self.links.setdefault(args["parentDocId"], []).append(args["childDocId"])
            return text({"success": True})
        if name == "get_links":
            return text({"links": titled(self.links.get(args["docId"], []))})
        if name == "get_backlinks":
            return text({"backlinks": titled(p for p, kids in self.links.items()
                                             if args["docId"] in kids)})
        return {"content": [{"type": "text", "text": f"no tool {name}"}], "isError": True}

    def run(self, *args, stdin=None):
        return runner.invoke(notes_app, list(args), input=stdin, env={
            "NOTESGRAPH_URL": self.url, "NOTESGRAPH_WORKSPACE": "ws-1",
            "NOTESGRAPH_TOKEN": "pat"})

    def __enter__(self):
        return self

    def __exit__(self, *a):
        self.server.shutdown()


def graph():
    return FakeGraph({
        "id-paper-aaaaaaaaaaaaa": {"title": "Diffusion policy paper", "markdown": "robot learning"},
        "id-blog-bbbbbbbbbbbbbb": {"title": "ACT blog post", "markdown": "robot teleop"},
        "id-shop-cccccccccccccc": {"title": "Shopping", "markdown": "milk"},
    })


def test_find_lists_ids_and_titles_on_the_workspace_from_the_env():
    with graph() as g:
        out = g.run("find", "robot")
    assert out.exit_code == 0, out.output
    assert out.output.splitlines() == ["id-paper-aaaaaaaaaaaaa\tDiffusion policy paper",
                                       "id-blog-bbbbbbbbbbbbbb\tACT blog post"]
    assert g.auth[0] == ("/api/workspaces/ws-1/mcp", "Bearer pat")


def test_found_notes_pipe_into_a_new_nested_folder_and_refiling_adds_no_second_link():
    with graph() as g:
        found = g.run("find", "robot").output
        out = g.run("file", "-", "--into", "Research / Robots", stdin=found)
        assert out.exit_code == 0, out.output
        research = next(i for i, n in g.notes.items() if n["title"] == "Research")
        robots = next(i for i, n in g.notes.items() if n["title"] == "Robots")
        assert g.links[research] == [robots]
        assert g.links[robots] == ["id-paper-aaaaaaaaaaaaa", "id-blog-bbbbbbbbbbbbbb"]

        again = g.run("file", "ACT blog post", "--into", "Research / Robots")
        assert again.exit_code == 0, again.output
        assert g.links[robots] == ["id-paper-aaaaaaaaaaaaa", "id-blog-bbbbbbbbbbbbbb"]
        assert g.links[research] == [robots]
        assert "already there" in again.output

        assert g.run("links", "Robots", "--ids").output.split() == [
            "id-paper-aaaaaaaaaaaaa", "id-blog-bbbbbbbbbbbbbb"]
        assert g.run("links", "ACT blog post", "--back").output.strip().endswith("Robots")


def test_read_new_edit_append_and_rename():
    with graph() as g:
        assert g.run("read", "Shopping").output.strip() == "milk"
        doc_id = g.run("new", "Todo", "--content", "- [ ] call", "--into", "Home").output.strip()
        assert g.notes[doc_id]["markdown"] == "- [ ] call"
        home = next(i for i, n in g.notes.items() if n["title"] == "Home")
        assert g.links[home] == [doc_id]

        assert g.run("edit", "Shopping", "--append", stdin="eggs").exit_code == 0
        assert g.notes["id-shop-cccccccccccccc"]["markdown"] == "milk\n\neggs"
        assert g.run("rename", "Shopping", "Groceries").exit_code == 0
        assert g.notes["id-shop-cccccccccccccc"]["title"] == "Groceries"
        writes = [a for n, a in g.calls if n == "update_document"]
        assert writes[0]["agent"] == "wf notes"


def test_edit_with_nothing_piped_and_no_terminal_leaves_the_note_alone():
    with graph() as g:
        out = g.run("edit", "Shopping", stdin="")
        assert out.exit_code == 1
        assert g.notes["id-shop-cccccccccccccc"]["markdown"] == "milk"


def test_an_unknown_title_is_an_error_not_a_guess():
    with graph() as g:
        out = g.run("read", "Nope")
    assert out.exit_code == 1
    assert "no note titled 'Nope'" in out.output


def test_call_runs_any_tool_with_key_value_or_json_arguments():
    with graph() as g:
        out = g.run("call", "list_documents", "limit=1")
        assert out.exit_code == 0, out.output
        assert json.loads(out.output)["documents"][0]["title"] == "Diffusion policy paper"
        g.run("call", "update_document_meta", '{"docId": "id-shop-cccccccccccccc", "title": "X"}')
        assert g.notes["id-shop-cccccccccccccc"]["title"] == "X"
        assert g.run("tools").output.strip() == "keyword_search\tFuzzy search"


@pytest.mark.parametrize("harness", ["shell", "claude"])
def test_a_terminal_session_gets_the_workspace_for_wf_notes(harness, tmp_path, monkeypatch):
    monkeypatch.setattr("workflow.session.apply_claude_env", lambda env, project=None: None)

    async def emit(*a):
        pass

    device = Device("ws-1", "dev", emit, allow_shell=True, sessions_dir=tmp_path,
                    mcp=NotesGraphMcp("https://ng.example", "ws-1", "pat"))
    _, env, _ = device._launch({"harness": harness}, "s1")
    assert env["NOTESGRAPH_WORKSPACE"] == "ws-1"
    assert env["NOTESGRAPH_URL"] == "https://ng.example"
    assert env["NOTESGRAPH_TOKEN"] == "pat"
