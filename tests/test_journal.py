"""A created task is added under its project's heading in the day's journal note."""
from workflow.journal import insert_task, task_line

DAY = """## Work

- [ ] robots realtime should be a project

### todos:
|Status|Deadline|Scheduled|
|---|---|---|

## notes graph

- [x] BLOCKED a blocked task
I (the agent) could not do it.

- [ ]

### todos:
|Status|Deadline|Scheduled|
|---|---|---|

## workflow

- [ ] [-] an earlier task @claude STARTED: [2026-11-15 Sun 22:53]

### todos:
|Status|Deadline|Scheduled|
|---|---|---|

## Inbox

- [ ]
"""


def test_the_line_goes_after_the_sections_last_item_before_its_subheading():
    out = insert_task(DAY, "workflow", "- [ ] PERSONAL-9 New")
    assert "STARTED: [2026-11-15 Sun 22:53]\n- [ ] PERSONAL-9 New\n\n### todos:" in out
    assert out.count("PERSONAL-9") == 1
    assert out.replace("- [ ] PERSONAL-9 New\n", "") == DAY


def test_it_fills_the_sections_empty_item_and_matches_the_heading_loosely():
    out = insert_task(DAY, "notes-graph", "- [ ] PERSONAL-9 New")
    assert "I (the agent) could not do it.\n\n- [ ] PERSONAL-9 New\n\n### todos:" in out
    assert out.count("- [ ]\n") == DAY.count("- [ ]\n") - 1 == 1


def test_a_missing_heading_is_added_before_the_inbox():
    out = insert_task(DAY, "robots", "- [ ] PEN-3 New")
    assert "|---|---|---|\n\n## robots\n\n- [ ] PEN-3 New\n\n## Inbox" in out


def test_an_empty_note_gets_the_heading_and_the_line():
    assert insert_task(None, "workflow", "- [ ] PERSONAL-9 New") == "## workflow\n\n- [ ] PERSONAL-9 New\n"
    assert insert_task("## workflow\n", "workflow", "- [ ] X-1 a") == "## workflow\n\n- [ ] X-1 a\n"


def test_the_line_links_the_task_when_it_has_a_url():
    assert task_line("PEN-3", " Fix \n it ", "https://linear.app/x/PEN-3") == \
        "- [ ] [PEN-3](https://linear.app/x/PEN-3) Fix it"
    assert task_line("PERSONAL-9", "Fix it") == "- [ ] PERSONAL-9 Fix it"
