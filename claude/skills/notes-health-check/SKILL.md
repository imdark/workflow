---
name: notes-health-check
description: Lint the NotesGraph workspace - orphan notes, broken or dangling links, near-duplicate notes, stale dated facts in Agent memory, contradictions between notes on the same subject - and write a report note with the fixes as to-dos. Changes nothing itself.
---

# Notes health check

Use this for a scheduled or on-demand run whose job is to tell the user what
is wrong with their notes, the way a linter does for code: find the problems,
list them where they can be acted on, and apply no fix without a person
saying so. The output is one report note; the fixes are checkboxes in it,
which the user ticks or starts a run on.

You have the NotesGraph tools (`mcp__notesgraph__*`). Everything below is done
with them.

## Scope

Walk the workspace with `list_documents` (page through all of it). Skip:

- notes under `ai / conversations` folders and anything titled with a
  conversation id: they are transcripts, not knowledge;
- daily notes titled with a bare date (`2026-10-06`) unless a check below says
  otherwise;
- earlier `Notes health check` reports.

Work from titles and the graph first (`get_links`, `get_backlinks`); open a
note with `read_document` only when a check needs its text. Reading every
note is slow and rarely needed.

## Checks

1. **Orphans.** A note with no backlinks and no outgoing links, older than a
   week. Suggest the note it most likely belongs under (by `keyword_search` on
   its title words).
2. **Dangling links.** A `[[wikilink]]` or link whose target does not exist.
   Say which note has it and what it points at; suggest the nearest existing
   title if there is one.
3. **Near-duplicates.** Two notes whose titles differ only by case, spacing,
   punctuation, a plural, or a prefix like `Summary:` or `Re:`; and pairs
   `semantic_search` ranks very close whose first paragraphs say the same
   thing. Propose which to keep and offer the merge as a to-do. Do not merge.
4. **Stale facts.** In the `Agent memory` note and the notes linked under it,
   a fact dated `as of <date>` more than six months ago, or that names an age
   or a plan with a date now past. List each with the note and the line.
5. **Contradictions.** Two notes about the same person or subject that state
   incompatible facts (two different ages, employers, dates for the same
   event), and any line already starting `Conflict:` that a compile left
   behind. Quote both lines with their notes.
6. **Empty and stub notes.** A note with a title and no body, or only a
   heading. Suggest deleting it or filling it from a search.

Cap each list at the 20 worst cases and say how many more there are. A
report nobody can read through fixes nothing.

## The report

Create one note titled `Notes health check <YYYY-MM-DD>`. Structure:

- First line: one sentence with the counts (`N orphans, N dangling links, …`)
  and whether that is better or worse than the previous report, if there is
  one (`keyword_search` "Notes health check").
- One section per check, in the order above. Each finding is a task line
  (`- [ ] …`) that names the note (as a link), says what is wrong, and ends
  with the suggested fix, so a run started on that line can do just that. A
  check with nothing to report is one line saying so.
- Last section, `Skipped`: what you did not check and why (a folder too
  large, a tool that failed), so a clean section is not mistaken for a clean
  workspace.

Link the report under the previous report (or under `Agent memory` for the
first one) with `link_document`, so the series is one chain.

## Rules

- Apply no fix. Not a merge, not a link, not a deletion. The to-dos are the
  fixes; the user decides.
- Prefer a short true report to a long guessed one. A pair you are not sure
  about goes under a `Maybe` sub-heading, not in the main list.
- Finish by telling the user where the report is and its first line.
- When the "Agent rules" note names folders or notes to leave alone, or a
  different place for reports, that wins over what is written here.
