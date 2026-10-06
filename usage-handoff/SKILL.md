---
name: usage-handoff
description: Monitor remaining usage during active work in Codex, Claude Code, or Claude desktop. When the next action may exhaust the budget, stop safely and save a resumable handoff.
---

# Usage handoff

Reserve enough usage to save a handoff before starting another costly step. This reserve is protected: task work, retries, and validation cannot spend it. Apply throughout the active task, starting before substantial work. A skill is cooperative guidance, not a background monitor or a guarantee against a provider cutting off a request. Read [platforms.md](references/platforms.md) for the current host; use only its supported usage source.

## Check and reserve

1. Save a small checkpoint immediately, then update it after meaningful changes and before a long action. Record the task, constraints, artifacts, completed work, exact next steps, verification, and any running jobs. Use a unique session/task identifier. Read [state.md](references/state.md) for the helper schema and commands. Without local file tools, keep the checkpoint as a visible artifact in the conversation.
2. Read current account usage before substantial work, before a long action, and after each milestone. Reuse a reading for at most 120 seconds. Check every applicable window (including weekly or spend limits), not just the shortest one. Usage is shared with other chats; changes are not necessarily this task's cost.
3. Estimate the cost of completing the next bounded action, including validation and any planned retries or concurrent work, in the same units as each meter. Separately size the handoff reserve to cover a minimal safe finish of an in-progress write, the checkpoint update, compact handoff preparation, file save/read-back, and the final link or inline delivery. Use conservative estimates from comparable observed work or an explicit user budget. Record both estimates and their bases when known. Percentage estimates are percentage points of that specific window, not tokens or money. Never invent a credits-to-tokens conversion. Recalibrate after model, reasoning, speed, or workload changes.
4. Set aside the handoff reserve first. Start the action only if every applicable meter has **remaining > max(stop floor, protected handoff reserve + buffered action cost)**. With percentage meters, the defaults are a 10-point stop floor, a minimum 5-point handoff reserve, and a 1.5 safety factor. If a fresh handoff cost estimate is available, the protected reserve is **max(5, 1.5 × estimated handoff cost)**; otherwise use the configured reserve and increase it when the handoff needs more. The default action allowance is 1.5 × estimated action cost. These are configurable heuristics, not provider rules or guaranteed costs. Hand off at equality too. With absolute credit, dollar, or token meters, explicitly supply a positive reserve and stop floor in those same units. The strictest applicable meter wins; do not count purchased credit fallback unless the user explicitly chooses that budget.
5. Missing, stale, reset-expired, or invalid readings and estimates mean **unknown**, never zero or unlimited. Refresh once through the supported source. Until a fresh forecast protects the reserve, use only short actions needed to read usage and save a checkpoint; do not start costly task work. If the budget still cannot be established, state the limitation and save a handoff before another costly step. Strict monitoring hands off immediately on uncertainty. A known exhausted limit still requires a handoff even when another reading is unknown.

The bundled `scripts/guard.py` normalizes supported readings and evaluates this rule deterministically. A `continue` result requires fresh meters and action estimates for all supplied windows, plus valid fresh handoff estimates when supplied. `checkpoint` permits only the usage checks and checkpoint writes in step 5; it is not permission for costly task work. `handoff` means stop task work now. Recheck before each new step; an earlier approval does not reserve usage against consumption by other chats.

## Stop and preserve

When the rule triggers, stop starting new task actions, retries, tool batches, or delegated work. Finish only the minimal safe operation needed to leave an already-started write consistent. Inspect an existing job if needed to preserve its identifier and effects; do not blindly kill it or start broad validation to make the handoff look finished. Record what is still running and what is unknown.

Write a compact Markdown handoff in the task's writable workspace, with a task-specific name. The helper uses `.usage-handoff/<session-key>/handoffs/`. Include:

- Original task, constraints, and completion criteria.
- Workspace, branch/worktree if relevant, changed files and external artifact links.
- Completed work, current partial action, and decisions that affect continuation.
- Checks actually run and their results; outstanding or failed checks.
- Running job/session identifiers, possible external effects, and pending approvals.
- Usage source and sample time, applicable remaining limits and reset times, estimates, and why work stopped. Clearly label estimates and unavailable data.
- Ordered next steps and a ready-to-paste resume prompt.

Preserve user edits and uncommitted work. Do not reset, clean, commit, push, publish, purchase credits, switch models/accounts, message another chat, or create a new chat just to hand off unless the user has authorized that action. A handoff file plus resume prompt is the default. Do not include credentials, raw auth data, private reasoning, or entire transcripts.

Verify the saved file exists and can be read. If file writing is unavailable, give the handoff inline and explicitly state it was not saved to disk. Link the handoff, state the stop reason briefly, and end the turn. Do not claim completion while work remains. On resume, read the handoff, verify workspace state and running jobs, then obtain a fresh usage reading before continuing. A reset timestamp alone is not proof that budget replenished.

## Host behavior

- **Codex desktop:** prefer the exposed `get_usage_limits` tool, when available; normalize its returned data without storing account identifiers. Local Codex CLI can use the read-only app-server helper described in [platforms.md](references/platforms.md).
- **Claude Code:** the installed status-line adapter records official `rate_limits` fields. The installed hooks activate this skill and check before tools. On known insufficient usage, the hook saves a handoff from the latest checkpoint and stops the turn without spending another model call. Keep that checkpoint current: the hook cannot reconstruct decisions that were never saved.
- **Claude desktop chat / Cowork:** upload and enable the ZIP in the account's Skills settings. Use a supported exposed usage tool if one exists, otherwise a recent user-provided usage reading. The skill has no documented access to the account's Usage UI from its code container. Without that signal, automatic account-budget detection is unavailable; use the checkpoint fallback above. Export a handoff artifact in chat, or write into an authorized Cowork folder.

Explicit invocation: `$usage-handoff` in Codex, `/usage-handoff` in Claude Code, or “Use usage-handoff while working on this task” in Claude desktop. Automatic discovery alone may not load a skill for every task; the local installer adds small activation instructions for Codex and Claude Code. Desktop account upload still requires enabling the skill.
