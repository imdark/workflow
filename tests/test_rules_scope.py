"""Scoped "Agent rules" sections: the shared spec's test cases (NotesGraph's
TypeScript implementation passes the same ones)."""

from workflow.rules_scope import filter_rules, has_kept_scope, normalize, parse_scope

NOTE = """\
intro line
## Writing
* w1
## For project: notes-graph
* p1
### Details
* p1-detail
## For tag: mobile, #android
* m1
### For agent: Researcher
* m1-researcher
## For agent: Researcher
* r1
## Learned
* l1"""

BASE = ["intro line", "## Writing", "* w1"]
LEARNED = ["## Learned", "* l1"]


def lines(**ctx):
    text, scoped = filter_rules(NOTE, **ctx)
    return text.split("\n"), scoped


def test_no_context_keeps_only_the_unscoped_parts():
    out, scoped = lines()
    assert out == BASE + LEARNED
    assert scoped is False


def test_a_project_matches_by_normalized_name():
    out, scoped = lines(projects=["Notes Graph"])
    assert out == BASE + ["## For project: notes-graph", "* p1", "### Details", "* p1-detail"] + LEARNED
    assert scoped is True


def test_a_tag_matches_any_value_but_not_a_nested_scope_it_does_not_match():
    out, scoped = lines(tags=["Android"])
    assert out == BASE + ["## For tag: mobile, #android", "* m1"] + LEARNED
    assert scoped is True


def test_a_nested_scope_applies_when_it_and_its_parent_match():
    out, _ = lines(tags=["mobile"], agents=["researcher"])
    assert out == BASE + ["## For tag: mobile, #android", "* m1",
                          "### For agent: Researcher", "* m1-researcher",
                          "## For agent: Researcher", "* r1"] + LEARNED


def test_a_nested_scope_without_its_parent_is_dropped():
    out, _ = lines(agents=["Researcher"])
    assert out == BASE + ["## For agent: Researcher", "* r1"] + LEARNED


def test_a_scope_heading_in_a_code_fence_is_plain_text():
    note = "intro\n```\n## For tag: mobile\n* in fence\n```\n## For tag: mobile\n* m1\nafter"
    text, scoped = filter_rules(note)
    assert text == "intro\n```\n## For tag: mobile\n* in fence\n```"
    assert scoped is False
    text, _ = filter_rules(note.replace("```", "~~~"))
    assert "* in fence" in text and "* m1" not in text


def test_values_are_trimmed_and_empties_dropped():
    assert parse_scope("For tags:  mobile ,  ") == ("tag", ["mobile"])
    assert parse_scope("for PROJECTS: #a, b") == ("project", ["a", "b"])
    assert parse_scope("For agents :Researcher") == ("agent", ["Researcher"])
    assert parse_scope("Formatting") is None
    assert parse_scope("For example: x") is None
    _, values = parse_scope("For tags:  mobile ,  ")
    assert values == ["mobile"]


def test_names_normalize_alike():
    assert normalize("Notes Graph") == normalize("notes-graph") == normalize("notes_graph") == "notes-graph"
    assert normalize("#Mobile") == "mobile"
    assert normalize("a -_ b") == "a-b"


def test_blank_lines_left_behind_collapse_to_two():
    note = "a\n\n## For tag: x\n\n* x\n\n\n## B\n\n\n\n\nb\n\n"
    text, _ = filter_rules(note)
    assert text == "a\n\n## B\n\n\nb"


def test_a_note_of_only_unmatched_scopes_is_empty():
    assert filter_rules("## For tag: mobile\n* m1\n") == ("", False)
    assert filter_rules("") == ("", False)


def test_a_closing_heading_trails_hashes_and_ranks_by_level():
    note = "# Rules\n## For tag: x ##\n* x\n# Other\n* o"
    assert filter_rules(note)[0] == "# Rules\n# Other\n* o"
    assert filter_rules(note, tags=["x"])[0] == note


def test_kept_scope_is_seen_in_the_filtered_text():
    text, scoped = filter_rules(NOTE, projects=["notes-graph"])
    assert has_kept_scope(text) is scoped is True
    text, scoped = filter_rules(NOTE)
    assert has_kept_scope(text) is scoped is False
    assert has_kept_scope("```\n## For tag: x\n```") is False
