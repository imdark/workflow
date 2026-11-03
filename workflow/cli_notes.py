"""`wf notes` -- find, read, edit and file notes from a shell.

Every command is a call to NotesGraph's MCP endpoint, the same tools an agent
gets, so a script can do what an agent would: `wf notes find`, pipe the ids
to `wf notes file - --into "Reading list"`, and the notes are grouped under
that note in the sidebar's tree. The sidebar tree is built from links, so a
folder here is a note other notes are linked under.

Which workspace: NOTESGRAPH_URL, NOTESGRAPH_WORKSPACE and NOTESGRAPH_TOKEN
when set (a terminal NotesGraph opens through `wf terminal serve` sets them
to the workspace it was opened from), else deploy.notesgraph in
~/.wf/config.yaml and the token from `wf deploy notes-login`.
"""

import json
import os
import re
import subprocess
import sys
import tempfile
from typing import List, Optional

import typer

from workflow.deploy.notes import NotesReader, _json_objects, _same_title, strip_mcp_stamp
from workflow.deploy.registry.notesgraph import NotesGraphError

notes_app = typer.Typer(help="Find, read, edit and file NotesGraph notes", no_args_is_help=True)

# Shown on every note this CLI writes ("Edited via MCP by wf notes").
AGENT = "wf notes"
# NotesGraph doc ids are nanoids: one word of letters, digits, - and _.
_DOC_ID = re.compile(r"^[A-Za-z0-9_-]{16,}$")


def reader_from_env() -> NotesReader:
    """A reader for the workspace in the environment, else the config's."""
    workspace = os.environ.get("NOTESGRAPH_WORKSPACE")
    if workspace:
        return NotesReader(url=os.environ.get("NOTESGRAPH_URL") or "",
                           workspace=workspace,
                           token=os.environ.get("NOTESGRAPH_TOKEN") or None)
    from workflow.config import load_effective_config
    from workflow.deploy.notes import reader_from_config

    return reader_from_config(load_effective_config())


def _reader() -> NotesReader:
    try:
        return reader_from_env()
    except (NotesGraphError, ValueError) as e:
        _fail(str(e))


def _fail(message: str):
    typer.echo(f"wf notes: {message}", err=True)
    raise typer.Exit(1)


def _result(text: str):
    """A tool's answer as JSON when it is JSON, else the text."""
    values = _json_objects(text)
    return values[0] if len(values) == 1 else (values or text)


def resolve(reader: NotesReader, ref: str) -> str:
    """The doc id for `ref`: a doc id as given, else the note with that title."""
    ref = ref.strip()
    if _DOC_ID.match(ref):
        return ref
    hit = reader.find(ref)
    if hit:
        return hit["docId"]
    _fail(f"no note titled {ref!r}")


def _refs(refs: List[str]) -> List[str]:
    """`refs`, with "-" replaced by the first column of each stdin line, so
    `wf notes find … | wf notes file - --into X` passes the found notes on."""
    out = []
    for ref in refs:
        if ref == "-":
            out += [line.split("\t")[0].strip() for line in sys.stdin if line.strip()]
        else:
            out.append(ref)
    return out


def _print_docs(docs: list, ids: bool, as_json: bool) -> None:
    if as_json:
        typer.echo(json.dumps(docs, indent=2))
        return
    for doc in docs:
        typer.echo(doc["docId"] if ids else f"{doc['docId']}\t{doc.get('title') or 'Untitled'}")


def _hits(text: str) -> list:
    """Documents in a search answer, one per doc, in rank order."""
    seen, docs = set(), []

    def walk(value):
        if isinstance(value, list):
            for v in value:
                walk(v)
        elif isinstance(value, dict):
            if value.get("docId") and value["docId"] not in seen:
                seen.add(value["docId"])
                docs.append(value)
            for key in ("documents", "results", "blocks", "links", "backlinks"):
                if key in value:
                    walk(value[key])

    walk(_json_objects(text))
    return docs


def _body(content: Optional[str]) -> Optional[str]:
    """Text from --content, "-" for stdin, or piped stdin; None when neither
    (an empty pipe counts as neither, so it never blanks a note)."""
    if content == "-":
        return sys.stdin.read()
    if content is None and not sys.stdin.isatty():
        return sys.stdin.read() or None
    return content


@notes_app.command("find")
def notes_find(
    query: str = typer.Argument(..., help='Keywords; filters like tag:x prop:k:v doc:"Title" work too'),
    semantic: bool = typer.Option(False, "--semantic", "-s", help="Match by meaning, not words"),
    ids: bool = typer.Option(False, "--ids", help="Print only doc ids"),
    as_json: bool = typer.Option(False, "--json", help="Print the matches as JSON"),
):
    """Notes matching QUERY: id and title, tab-separated, one per line."""
    reader = _reader()
    try:
        text = reader.call("semantic_search" if semantic else "keyword_search", {"query": query})
    except NotesGraphError as e:
        _fail(str(e))
    _print_docs(_hits(text), ids, as_json)


@notes_app.command("ls")
def notes_ls(
    limit: int = typer.Option(50, "--limit", "-n", help="How many (max 200)"),
    ids: bool = typer.Option(False, "--ids", help="Print only doc ids"),
    as_json: bool = typer.Option(False, "--json", help="Print the notes as JSON"),
):
    """The most recently updated notes."""
    reader = _reader()
    try:
        text = reader.call("list_documents", {"limit": min(max(limit, 1), 200)})
    except NotesGraphError as e:
        _fail(str(e))
    _print_docs(_hits(text), ids, as_json)


@notes_app.command("read")
def notes_read(note: str = typer.Argument(..., help="Doc id or exact title")):
    """A note's markdown."""
    reader = _reader()
    try:
        typer.echo(reader.read(resolve(reader, note)))
    except NotesGraphError as e:
        _fail(str(e))


@notes_app.command("links")
def notes_links(
    note: str = typer.Argument(..., help="Doc id or exact title"),
    back: bool = typer.Option(False, "--back", "-b", help="Notes linking to it instead"),
    ids: bool = typer.Option(False, "--ids", help="Print only doc ids"),
    as_json: bool = typer.Option(False, "--json", help="Print the notes as JSON"),
):
    """Notes NOTE links to (the notes filed under it), or with --back, links to NOTE."""
    reader = _reader()
    try:
        text = reader.call("get_backlinks" if back else "get_links",
                           {"docId": resolve(reader, note)})
    except NotesGraphError as e:
        _fail(str(e))
    _print_docs(_hits(text), ids, as_json)


@notes_app.command("new")
def notes_new(
    title: str = typer.Argument(...),
    content: Optional[str] = typer.Option(None, "--content", "-c",
                                          help='Markdown body; "-" or a pipe reads stdin'),
    into: Optional[str] = typer.Option(None, "--into", "-i", help='Folder note to file it under, e.g. "Work / Reading"'),
):
    """Create a note; prints its id."""
    reader = _reader()
    try:
        doc_id = create(reader, title, _body(content) or "")
        if into:
            file_under(reader, folder(reader, into), [doc_id])
    except NotesGraphError as e:
        _fail(str(e))
    typer.echo(doc_id)


@notes_app.command("edit")
def notes_edit(
    note: str = typer.Argument(..., help="Doc id or exact title"),
    content: Optional[str] = typer.Option(None, "--content", "-c",
                                          help='New markdown body; "-" or a pipe reads stdin'),
    append: bool = typer.Option(False, "--append", "-a", help="Add to the end instead of replacing"),
):
    """Replace (or --append to) a note's body; with no new text, opens $EDITOR on it."""
    reader = _reader()
    try:
        doc_id = resolve(reader, note)
        current = reader.read(doc_id)
        text = _body(content)
        if text is None:
            if not sys.stdin.isatty():
                _fail("no new text: pass --content, pipe it in, or run in a terminal for $EDITOR")
            text = _in_editor(current)
            if text.rstrip() == current.rstrip():
                typer.echo("unchanged", err=True)
                return
        elif append:
            text = f"{current.rstrip()}\n\n{text}" if current.strip() else text
        reader.call("update_document", {"docId": doc_id, "content": text, "agent": AGENT})
    except NotesGraphError as e:
        _fail(str(e))


@notes_app.command("rename")
def notes_rename(note: str = typer.Argument(..., help="Doc id or exact title"),
                 title: str = typer.Argument(..., help="New title")):
    """Retitle a note."""
    reader = _reader()
    try:
        reader.call("update_document_meta", {"docId": resolve(reader, note), "title": title})
    except NotesGraphError as e:
        _fail(str(e))


@notes_app.command("file")
def notes_file(
    notes: List[str] = typer.Argument(..., help='Doc ids or titles; "-" reads ids from stdin'),
    into: str = typer.Option(..., "--into", "-i",
                             help='Folder note, e.g. "Reading list" or "Work / Reading"; made if missing'),
):
    """Group notes under a folder note, as the sidebar tree shows them.

    Adds a link from the folder to each note (a note can sit in several
    folders; nothing is unlinked). A note already in the folder is skipped.
    """
    reader = _reader()
    try:
        children = [resolve(reader, ref) for ref in _refs(notes)]
        parent = folder(reader, into)
        added = file_under(reader, parent, children)
    except NotesGraphError as e:
        _fail(str(e))
    typer.echo(f"filed {added} of {len(children)} under {into}"
               + (f" ({len(children) - added} already there)" if added < len(children) else ""),
               err=True)


@notes_app.command("tools")
def notes_tools():
    """The MCP tools `wf notes call` can run."""
    reader = _reader()
    try:
        tools = reader.tools()
    except NotesGraphError as e:
        _fail(str(e))
    for tool in tools:
        description = (tool.get("description") or "").split(". ")[0]
        typer.echo(f"{tool['name']}\t{description[:100]}")


@notes_app.command("call")
def notes_call(
    tool: str = typer.Argument(..., help="An MCP tool, see `wf notes tools`"),
    args: Optional[List[str]] = typer.Argument(
        None, help="A JSON object, or key=value pairs (a value that parses as JSON is used as JSON)"),
):
    """Run any NotesGraph MCP tool, e.g. `wf notes call get_backlinks docId=abc`."""
    reader = _reader()
    try:
        text = reader.call(tool, parse_args(args or []))
    except NotesGraphError as e:
        _fail(str(e))
    value = _result(text)
    typer.echo(value if isinstance(value, str) else json.dumps(value, indent=2))


def parse_args(args: List[str]) -> dict:
    if len(args) == 1 and args[0].lstrip().startswith("{"):
        try:
            return json.loads(args[0])
        except json.JSONDecodeError as e:
            _fail(f"arguments are not valid JSON: {e}")
    out = {}
    for arg in args:
        key, sep, value = arg.partition("=")
        if not sep:
            _fail(f"expected key=value, got {arg!r}")
        try:
            out[key] = json.loads(value)
        except json.JSONDecodeError:
            out[key] = value
    return out


def create(reader: NotesReader, title: str, content: str) -> str:
    answer = _result(reader.call("create_document",
                                 {"title": title, "content": content, "agent": AGENT}))
    doc_id = answer.get("docId") if isinstance(answer, dict) else None
    if not doc_id:
        raise NotesGraphError(f"create_document gave no docId: {str(answer)[:200]}")
    return doc_id


def linked(reader: NotesReader, doc_id: str) -> list:
    """[{docId, title}] the note links to."""
    return _hits(reader.call("get_links", {"docId": doc_id}))


def folder(reader: NotesReader, path: str) -> str:
    """The note for the last folder on `path` ("A / B"), made where missing.

    The first folder is the note titled so anywhere; each one after it is
    looked for among the notes its parent links to, so "Work / Reading" and
    "Home / Reading" stay apart.
    """
    parts = [p.strip() for p in path.split(" / ") if p.strip()]
    if not parts:
        raise NotesGraphError("a folder needs a title")
    parent = None
    for part in parts:
        if parent is None:
            hit = reader.find(part)
        else:
            hit = next((d for d in linked(reader, parent)
                        if _same_title(d.get("title", ""), part)), None)
        doc_id = hit["docId"] if hit else create(reader, part, "")
        if parent and not hit:
            reader.call("link_document", {"parentDocId": parent, "childDocId": doc_id})
        parent = doc_id
    return parent


def file_under(reader: NotesReader, parent: str, children: List[str]) -> int:
    """Link each child under `parent` unless it already is; returns how many
    were linked. Linking twice would add a second link, hence the check."""
    present = {d["docId"] for d in linked(reader, parent)}
    added = 0
    for child in children:
        if child in present or child == parent:
            continue
        reader.call("link_document", {"parentDocId": parent, "childDocId": child})
        present.add(child)
        added += 1
    return added


def _in_editor(text: str) -> str:
    editor = os.environ.get("VISUAL") or os.environ.get("EDITOR") or "vi"
    with tempfile.NamedTemporaryFile("w+", suffix=".md", delete=False) as f:
        f.write(text)
        path = f.name
    try:
        if subprocess.call([*editor.split(), path]) != 0:
            _fail(f"{editor} exited with an error; the note is unchanged")
        with open(path) as f:
            return strip_mcp_stamp(f.read())
    finally:
        os.unlink(path)
