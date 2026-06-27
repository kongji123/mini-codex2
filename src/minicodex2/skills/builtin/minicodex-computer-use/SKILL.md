# MiniCodex Computer Use

Use this skill when a task requires observing a rendered UI, deciding the next visual/browser action, or repairing a failed UI action.

This package demonstrates MiniCodex2's plugin-shaped skill mechanism:

- `skill.toml` declares domain, role, capabilities, and hooks.
- `hooks.py` contains trusted hook code.
- `prompts/*.md` contains hook prompt text.
- `references/*.md` contains longer on-demand guidance for the model to load with `load_skill`.

Agent OS still owns tools, permissions, logs, screenshots, browser sessions, evidence, and persistence. This skill only supplies computer-use workflow guidance.

## Hooks

- `visual_task_preflight`: observe before acting.
- `action_repair`: recover after a UI/browser action fails.

## References

- `references/visual-action-loop.md`
- `references/action-repair.md`
