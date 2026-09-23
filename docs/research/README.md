# Research log

The measurements behind skill-plus-plus's defaults, in the order they were made. Each
entry records what was tried, on what, and what was kept, including what did not
work, so that it is not tried again.

- [benchmarks.md](benchmarks.md): cutting sessions into tasks (the boundary
  judge), recognising repeats (matching), naming, and the inputs each model is
  shown.
- [episode-filter.md](episode-filter.md): ranking candidates by how repeatable
  they look (`sift`), and the first design of the draft stage.

**The sessions cited here**, by 8-character tags such as `241955c7`, are a
private set recorded on internal work. They are not in the repo. The public
sessions in `tests/fixtures/sessions/` take their place for anyone reproducing a
result, and the numbers on them will differ.

## Where things stand

- **Cutting sessions into tasks.** `gemma3n:e4b` is asked once per prompt gap
  whether a new job started. 19 of 21 private sessions come out right; both
  misses cut one task in two at a review request. Only 2 of the 45 gaps were
  real boundaries, so a judge answering "no" everywhere also scores 19 of 21:
  the set cannot tell a better judge from a lucky one.
- **The default judge is `gemma4:e4b` since 2026-09-22**, with thinking off;
  the numbers above were measured on `gemma3n:e4b`, the previous default.
- **On the public sessions** (22, `tests/fixtures/sessions/`, gemma4): 19 of 22
  right, 6 of 9 real task switches caught, 0 false cuts over 77 prompts. The
  three misses are switches between code tasks. Merging: 0 wrong merges; pairs
  with identical prompts or the same goal merge (3/3 for code, 3/3 for
  knowledge work); on different subjects, 12/12 for knowledge work but 1/23 for
  code. Unlike the private set, these have 9 real switches, so a judge that
  always says "no" would score 15 of 22.
- **The reply tail, measured and not adopted.** `gemma4:e4b`, shown the last
  400 characters of the agent's reply before the gap, scored 20 of 21 with no
  false cuts. With thinking on, it caught both real boundaries but cut three
  single tasks, at about 15 seconds a gap instead of 1.4.
- **Recognising repeats.** Runs are compared by their conversation, at a cosine
  floor of 0.85: 14 of 61 same-procedure pairs merge, with no wrong merge. The
  floor is set by a real pair, writing an article from documents against
  building a deck from documents, which score 0.849.
- **Drafting** reads a candidate's first run only. The note on Draft Skill is
  where the developer adds what the later runs showed.
