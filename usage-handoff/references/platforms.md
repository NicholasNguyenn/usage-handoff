# Supported usage sources

Verified against official documentation on 2026-10-06. Recheck documentation if fields or host behavior differ. No private endpoints or credential scraping are needed.

## Codex desktop and CLI

When available, call the Codex app's `get_usage_limits` tool. Prefer `rateLimitsByLimitId` over the legacy `rateLimits`. Select the active model's metered bucket; the helper defaults to `codex`, and `--limit-id` selects another known bucket. Do not gate ordinary task work on an unrelated review limit. `usedPercent` is consumed; remaining is `100 - usedPercent`, clamped to 0–100. `resetsAt` is Unix seconds. Null means unavailable.

Save only the normalized snapshot, not the raw tool envelope or account ID. The helper accepts a JSON result or an MCP text-content envelope. Do not treat a token count, context-window meter, elapsed time, or local cost estimate as an account balance.

For local CLI without the app tool:

```text
python <skill-directory>/scripts/guard.py read-codex --output <session-directory>/usage.json
```

This starts `codex app-server` temporarily, performs `initialize` / `initialized`, reads `account/rateLimits/read`, and closes it. It never starts a thread or model turn. Use `--executable` for the installed Codex binary if it is not on PATH. Timeout/auth/network failures give unknown usage. This reads the CLI's signed-in account, which must match the task's account. Prefer the app tool if accounts or hosts differ. API-key billing needs a separately supplied billing/budget meter; a ChatGPT plan meter cannot describe it.

Sources: [skill discovery](https://learn.chatgpt.com/docs/build-skills), [read-only account rate limits](https://learn.chatgpt.com/docs/app-server). The local skill checks cooperatively; this package does not install Codex enforcement hooks. Provider interruption and concurrent usage can occur between checks.

## Claude Code

The supported status-line input can contain `rate_limits.five_hour`, `rate_limits.seven_day`, and `rate_limits.spend_limit`. Use `used_percentage` and `resets_at`. These fields can be absent, particularly before a response, with older versions, or for some billing/auth modes. A spend meter can also include `used_usd` and `limit_usd`; percentage gating is used when supplied. `cost.total_cost_usd` and `context_window.remaining_percentage` are different measurements and are not substitutes.

The installer adds `UserPromptSubmit` and `PreToolUse` hooks, plus a status-line adapter. Existing hooks remain intact; an existing status-line command is delegated to with the original stdin. The adapter saves normalized usage and sample time; it uses the session ID only to scope the state directory. Local status-line scripts consume no model tokens.

State is scoped by session ID under the current workspace's `.usage-handoff/`. The prompt hook supplies the checkpoint path. Before each tool, the guard uses a snapshot no older than 120 seconds. Fresh insufficient usage produces a handoff and `continue: false` / `stopReason`, stopping Claude before that tool runs. Unknown usage produces a checkpoint reminder; it does not pretend to enforce a missing budget. If the workspace is not writable, the hook reports that failure.

Hooks run at tool boundaries, after the model has prepared a call. They cannot interrupt already running tools or ongoing generation, and cannot guarantee a complete handoff without a maintained checkpoint. No raw prompt or transcript is copied by the hooks. Headless sessions or versions that do not emit status-line data use the unknown-reading fallback.

Sources: [status-line data](https://code.claude.com/docs/en/statusline), [hook stop semantics](https://code.claude.com/docs/en/hooks), [skill locations](https://code.claude.com/docs/en/skills).

## Claude desktop, including Cowork

Upload the packaged ZIP and enable it under Customize → Skills. Code execution must be enabled. Account-uploaded skills apply to desktop chat and Cowork; installing a skill under `~/.claude/skills` alone does not enable it in Cowork.

If no supported usage-reading tool is exposed, use a user-supplied current Usage reading with its sample time. For example, 87% used in the five-hour window becomes 13 percentage points remaining. Gather other applicable windows too. Never silently assume an unreported weekly or spend meter is healthy. User-provided readings are snapshots, not a live feed; refresh them before a long action. A Claude Code snapshot is usable only if the same account and scope are confirmed and the file is accessible. Claude's product surfaces share allowance, so concurrent activity can deplete it.

In chat, export a Markdown artifact using the available file tool, or give the complete handoff inline if no file tool is available. Do not reference a laptop path from the container as if it were accessible on the user's machine. In Cowork, use the folder the user granted. There is no documented general skill API to poll the account's remaining usage from desktop chat; fully automatic detection there requires an exposed supported usage feed. The ZIP provides the same decision and handoff protocol with that limitation.

Sources: [upload and enable skills](https://support.claude.com/en/articles/12512180-use-skills-in-claude), [custom ZIP structure](https://support.claude.com/en/articles/12512198-how-to-create-custom-skills), [shared usage and context distinction](https://support.claude.com/en/articles/11647753-how-do-usage-and-length-limits-work).
