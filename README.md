# Usage Handoff

A shared skill for Codex, Claude Code, and Claude desktop/Cowork. It checks remaining usage before substantial actions and saves a Markdown handoff when the next action may consume the budget needed to preserve progress.

| Host | Usage signal | Stop behavior |
| --- | --- | --- |
| Codex desktop | Exposed `get_usage_limits` tool | Agent follows skill, saves handoff, ends turn |
| Codex CLI | Read-only `account/rateLimits/read` adapter | Agent follows skill, saves handoff, ends turn |
| Claude Code | Official status-line `rate_limits` data, when emitted | Local hook saves the latest checkpoint and stops before the next tool |
| Claude desktop chat / Cowork | Exposed supported tool or a fresh supplied Usage reading | Agent follows skill and exports a handoff; without a signal, only checkpoint fallback is available |

This is an early-warning estimate. Neither exact future action cost nor provider interruption can be guaranteed. Context-window percentage is not an account usage balance. A missing reading is explicitly unknown.

The handoff reserve comes first. Start a costly step only when every applicable meter has enough for the whole step plus its protected handoff reserve. Default percentage rule: stop when remaining usage **≤ max(10, protected reserve + 1.5 × estimated action cost)**. The reserve is at least 5 percentage points; a separate fresh `handoff_estimates` entry raises it to **max(5, 1.5 × estimated handoff cost)**. It covers preserving state, preparing the handoff, saving/reading it back, and delivering the resume link. Task work cannot borrow from it. Without a calibrated action estimate, a healthy meter does not certify that the step fits: only usage checks and checkpoint writes should proceed until a fresh forecast is available, otherwise save a handoff. Thresholds are configurable, but the handoff reserve must stay positive. Absolute credit/dollar/token budgets need reserves and estimates in their own units.

## Install locally

Python 3.10+ is required; no runtime packages are needed. From this directory:

```powershell
python .\install.py
```

The installer copies the skill to `~/.agents/skills/usage-handoff` for current Codex discovery and `~/.claude/skills/usage-handoff` for Claude Code. It adds a small activation block to `~/.codex/AGENTS.md` (or the configured `CODEX_HOME`) and `~/.claude/CLAUDE.md`, plus Claude Code prompt/tool hooks and a status-line adapter. Existing settings, hooks, and instruction text are preserved. Changed existing files get adjacent `.usage-handoff-*.bak` backups. Existing status-line commands are delegated to with their original session JSON. Installing again does not add duplicate hooks.

Open a new Codex chat and restart Claude Code to load the new instructions. Explicitly activate with `$usage-handoff` in Codex or `/usage-handoff` in Claude Code if needed. Some hosts or managed policies may not load user instructions or hooks; verify activation in the actual session.

The installer prints the installation paths and creates `dist/usage-handoff.zip`. To make only the ZIP without changing user settings:

```powershell
python .\install.py --package-only
```

## Enable in Claude desktop

Open **Customize → Skills → + → Create skill → Upload a skill**, upload `dist/usage-handoff.zip`, and enable it. Code execution must be enabled. The account upload applies to desktop chat and Cowork; local Claude Code installation alone does not enable those surfaces. See [Claude's upload instructions](https://support.claude.com/en/articles/12512180-use-skills-in-claude).

Start a task with “Use usage-handoff while working on this task.” If no supported live usage tool is exposed, provide a current reading for all applicable limits and its time. The skill cannot inspect your account's Usage screen from its code container. For strict monitoring, tell it to hand off whenever usage cannot be measured. Fully automatic detection in desktop requires a supported live usage feed that the agent can access.

## Handoffs and resuming

Local sessions store usage and `checkpoint.json` under a unique `.usage-handoff/<session-key>/` directory in the workspace. The prompt hook tells Claude Code which path to update. Handoffs are named separately under its `handoffs/` directory. The skill must update the checkpoint after milestones; the emergency hook can only preserve what was saved. No hook reads raw prompts, tool arguments, or transcripts.

Paste this into the next agent with the saved file attached or accessible:

> Read the attached Usage Handoff. Verify workspace state, running jobs, and pending external effects, check fresh usage, then continue the original task from its next steps.

Handoff creation does not create another chat, send messages, commit work, or buy credits. Claude Code hooks run at tool boundaries and do not kill jobs already running. Codex follows the skill cooperatively; this package does not install Codex enforcement hooks.

For manual signals and configuration, read [the state schema](usage-handoff/references/state.md). For each host's limits and official sources, read [platform behavior](usage-handoff/references/platforms.md).

## Validate

```powershell
python -m unittest discover -s tests -v
```

Tests exercise fresh/stale/missing readings, multiple windows, forecasting, unit matching, session isolation, stop artifacts, installer preservation and idempotence, ZIP shape, and the app-server protocol using a fake local process. They do not spend model usage.

## Remove locally

Remove only the usage-handoff blocks marked `<!-- usage-handoff:start -->` and `<!-- usage-handoff:end -->` from the two instruction files, remove only the hook entries whose command invokes `guard.py claude-hook`, restore the prior `statusLine` from `agents/statusline-delegate.json` if present (otherwise remove the installed `statusLine`), and remove the two installed skill folders. Leave task checkpoints/handoffs intact. Backups can help inspect prior settings; do not restore an old entire file over subsequent unrelated changes. Disable the uploaded skill separately in Claude desktop.
