---
name: learn
description: "Use when users /learn, save, remember, or distill lessons."
category: general
---
# Learn: distill this session into the skill library

Distill reusable lessons through `stage_skill`; never write skill files directly.

1. Capture corrected approaches, useful techniques, and durable user preferences. Project-specific workflow or formatting preferences are valid project skills; cross-project technical generality is not required. Skip one-off narratives and environment-specific failures.
2. Compare the managed skill index first; prefer `patch`/`update` for the same scenario and reconcile contradictory rules in the same run. When the user explicitly requests a project skill, existing native memory or project instructions are not a reason to refuse it or substitute a memory write.
3. Preserve only user-confirmed preferences and supported lessons. Do not turn assistant-suggested font sizes, spacing, or exceptions into user requirements. When asked to save only a stated preference, do not expand it with unconfirmed details copied from memory or project instructions.
4. Submit one intent per lesson, with `reason` and a verbatim `evidence` quote. Use `level: project` when requested. For `create`/`update`, `body` must contain a complete SKILL.md, including YAML frontmatter; a Markdown heading alone is invalid. Keep the body within the configured non-blank line limit (default 25); put detailed examples in referenced subfiles. A simple preference needs only one or two rule lines.

Minimal project-skill body (adapt the name, trigger and rule to the actual lesson):

```markdown
---
name: print-document-fonts
description: 当在本项目中生成用于打印的文档时使用。
category: document-generation
---
生成用于打印的文档时，使用用户明确确认的字体系列。
```

If `stage_skill` returns `ok: false`, read `errors`, correct the arguments and retry when repairable. A frontmatter/description error is a format error, not a ban on user preferences or project skills. If it remains blocked, report the actual tool error.
`ok: true` means the proposal is queued; the Stop hook validates and lands it. Report queued status, and claim it is saved only after checking the resulting file or run record.
If there is genuinely no reusable lesson and no explicit request to persist a supported rule, explain why and stage nothing.
