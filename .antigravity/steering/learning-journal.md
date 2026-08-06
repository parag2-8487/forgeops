---
inclusion: always
---

# Keep the learning journal current

`docs/LEARNING-JOURNAL.md` teaches a newcomer what this project is, why it is built this way,
and where it stands. It only works if it is updated as the work happens.

## After every completed feature, task leaf, decision or defect fix

Append to `docs/LEARNING-JOURNAL.md`: **what changed**, **which chapter it belongs to**, **why
this approach**, **what was rejected**, and **any cost accepted**. A change with no cost is
rare; if you cannot name one, look harder before writing "none".

Update the header table's **date** and **leaf count** in the same edit.

## How to edit it

- **Append and revise in place.** Never rewrite the file wholesale. Never delete a chapter,
  and never delete or truncate any `.md` file (see `agent-autonomy.md`).
- **Every new numbered decision gets a paragraph in chapter 8** — D-59 onward; the log reaches
  D-58 today. Say what it decided, what it rejected, and what it cost.
- **Every defect found in pre-existing code gets a paragraph in chapter 9**, including the
  pattern it belongs to. If it is a new pattern, add the pattern and name it.
- **Regenerate the comprehension artifact whenever a group of task leaves completes.** The
  command is in `docs/development.md` under "Comprehension artifact"; the output lives in
  `docs/understand-anything/`. Note the regeneration date in that directory's `README.md`.

## It explains; it is never an authority

`.antigravity/specs/*/design.md`, `.antigravity/specs/*/tasks.md` and `PROGRESS.md` are the sources of truth.
The journal must not contradict them. If it would, **the journal is wrong** — fix the journal,
not the record.

## Keep it honest

Record what was verified and what was not. Never write "verified", "proven" or "gated" where
the underlying source says otherwise — including where a source says a check exists but did not
run. An unverified claim in a teaching document is worse than a gap, because a reader cannot
tell the difference.
