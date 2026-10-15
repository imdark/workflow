---
name: compile-into-notes
description: Fold a newly imported or clipped source (article, PDF, highlights) into the NotesGraph graph - a summary note, facts appended to the concept and person notes it touches, missing concept notes created and linked.
---

# Compile a source into the notes

Use this when a run is started on an imported or clipped note (a web clip, a
PDF, Readwise highlights, a pasted article) and the ask is to work it into the
user's knowledge rather than leave it as one more raw page. The model is
OpenKB's compile step: a source is read once and the notes it touches are
updated, so later questions are answered from the notes, not by re-reading
sources.

You have the NotesGraph tools (`mcp__notesgraph__*`). Everything below is done
with them.

## 1. Read the source

- `read_document` the note the run was started on. Note its title and docId;
  that is the source you will cite.
- If it is long, read it all anyway. A compile from the first screen is wrong
  about the rest.
- Write down for yourself: the 3 to 8 claims worth keeping, each concept
  (topic, tool, method, place) it is about, and each person or organisation it
  names.

## 2. Write the summary note

Create one note titled `Summary: <source title>` (shorten a long title, keep it
recognisable) with:

- One paragraph: what the source is and why it matters to the user, judged
  from what else is in their notes.
- The claims as a bulleted list. Each bullet is one fact in plain words, in
  the user's voice, not the source's. End each bullet with the citation
  `(source: <source title>)`.
- A "Concepts" line listing the concept and person notes you touched or made,
  so the summary links out to them.

Link it under the source with `link_document` (parent: the source, child: the
summary). The source stays untouched.

## 3. Append to the notes that already exist

For each concept and person:

- Look for an existing note: `keyword_search` the name, then `semantic_search`
  if the first finds nothing. Open the candidates with `read_document` and keep
  the one that is about the same thing. Do not merge two things that share a
  name.
- Append a short block at the end of that note, under a heading
  `From <source title> (<date>)`, with the facts this source adds. One line
  per fact, dated, with the citation. Use `update_document` with the full
  content; change nothing above your block.
- If the new fact contradicts something already in the note, do not overwrite
  it. Add the new fact and a line starting `Conflict:` that names both
  versions, so the user decides.
- Link the concept note under the summary note with `link_document`, so the
  summary's "Concepts" line resolves to real links.

## 4. Create the concept notes that are missing

For a concept or person with no note of its own and at least two facts worth
keeping, create one titled with the subject's name. First paragraph: what it
is, in one or two sentences. Then the facts, dated and cited as above. Link it
under the summary note. A subject with a single passing mention gets no note;
it stays as a line in the summary.

## 5. Report

Finish by telling the user, in a few lines: the summary note you made, which
notes you appended to, which you created, and any `Conflict:` lines they
should look at. Nothing is deleted or rewritten by this skill; it only adds.

## Rules

- Cite every fact with its source title. An uncited fact in a concept note
  cannot be checked later.
- Date every fact you add (`as of <YYYY-MM-DD>`), the date of the source if it
  has one, else today's.
- Keep the user's existing wording and structure. Your block goes at the end.
- When the "Agent rules" note says where a kind of note goes or how it is
  named, that wins over the titles suggested here.
