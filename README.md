<h1 align="center">KitHarness</h1>
<p align="center"><strong>Durable skill changes, task-scoped recall, and a measured three-arm study</strong></p>

<p align="center">
  <img src="https://img.shields.io/badge/python-3.11%2B-blue.svg" alt="python" /> <img src="https://img.shields.io/badge/platform-Linux%20%7C%20macOS-lightgrey.svg" alt="platform" /> <img src="https://img.shields.io/badge/license-MIT-yellow.svg" alt="license MIT" />
</p>

**KitHarness** runs on Claude Code. It **learns** skills from real sessions, **merges** same-scenario ones instead of stacking near-duplicates, **updates** them in use, and **prunes** skills it wrote itself. Reflection, curation, a deterministic promoter, and lifecycle retirement stay in the loop.

The license is [MIT](LICENSE). Project notes are in [项目说明](docs/project.md). A local demo that does not call a model is in [演示](docs/demo.md).

| | |
|---|---|
| **Learns from real work** | Each episode is distilled into a skill from the session you were already having — no separate data-collection or replay loop. It fires on its own once a session has done enough work; `/learn` distills on demand when you want a lesson kept now. |
| **Groups, doesn't just pile up** | A new episode doesn't always add a skill — the reflector compares it against what's there and folds same-scenario skills into one, so the layer consolidates by category instead of accreting near-duplicates. A fold records which skill absorbed which, so a merge is never mistaken for a death. |
| **Offers skills for the current task** | Each submitted prompt selects related agent-created skills with explainable lexical ranking and an injection budget. A grouped startup catalog remains available as a compatibility mode. The host's native recall stays untouched. |
| **Kept by use; measured apart** | A skill still survives by later adherence, not by a held-out score on the live path. Synthetic recall and one live three-arm study are recorded separately. That study did not show an accuracy gain. |
| **Only its own skills** | Touches only the skills it generated through this plugin — everything else, whether you wrote it or installed it, is left completely alone. |
| **Evidence kept for later** | Every create/update logs its scenario and decision to a per-skill ledger — the raw material to build a benchmark from real usage if you ever want one. |

## What KitHarness adds

Progress and the acceptance record are in the [iteration plan](docs/PLAN.md). The three changes below are the ones with checks in this repo. They are not a claim that learning raised task accuracy.

| | Change | What was measured |
|---|---|---|
| **Durable edits** | A model delete must name a live merge target. A failed update in the same batch keeps the source skill. Landed edits have an operation id, a full-directory snapshot, and a rollback. | Recovery and rollback behavior is covered by the non-live suite. Commands: [reliable skill evolution](docs/reliable-skill-evolution.md). |
| **Task recall** | The current prompt selects agent-created skills with explainable lexical ranking and a byte budget. Chinese trigger text is accepted, and the full description is no longer cut to the 60-character preview. | On 18 synthetic queries and 20 skills, task ranking recalled 15/16 relevant skills (MRR 0.938) at a mean 170.7 injected bytes. The full catalog recalled 16/16 at 2092 bytes. This is not a live task-success result. See [task recall](docs/task-recall.md). |
| **A recorded study** | The same 6 tasks and 24 cases can be prepared, scored, and rechecked without executing candidate code. One live run compared no learning, the baseline at `f2de058`, and KitHarness at `b232f74`. | All three arms were 6/6 tasks and 24/24 cases with `deepseek-flash`, one repeat. Mean plugin injection was 582 bytes on the baseline and 506 bytes here. That does not show an accuracy gain or a general token saving. See [live study](docs/live-study-results.md). |

## Install

**Requires Python 3.11+ as the `python3` on your PATH** — KitHarness runs entirely as Python
(zero third-party dependencies); its hooks and MCP server won't fire without it. The hooks resolve
bare `python3`, so an older interpreter earlier on your PATH (Xcode ships 3.9.6 at
`/usr/bin/python3`) turns every hook off for the session; KitHarness says so on stderr.

Type these in the Claude Code input box. The plugin id and the marketplace id are both `kitharness`.

```
/plugin marketplace add /absolute/path/to/KitHarness
/plugin install kitharness@kitharness
```

Then run `/reload-plugins` (or restart Claude Code).

Zero config. It now watches your sessions and lands learned skills into `.claude/skills/` in the
background. Cadence and lifecycle thresholds are tunable — see [Configuration](#configuration).

Nothing to invoke, but one entry point exists when you want it: **`/learn`** distills the session
you're in right now — say it after working something out and the lesson goes through the same
proposal-and-validation chain the background pass uses.

**MCP server naming.** `.claude-plugin/plugin.json` registers the server as `stage_skill`, but agent
definitions reference the fully-qualified name `mcp__plugin_kitharness_stage_skill__stage_skill`.
This translation is automatic: the plugin runtime constructs the qualified name from the plugin
id (`kitharness`) and the server key. Outside the plugin context (e.g., registering the server
by hand), the tool would be available as `mcp__stage_skill__stage_skill` — but the agent allowlists
still reference the plugin-namespaced form. Always install as a plugin to match both names. The
server is declared in the manifest, not in a root `.mcp.json`, because the plugin root is the repo
root: a `.mcp.json` there would also be this checkout's project config, which outranks the plugin's
same-command server and leaves the reflector without its tool in sessions inside the repo.


### Update

Update and uninstall are shown in their terminal form (`claude plugin …`); inside Claude Code the same
operations live in the `/plugin` menu.

Update from a terminal — refresh the catalog by marketplace name, then update the plugin by its
**full `plugin@marketplace` id**, then restart:

```
claude plugin marketplace update kitharness
claude plugin update kitharness@kitharness
```

Then **restart Claude Code** to apply — a version bump is a fresh cached copy, not a hot reload.

The refresh is first on purpose: without it, `update` checks a stale local catalog and may report
`already at the latest version` when a newer release actually shipped.

Third-party marketplaces have auto-update **off** by default. To make future releases hands-off,
enable it once: `/plugin` → **Marketplaces** → **kitharness** → **Enable auto-update**. The
installed copy is cached by the `version` in `plugin.json`; a release reaches users only when that
field is bumped.

### Uninstall

```
claude plugin uninstall kitharness@kitharness
claude plugin marketplace remove kitharness
```

Uninstalling only stops it from running — the skills it landed and its own state live **outside** the
plugin and stay on disk. To clear those too, delete its state dir (`~/.claude/kitharness/` global,
`<repo>/.claude/kitharness/` per project) and the self-authored skills under `.claude/skills/` (each
carries a `self-authored` ledger marker, so they're easy to tell from yours). Your own skills are
never touched.

## Configuration

Every knob is an `AUTOHARNESS_*` environment variable with a built-in default — nothing to
configure unless you want to change the pace.

**Placement — which layers it manages**

| Variable | Default | What it does |
|---|---|---|
| `AUTOHARNESS_DISABLE_GLOBAL` | `0` | Set to `1` for a project-only deployment. Global create intents are rejected before staging and landing; global request/use/view counters, archiving, orphan sweeps, recall, curator snapshots, and metrics are skipped. Existing global skills stay untouched. Project-layer learning continues normally. |
| `AUTOHARNESS_GLOBAL_ROOT` | _(unset)_ | Replaces `~/.claude` as the global layer's root. Unset, or set to blank, is stock behaviour — the real home. Set it when KitHarness itself has to run sandboxed (a pinned clone, CI, an evaluation harness) and the production skills library must stay untouched. The project layer is unaffected: it is still keyed to the session's own checkout. |

**Cadence — when it learns**

| Variable | Default | What it does |
|---|---|---|
| `AUTOHARNESS_REFLECT_EVERY_N` | `50` | Reflection cadence, counted in **tool calls**, not turns: every main-session tool call advances a counter, and the turn that pushes it past N ends with a background reflection. A working stretch triggers; a conversation that only talks never does. Lower = learns faster and spawns more child sessions. |
| `AUTOHARNESS_CONSOLIDATE_EVERY_N` | `250` | Curator cadence in **main-session tool calls**, accumulated across sessions in one project. Stop launches at most one pass when the threshold is crossed and preserves the remainder. `0` disables it; negative values fall back to the default. |
| `AUTOHARNESS_DIGEST_EXCHANGES` | `20` | How many exchanges *before* the episode window are compressed into the reflector's prior-context digest (text + tool names only). Unused by the fork carrier, which replays the real conversation instead. |
| `AUTOHARNESS_CARRIER` | `bundle` | What carries the reflection. `bundle` hands a redacted window + digest to a fresh subagent. `fork` resumes and forks the session that just ended, so the reflector reads the real conversation on the parent's warm cache. Stays `bundle` until the cache-hit measurement is in. |

Chat-only turns do not advance either learning cadence. When both are due, Stop requests
consolidation first and keeps reflection activity for a later Stop or SessionEnd. Background
execution remains subject to the [runtime limits](docs/runtime-controls.md); a launch request
is not proof that a model job completed. Existing request counters remain in turns for lifecycle
aging; they are not converted into historical tool activity.

**Recall — what the model sees**

| Variable | Default | What it does |
|---|---|---|
| `AUTOHARNESS_INDEX_SUSPENDED` | `0` | Set to `1` to stop skill-index injection in both task and catalog modes. Lifecycle, use/view counters and last-run reporting continue. |
| `AUTOHARNESS_RECALL_MODE` | `task` | Rank matching skills on `UserPromptSubmit`; `catalog` instead offers a bounded grouped directory at SessionStart. No query history is stored. |
| `AUTOHARNESS_RECALL_MAX_BYTES` | `4096` | Maximum decoded UTF-8 bytes of this plugin's additional context per hook event, including headings and path hints. `0` suppresses context. This does not cap the whole conversation or host-native skill context. |
| `AUTOHARNESS_RECALL_TOP_K` | `5` | Maximum selected skills per injection. Zero-overlap task queries inject none; entries that do not fit are skipped whole. |
| `AUTOHARNESS_INDEX_DESC_MAX_CHARS` | `60` | Display-only prefix preview length in either recall mode; truncated previews end in `...`. Full descriptions remain available for matching and authoring. |
| `AUTOHARNESS_SKILL_DESC_MAX_CHARS` | `1024` | Independent full-description authoring cap for create/update, in Unicode characters. Chinese/English trigger cues are accepted; legacy descriptions remain repairable by patch. |
| `AUTOHARNESS_SKILL_BODY_MAX_LINES` | `25` | Altitude cap: a `SKILL.md` body over this many non-blank lines is rejected as a transcript rather than a rule. Backing detail belongs in the skill's `references/`. |

Descriptions support Chinese conditions such as `当…时使用` and `用于…`, and quoted user phrases
such as `“检查日期边界”`. Full text and display previews have separate limits; the preview is
a shortened prefix, not a semantic summary. See [description authoring](docs/description-routing.md).
Task ranking, explanations, token-estimation limits and the fixed synthetic comparison are documented
in [task recall](docs/task-recall.md).

**Lifecycle — what survives**

| Variable | Default | What it does |
|---|---|---|
| `AUTOHARNESS_MATURITY_PROJECT` | `100` | Probation gate, project layer: after this many requests have arrived in its layer since a skill landed, it faces graduation review. Until then it's recalled as usual but can't be archived. |
| `AUTOHARNESS_MATURITY_GLOBAL` | `300` | Same gate for the global layer — higher because a global skill loads in every project. |
| `AUTOHARNESS_CAPACITY_PROJECT` | `50` | Cap on mature skills surviving graduation review, including viewed-only skills and those spared by suspended review. Lowest use/request rates go first; views do not raise that rate. Probation remains outside the cap, so this does not bound the full session-start index. |
| `AUTOHARNESS_CAPACITY_GLOBAL` | `20` | Same cap for the global layer — smaller because its blast radius is every project. |
| `AUTOHARNESS_GRADUATION_SUSPENDED` | `0` | Set to `1` to suspend graduation review when recall is unreliable. Mature zero-use skills still join capacity contention and can be archived under pressure. |
| `AUTOHARNESS_SNAPSHOT_KEEP` | `5` | How many pre-run snapshots of each skill tree the curator keeps before merging. A merge is the one operation a single atomic rename can't undo. |

**Notify — hearing about it as it happens**

Off by default. The session-start summary line reports a run one session late, with counts but no
names; these push the same run account out as soon as a drain finishes. Fail-open: the notifier runs
only after the account is written and the intent queue cleared, so a missing, failing or hung
notifier can delay a drain but never fail or replay it.

| Variable | Default | What it does |
|---|---|---|
| `AUTOHARNESS_NOTIFY` | _(unset)_ | Set to `desktop` for a native notification per run — `osascript` on macOS, `notify-send` on Linux when installed (a silent no-op otherwise). Names what landed and what was rejected, e.g. `create foo, patch bar · rejected: baz`. On macOS the notification is attributed to Script Editor; if nothing appears, allow it under System Settings → Notifications. |
| `AUTOHARNESS_NOTIFY_CMD` | _(unset)_ | A command (split like a shell argv, never run through a shell) that receives the run record JSON — the same shape as `runs/<run-id>.json` — on stdin, with the one-line summary in `AUTOHARNESS_NOTIFY_SUMMARY`. The hook for Slack, webhooks, or anything else. Skill names are redacted first, and the command runs with the child-session guard set, so a notifier that launches `claude` isn't itself captured. Works alongside `desktop`. |
| `AUTOHARNESS_NOTIFY_TIMEOUT_S` | `5` | Whole seconds, minimum 1, per channel: how long a notifier may hold up a drain. The `/learn` drain runs inside the host's Stop hook, so this is also the most it can delay a turn. |

Set them in the environment Claude Code launches with — either the shell
(`export AUTOHARNESS_REFLECT_EVERY_N=10`) or the `env` map in `.claude/settings.json`:

```json
{ "env": { "AUTOHARNESS_REFLECT_EVERY_N": "10" } }
```

Hooks read the environment on every event, so a change applies from the next session. The defaults
are deliberate placeholders pending empirical calibration; byte caps
on captured windows and staged files are fixed constants, not env knobs.

## How it works

A learning pipeline runs beside the host. Skills are plain native files, recalled by the host's own
name-and-description mechanism as if a human had written them — that path is left untouched. On top
of it, task mode offers lexical matches when a prompt is submitted. Catalog mode offers a bounded
directory at session start. Both use only agent-created skills and keep retrieval separate from use counts.

<p align="center"><img src="docs/assets/pipeline.svg" alt="KitHarness pipeline: host → CAP → REF → promoter → .claude/skills → host, with the curator feeding the promoter, IDX injecting the index at session start, and MNG and LED beside" width="760" /></p>

<sub>Diagram source: [`docs/assets/pipeline.mmd`](docs/assets/pipeline.mmd) — re-render to `pipeline.svg` after editing.</sub>

The diagram shows the original catalog path; default task mode injects at UserPromptSubmit instead.

| Component | Role |
|---|---|
| **CAP** · capture | Hook-driven dumb pipe: grabs each turn (user input, agent output, tool I/O), redacts at egress, points back at the host log instead of copying it. It also holds the trigger, which is deterministic and counts one thing — tool calls. The turn that crosses the threshold ends with a reflection; nothing about the content is judged here. |
| **REF** · reflect | Reads the episode, compares it against the existing skill index, and decides add / merge / patch / drop a support file / delete — emitting an intent (body, delta, or path, plus reason and evidence). Where a new lesson contradicts an older skill, it must rewrite the stale one in the same run rather than leave the library arguing with itself. Proposes only; it has no write tools, and a fork carrier's inherited ones are denied at the hook. |
| **promoter** · validate·store | The only writer. Lints the intent in memory (safety, structure, ledger, completeness, self-authored-only) and on pass does an atomic rename into the live skill directory. Full descriptions have their own authoring limit and accept English/Chinese trigger cues independently of the compact index preview. A fold must name the skill that absorbed the deleted one, and the umbrella has to be a live skill KitHarness manages — an invented name fails the whole intent rather than losing the content. Each run leaves an account of what landed and what was rejected. |
| **IDX** · surface | Scores the full name/category/description with field-weighted BM25 and English/Chinese terms, selects matches within a byte and top-K budget, and displays short previews. Read-only CLI explanations show matched terms, scores and exclusion reasons. Catalog mode retains the grouped startup directory; last-run reporting remains at SessionStart. This is lexical retrieval, not embedding similarity or a measured task-success improvement. |
| **MNG** · lifecycle | Daemon-free: recomputed lazily at session start, once per session. Ranks symbols by usage rate — loads over the requests that arrived since the symbol was created, so the measure is opportunity-relative and a closed laptop doesn't age anyone out (the wall-clock replacement). Three signals are kept apart: a **load** (the model invoked the skill) is the only thing the rate counts; a **view** (a session read into the skill's directory) is evidence it had recall value, but not adherence; a **patch** marks the skill being improved, so a load after one reads as reuse-after-improvement. New symbols sit in probation until they've had a fair sample of requests: recalled as usual, but neither counted against the cap nor evictable. At maturity, graduation review archives only a symbol that was never loaded *and* never viewed — no evidence of use is not the same as evidence of no use. For graduates, capacity contention is the only death — nothing is archived until a layer's mature pool exceeds its cap, then the lowest rates go first. Archives, never deletes: an archived symbol is a directory moved out of recall, and moving it back revives it. |
| **curator** · consolidate | The rarer whole-library pass: reads the library as one thing and folds near-duplicates under umbrellas, which is the judgment a single episode can't make. Snapshots both skill trees before it starts, since a merge is the one operation an atomic rename can't undo. |
| **LED** · ledger | Per-symbol append-only sidecar: why each symbol was born or changed, with evidence and a reflection watermark. Kept out of the skill body so recall stays clean. |

## Walkthrough: watching it learn

Everything KitHarness does lands on disk as plain files — a demo is just opening them in the
right order. For a fast-paced run, speed up the loop first (see [Configuration](#configuration)):

```json
{ "env": { "AUTOHARNESS_REFLECT_EVERY_N": "3",
           "AUTOHARNESS_MATURITY_PROJECT": "5",
           "AUTOHARNESS_CAPACITY_PROJECT": "2" } }
```

**1 · The pipeline running.** Work a few normal turns on anything non-trivial (debug something,
figure out a workflow). Once a turn pushes the tool-call count past N, a background reflection
fires as that turn ends — nothing blocks your session. Its bookkeeping is visible in the state dir:

```
ls .claude/kitharness/        # per project — ~/.claude/kitharness/ for the global layer
  requests                     # layer request counter (MNG's denominator)
  session-<id>                 # tool calls counted toward the next reflection
  offset-<id>                  # byte watermark: where the last captured window ended
  intents/                     # queued skill proposals awaiting the promoter
  runs/<run-id>.json           # what that run proposed, landed, and rejected — with reasons
  last_run.json                # the summary line awaiting the next session start
  snapshots/                   # skill-tree tarballs the curator takes before merging
```

**2 · A skill is born.** After a reflection lands, a new folder appears under `.claude/skills/`
(project) or `~/.claude/skills/` (global — for techniques that aren't repo-specific). Use `ls -la`:
the interesting files are hidden.

```
.claude/skills/<name>/
  SKILL.md                     # the skill itself — plain native format, nothing proprietary
  .ledger.jsonl                # LED: why it was born / changed (append-only)
  .sidecar.json                # lifecycle counters MNG reads
  references/evidence-*.md     # the transcript slice that justified each ledger entry
  scripts/ templates/ ...      # optional support files the reflector attached
```

**3 · LED — the paper trail.** `cat .ledger.jsonl` — one JSON line per lifecycle event:

```json
{"action": "create", "reason": "User asked about the correct command to update a plugin ...", "evidence": "references/evidence-21cd22cc.md"}
{"action": "patch",  "reason": "User discovered /reload-plugins is required in-session ...",  "evidence": "references/evidence-1a4ec51d.md"}
```

`action` + `reason` + `evidence` — and the evidence file is a real, redacted slice of the session
that taught it, materialized by the promoter (content-addressed, so the model never names files).
This is the "evidence kept for later" from the table above.

**4 · An update, not a duplicate.** Hit the same scenario again with a correction ("that's missing
a step") and let the next reflection run. The skill layer does **not** grow a near-duplicate:
the existing skill's `SKILL.md` changes and its ledger appends a `patch`/`update` line — the
two-line ledger above is a real example. `git diff` on a project-layer skill shows the edit.

**5 · The next session opens knowing.** Start a new session and the first thing it receives is a
grouped index of these skills — plus a one-line report of the last run, so a proposal that got
rejected says so instead of vanishing. The host's own recall still runs untouched underneath;
the index only makes sure the library is in front of the model either way.

**6 · Use is counted three ways.** `cat .sidecar.json`: `use` ticks when the model actually loads
the skill and is the only thing the survival rate counts; `view` ticks when a session reads into
the skill's directory — recall value, but not adherence; `patch` ticks when the promoter lands an
improvement, so a `use` after one is reuse-after-improvement. Keeping them apart is what stops a
skill that was merely browsed from looking like one that was followed.

**7 · Retirement is an archive, not a delete.** Two paths out, both a folder move to
`.claude/skills/.archive/<name>/` — ledger, evidence and all, out of recall. Graduation review
archives a skill only if its whole probation passed with no use *and* no view; after graduation,
once a layer's mature pool exceeds capacity the lowest-usage-rate skills go. Moving the folder
back revives it, history intact. With the shrunk knobs above this fires within one session; at
defaults it takes hundreds of requests. A skill merged into another is archived the same way, but
its ledger names the umbrella that absorbed it — a fold and a pruning stay distinguishable.

**8 · Yours are never touched.** Every KitHarness-authored skill carries the ledger marker;
anything without it — skills you wrote or installed — is invisible to the promoter and MNG.

## Task evaluation toolkit

Step 10 provides a pinned three-arm task protocol (`no_learning`, baseline,
KitHarness), six structured workflow tasks, deterministic JSON grading, failure accounting,
and telemetry coverage. It prepares and scores artifacts without invoking models or executing
candidate code. The bundled smoke report uses synthetic answers and is not evidence of model gains.
See [task evaluation](docs/task-evaluation.md) for reproduction and the remaining live-host protocol.
The [host preflight](docs/live-evaluation.md) preserves authentication checks and failed pilot attempts.
Step 11 completed a [controlled live DeepSeek study](docs/live-study-results.md), including real MCP
reflection/curation, frozen libraries, all-phase accounting, and an offline-verifiable evidence archive.
All three arms passed the 24 synthetic cases; this small run does not demonstrate an accuracy gain.

## How it compares

A self-learning skill layer can be validated against a held-out benchmark, or against its own use.
The live path still takes the second. The synthetic recall check and the one live study are separate measurements; they are not a held-out accuracy win.

| | Grow unbounded | Offline-gated self-edit<br/>([Self-Harness](https://arxiv.org/abs/2606.09498)) | Timer + daemon<br/>([hermes-agent](https://github.com/NousResearch/hermes-agent)) | KitHarness |
| --- | --- | --- | --- | --- |
| Bounds the skill layer | No | Yes | Yes | Yes |
| Validation signal | None | Held-out benchmark score | Wall-clock inactivity | Adherence in use |
| What starts a learning pass | — | An offline batch | Idle time and elapsed days | Work done in the session |
| Puts its own library in front of the model | No | No | Yes | Yes |
| Needs a benchmark / oracle | No | Yes | No | No |
| Needs a resident daemon | No | No | Yes | No |

## Acknowledgements

[NousResearch/hermes-agent](https://github.com/NousResearch/hermes-agent) — studying its
auto-skill-creation and memory-consolidation design helped sharpen this adherence-based,
daemon-free take.

Changes and measurements are listed in [docs/PLAN.md](docs/PLAN.md). This checkout is <https://github.com/jiapy97/KitHarness>.

## License

[MIT](LICENSE)

---

KitHarness · <https://github.com/jiapy97/KitHarness>
