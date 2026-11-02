# SKILL.md format spec — authoring + #416 lint, single source

Every agent-authored skill is one `SKILL.md` (plus any files it references) and must satisfy the
checks below. REF authors against this spec; the promoter's deterministic linter enforces it. Both
read this one file — keep them aligned.

## Required frontmatter

The dependency-free frontmatter reader supports flat string fields: existing single-line plain/quoted
values, plus literal (`|`) and folded (`>`) block scalars with indentation and chomping indicators.
This scalar support does not constitute general YAML collection, tag or anchor parsing.

YAML frontmatter that parses, carrying at least:

- `name` — short identifier for the skill.
- `description` — the trigger; see "Description is the trigger" below. This is what the host matches
  recall on. Write a concrete usage condition or quoted user phrase; a bare topic label is rejected.

- `category` — one lowercase word or hyphenated phrase naming the class of work the skill serves
  (`testing`, `git-workflow`, `deployment`). An open set: **pick one already visible in the injected
  index**, and mint a new one only when nothing existing fits — the index is the candidate list, and
  a library of near-synonym categories groups no better than none. It is what the session-start index
  groups by, so a skill without one lands in `general`.

  Enforcement is deliberately asymmetric with the description gate: an illegal value (anything but a
  single safe segment of letters, digits, `.`, `_`, `-`) is **rejected**, but a *missing* one is
  **allowed through** and noted in the run account. A missing category only files the skill badly;
  the curator backfills what lands without one.

## Description is the trigger — full text and display preview

`description` is the full routing signal, stored unchanged in SKILL.md and used in the authoring
comparison index. Its create/update limit is `SKILL_DESC_MAX_CHARS` (default 1024 characters).
The session-start index displays a separate, derived prefix preview, limited by
`INDEX_DESC_MAX_CHARS` (default 60). This is a **display only** budget: a longer full description
is accepted when it fits its own limit. There is no separate summary field to maintain.
Truncated previews end in `...`; whitespace is flattened. They do not rewrite the stored description
and are not semantic summaries. A legacy over-limit or cue-less description remains repairable
through `patch`, preserving the existing exemption on that action.

Prefer **one sentence, trigger first**, ending in a period or `。`. This is writing advice, not a
sentence-count gate. Name the concrete objects and the words a user would actually type — the
nouns, verbs, tools and file types. Keep reusable instructions and detailed procedures in the body.
Putting important cues early makes the short preview more useful, but the preview length never
restricts the full description's authoring limit.

English, Chinese, and mixed descriptions are supported. Examples:

    Use when a fetch fails with 403/429 or a bot wall.
    Fires on 'audit this repo'.
    当需要检查日期格式和跨时区边界时使用。
    用于验证分页接口的重试与超时处理。
    用户提出“检查日期边界”则使用。

Chinese cue forms include `当…时` / `在…时` / `需要…时`, `遇到…`, `用于…`, `适用于…`,
and `用来…`, with common traditional variants. Chinese quote pairs `“…”`, `‘…’`, `「…」`,
and `『…』` can contain literal user phrases, alongside existing ASCII quotes and backticks.
Bare labels such as `Manages project operations` or `日期格式工具` still fail the cue check.

**Enforced (a floor, not the standard):** MCP and the promoter share the full-description limit
and cue heuristic for `create`/`update`. Passing this syntax check does not prove relevance or
recall quality, and it does not recognize every natural-language expression.

## Structure (#416)

- Frontmatter parses as YAML; `name` and `description` are present and non-empty.
- Every file referenced by a relative path in the body exists.
- Every referenced `.py` file parses (no syntax error).
- No broken symlinks.

## Subfiles (folder-skill)

A skill is a folder: `SKILL.md` plus optional subfiles, carried in the same intent (`files`:
relative path → content, `create`/`update` only). Four whitelisted top-level directories, each
with a distinct meaning — put content where it belongs:

- `references/` — session-specific detail and condensed knowledge banks (quoted research, API-doc
  excerpts, domain notes). Concise and task-focused, not a mirror of upstream docs.
- `templates/` — starter files meant to be copied and modified (boilerplate, scaffolding, a
  known-good example).
- `scripts/` — re-runnable actions the skill invokes directly (verification scripts, fixture
  generators, probes) instead of retyping them each run.
- `assets/` — static support files.

Path rules (deny-by-default, violations reject the intent):

- Relative only, at least two `/`-separated segments, the first from the whitelist above.
- Every segment matches `[A-Za-z0-9][A-Za-z0-9._-]*` — no `..`, no dotfiles, no absolute paths,
  no empty segments, no backslashes.

Pointer rule: every subfile carried in the intent must be referenced by its relative path
somewhere in the `SKILL.md` body — an unpointed subfile is invisible to future readers and is
rejected. Conversely, a whitelisted-directory path referenced in the body must be carried in the
same intent or already live in the skill folder.

`references/evidence-*.md` files are promoter-materialized provenance (the ledger points at
them); they are never authored in an intent and are exempt from the pointer rule. Intents can
neither carry them in `files` nor target them with `remove_file`.

A subfile that is no longer needed is dropped with the `remove_file` action (one relative path
per intent, same path rules). The live `SKILL.md` must no longer reference the path — patch the
pointer out first; both intents can ride the same run, they land in order.

## Content completeness

- No `TODO`, no placeholder tokens (`FIXME`, `XXX`, `<...>`), no empty sections.

## Altitude — the body is a rule, not a transcript

A SKILL.md exists to hand the next session a *durable rule*, stated at the altitude of the class of
work — not to replay the episode that produced it. So:

- **Open with the rule itself.** The first content after the frontmatter is the reusable directive in
  one or two lines. A reader who stops there already has the skill.
- **A full explainer is a smell.** A body carrying the whole Pattern / Example / When-to-use /
  Anti-patterns quartet inline is documentation, not a rule — hoist the bulk into `references/` and
  leave a one-line pointer. The SKILL.md stays the rule; `references/` holds the backing detail.
- **Hard cap (enforced):** the body (frontmatter excluded) must be at most `SKILL_BODY_MAX_LINES`
  non-blank lines — the promoter rejects a `create`/`update` over the cap. The cap is a crude proxy
  for altitude; the real judgment ("is this a rule or a retelling?") is the author's. `patch` is
  exempt, so an existing over-long skill can still be amended and trimmed.

## Global is stricter (repo-agnostic)

A `global`-level skill loads in every project, so its blast radius is every project. It must not
embed repo-local identifiers — absolute paths, the current repo name, or repo-specific ids. Such
content downgrades the skill to `project`, or is rejected.
