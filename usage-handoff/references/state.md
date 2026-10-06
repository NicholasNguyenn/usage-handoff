# Checkpoint and guard schema

The helper needs Python 3.10+ and only its standard library. Resolve the installed skill directory before using the example commands. Each task has a separate directory under `.usage-handoff/`; Claude Code's prompt hook supplies its path, while other hosts should choose a unique task identifier with `paths`:

```text
python <skill-directory>/scripts/guard.py paths --root <workspace> --session-id <unique-task-id>
```

## Checkpoint

Write `checkpoint.json` at the returned path and keep it current. Store concise strings or lists in the fields below. Supply actual facts; empty values mean unavailable. `saved_at` is Unix seconds. `action_estimates` maps **each applicable snapshot window ID** to a conservative cost in that window's unit. Optional `handoff_estimates` maps those same IDs to the full cost of preserving and delivering the handoff. The reserve never falls below its configured minimum; a fresh handoff estimate can only increase it. Without a handoff estimate, the configured minimum is a heuristic that must be sized to the task. A new action needs new estimates; do not reuse an estimate for a different action. The helper never invents them.

```json
{
  "saved_at": 0,
  "task": "The user's original task and completion criteria",
  "constraints": [],
  "workspace": "Absolute workspace path, or available cloud artifact location",
  "branch": "",
  "completed": [],
  "artifacts": [],
  "current_action": "The complete bounded action currently planned, including validation",
  "decisions": [],
  "verification": [],
  "running_jobs": [],
  "pending_approvals": [],
  "next_steps": [],
  "estimate_basis": "Observed comparable work or an explicit user budget; unknown if uncalibrated",
  "action_estimates": {},
  "handoff_estimate_basis": "Observed cost of checkpoint update, compact handoff preparation, save/read-back, and final delivery, or a conservative configured reserve",
  "handoff_estimates": {},
  "resume_prompt": "Read this handoff, verify the workspace and running jobs, check fresh usage, then continue the original task."
}
```

No script reads Git diffs, prompt text, tool arguments, or transcripts. Record relevant facts yourself after checking them. The hook creates a minimal checkpoint when one is absent and explicitly labels it incomplete; it cannot supply your task understanding.

## Usage snapshot

Normalize a supported raw reading immediately after fetching it:

```text
python <skill-directory>/scripts/guard.py normalize --provider codex --input <temporary-raw-result.json> --output <session-directory>/usage.json
```

Prefer piping the JSON to stdin instead of storing raw account data; omit `--input` to read stdin. Do not resample an old raw result to make it appear fresh. `--sampled-at` preserves the actual fetch time. Providers are `codex`, `claude`, and `manual`. The Claude status-line adapter normalizes automatically.

A manual normalized reading has this form (replace sample time and values with actual readings):

```json
{
  "provider": "manual",
  "source": "User's current Usage screen; confirm account and applicable windows",
  "sampled_at": 0,
  "windows": [
    {"id": "five_hour", "unit": "percent", "remaining": 13, "resets_at": null},
    {"id": "seven_day", "unit": "percent", "remaining": 24, "resets_at": null}
  ],
  "exhausted": false
}
```

Codex IDs are `<limit-id>.primary` and `<limit-id>.secondary`; Claude IDs are `five_hour`, `seven_day`, and `spend_limit`. Custom absolute budgets may use `credits`, `usd`, or `tokens` as the unit. Keep each window ID unique. Only include applicable meters; disclose missing applicable meters rather than inventing them. A null value, invalid number, expired reset, old sample time, or missing window is uncertain. Source adapters retain a missing known window as null.

## Evaluate or hand off

```text
python <skill-directory>/scripts/guard.py check --snapshot <session-directory>/usage.json --checkpoint <session-directory>/checkpoint.json
python <skill-directory>/scripts/guard.py handoff --root <workspace> --session-id <unique-task-id> --reason "Usage reserve reached"
```

`check` prints JSON and returns exit code 0 for `continue`, 10 for `handoff`, and 20 for `checkpoint` (unknown or uncalibrated). Only `continue` permits a costly action. On `checkpoint`, use only short usage checks and checkpoint writes, then check again; if the forecast remains unavailable, save a handoff. These nonzero codes are deliberate decision signals, not a reason to retry the task. `handoff` reads the session checkpoint and snapshot, writes Markdown, reads it back, and returns its path. It does not stop another process or notify another chat.

Each assessed window reports `buffered_action_cost`, `estimated_handoff_cost`, `handoff_reserve`, and total `required`. The rule is `remaining > max(stop_floor, handoff_reserve + safety_factor * action_estimate)`. The protected handoff reserve is `max(configured_reserve, safety_factor * handoff_estimate)` when a fresh handoff estimate is supplied. At equality, save the handoff. Even without an action estimate, a known reading at or below the protected reserve stops work.

Optional `config.json` in the session directory overrides these defaults:

```json
{
  "max_age_seconds": 120,
  "safety_factor": 1.5,
  "percent_stop_floor": 10,
  "percent_handoff_reserve": 5,
  "unknown_policy": "checkpoint",
  "absolute_budgets": {
    "prepaid": {"unit": "credits", "stop_floor": 15, "handoff_reserve": 5}
  }
}
```

Pass `--config` to `check` when using a custom file. Claude hooks read the session's `config.json` automatically. `unknown_policy: "handoff"` is strict mode: uncertain signals also stop and save a handoff. Percentage reserves must be positive; setting the reserve to zero is rejected. Absolute budgets need a matching unit, positive reserve, nonnegative floor, and a same-unit action estimate. Never subtract an estimate in credits from a percentage meter.

Claude Code's status-line adapter takes official session JSON on stdin. Its prompt and tool hooks take official hook JSON on stdin. Their configuration is generated by the installer; do not run them with a task's free-form text as input. The status-line wrapper delegates only the existing configured command, not a command from incoming session data.
