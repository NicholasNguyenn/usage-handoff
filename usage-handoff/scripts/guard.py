#!/usr/bin/env python3
"""Read usage, reserve budget, and write handoffs. No model calls or auth scraping."""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
from pathlib import Path
import queue
import subprocess
import sys
import threading
import time
import uuid


DEFAULTS = {
    "max_age_seconds": 120,
    "safety_factor": 1.5,
    "percent_stop_floor": 10,
    "percent_handoff_reserve": 5,
    "unknown_policy": "checkpoint",
    "absolute_budgets": {},
}


def number(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value) if math.isfinite(value) else None


def read_json(path):
    return json.loads(Path(path).read_text(encoding="utf-8-sig"))


def atomic_text(path, text):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
    try:
        temporary.write_text(text, encoding="utf-8")
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()


def write_json(path, data):
    atomic_text(path, json.dumps(data, ensure_ascii=False, indent=2, allow_nan=False) + "\n")


def state_paths(root, session_id):
    if not isinstance(session_id, str) or not session_id.strip():
        raise ValueError("A unique session/task ID is required")
    key = hashlib.sha256(session_id.encode()).hexdigest()[:20]
    directory = Path(root).resolve() / ".usage-handoff" / key
    return {"directory": directory, "checkpoint": directory / "checkpoint.json",
            "snapshot": directory / "usage.json", "config": directory / "config.json"}


def unwrap(data):
    if not isinstance(data, dict):
        raise ValueError("Usage input must be a JSON object")
    if isinstance(data.get("structuredContent"), dict):
        return data["structuredContent"]
    if "content" in data:
        for block in data["content"]:
            if block.get("type") == "text":
                candidate = json.loads(block["text"])
                if isinstance(candidate, dict) and ("rateLimits" in candidate or "rateLimitsByLimitId" in candidate):
                    return candidate
        raise ValueError("No usage reading in the MCP result")
    return data.get("result", data)


def normalize(data, provider, sampled_at=None, limit_id="codex"):
    data = unwrap(data)
    observed = time.time() if sampled_at is None else sampled_at
    if provider == "manual":
        # Preserve the true sample time; never re-date a supplied snapshot.
        return {key: data.get(key) for key in
                ("provider", "source", "sampled_at", "windows", "exhausted")}
    windows = []
    exhausted = False

    def add(identifier, raw, used_key, reset_key):
        raw = raw if isinstance(raw, dict) else {}
        used = number(raw.get(used_key))
        windows.append({"id": identifier, "unit": "percent",
                        "remaining": None if used is None else max(0.0, min(100.0, 100 - used)),
                        "resets_at": raw.get(reset_key)})

    if provider == "codex":
        buckets = data.get("rateLimitsByLimitId")
        if isinstance(buckets, dict) and buckets:
            bucket = buckets.get(limit_id) or {}
        else:
            candidate = data.get("rateLimits") or {}
            bucket = candidate if candidate.get("limitId", limit_id) == limit_id else {}
        add(limit_id + ".primary", bucket.get("primary"), "usedPercent", "resetsAt")
        # A null secondary window is unavailable, not evidence of spare usage.
        add(limit_id + ".secondary", bucket.get("secondary"), "usedPercent", "resetsAt")
        exhausted = bucket.get("spendControlReached") is True or data.get("ordinaryUsageAllowed") is False
        source = "Codex account rate limits; bucket " + limit_id
    elif provider == "claude":
        limits = data.get("rate_limits") or {}
        for key in ("five_hour", "seven_day"):
            add(key, limits.get(key), "used_percentage", "resets_at")
        if "spend_limit" in limits:
            add("spend_limit", limits["spend_limit"], "used_percentage", "resets_at")
        source = "Claude Code official status-line rate_limits"
    else:
        raise ValueError("Unsupported usage provider")
    return {"provider": provider, "source": source, "sampled_at": observed,
            "windows": windows, "exhausted": exhausted}


def evaluate(snapshot, checkpoint=None, config=None, now=None):
    now = time.time() if now is None else now
    if not isinstance(snapshot, dict) or (checkpoint is not None and not isinstance(checkpoint, dict)):
        raise ValueError("Snapshot and checkpoint must be JSON objects")
    if config is not None and not isinstance(config, dict):
        raise ValueError("Configuration must be a JSON object")
    settings = {**DEFAULTS, **(config or {})}
    for key in ("max_age_seconds", "safety_factor", "percent_stop_floor", "percent_handoff_reserve"):
        value = number(settings.get(key))
        if (value is None or value < 0 or (key == "safety_factor" and value < 1)
                or (key == "percent_handoff_reserve" and value <= 0)):
            raise ValueError("Invalid configuration: " + key)
    if settings["unknown_policy"] not in ("checkpoint", "handoff"):
        raise ValueError("unknown_policy must be checkpoint or handoff")
    if not isinstance(settings["absolute_budgets"], dict):
        raise ValueError("absolute_budgets must be an object")
    issues, stop_reasons, assessed = [], [], []
    sampled = number(snapshot.get("sampled_at"))
    fresh = sampled is not None and -5 <= now - sampled <= settings["max_age_seconds"]
    if not fresh:
        issues.append("Usage sample is missing, stale, or future-dated; refresh it")
    if fresh and snapshot.get("exhausted") is True:
        stop_reasons.append("The usage source reports an exhausted or disabled budget")
    windows = snapshot.get("windows")
    if not isinstance(windows, list) or not windows:
        windows = []
        issues.append("No applicable usage windows were supplied")
    checkpoint = checkpoint or {}
    estimates = checkpoint.get("action_estimates") or {}
    if not isinstance(estimates, dict):
        raise ValueError("action_estimates must be an object")
    handoff_estimates = checkpoint.get("handoff_estimates", {})
    if not isinstance(handoff_estimates, dict):
        raise ValueError("handoff_estimates must be an object")
    saved_at = number(checkpoint.get("saved_at"))
    estimates_fresh = saved_at is not None and -5 <= now - saved_at <= settings["max_age_seconds"]
    seen = set()
    for window in windows:
        if not isinstance(window, dict):
            issues.append("Malformed usage window")
            continue
        identifier, unit = window.get("id"), window.get("unit")
        if not isinstance(identifier, str) or not identifier or identifier in seen:
            issues.append("Missing or duplicate window ID")
            continue
        seen.add(identifier)
        remaining = number(window.get("remaining"))
        reset = window.get("resets_at")
        if reset is not None and (number(reset) is None or reset <= now):
            issues.append(identifier + ": reset expired or invalid; refresh rather than assuming replenishment")
            continue
        if remaining is None or remaining < 0 or (unit == "percent" and remaining > 100):
            issues.append(identifier + ": remaining usage is unavailable or invalid")
            continue
        if unit == "percent":
            floor, reserve = settings["percent_stop_floor"], settings["percent_handoff_reserve"]
        elif unit in ("credits", "usd", "tokens"):
            budget = settings["absolute_budgets"].get(identifier, {})
            if not isinstance(budget, dict):
                raise ValueError("Absolute budget must be an object: " + identifier)
            floor, reserve = number(budget.get("stop_floor")), number(budget.get("handoff_reserve"))
            if budget.get("unit") != unit or floor is None or reserve is None or floor < 0 or reserve <= 0:
                issues.append(identifier + ": matching positive absolute handoff reserve and nonnegative stop floor are required")
                continue
        else:
            issues.append(identifier + ": unsupported meter unit")
            continue
        estimate = number(estimates.get(identifier)) if estimates_fresh else None
        calibrated = estimate is not None and estimate >= 0
        handoff_estimate = None
        if identifier in handoff_estimates:
            handoff_estimate = number(handoff_estimates[identifier]) if estimates_fresh else None
            if handoff_estimate is None or handoff_estimate < 0:
                issues.append(identifier + ": handoff estimate is stale or invalid; refresh it before costly work")
                handoff_estimate = None
            else:
                reserve = max(reserve, settings["safety_factor"] * handoff_estimate)
        if not calibrated:
            issues.append(identifier + ": no fresh same-unit action estimate; only the handoff reserve and stop floor can be checked")
        buffered_action_cost = settings["safety_factor"] * estimate if calibrated else None
        required = max(floor, reserve + buffered_action_cost) if calibrated else max(floor, reserve)
        assessed.append({"id": identifier, "unit": unit, "remaining": remaining,
                         "required": required, "estimated_action_cost": estimate,
                         "buffered_action_cost": buffered_action_cost,
                         "estimated_handoff_cost": handoff_estimate, "handoff_reserve": reserve,
                         "resets_at": reset})
        if fresh and remaining <= required:
            stop_reasons.append(f"{identifier}: {remaining:g} {unit} remaining <= {required:g} reserved/required")
    decision = "handoff" if stop_reasons else "checkpoint" if issues else "continue"
    if decision == "checkpoint" and settings["unknown_policy"] == "handoff":
        decision = "handoff"
        stop_reasons.append("Strict monitoring: fresh calibrated usage is unavailable")
    return {"decision": decision, "reasons": stop_reasons, "uncertainties": issues,
            "assessed_windows": assessed, "sampled_at": sampled, "source": snapshot.get("source")}


def format_time(value):
    value = number(value)
    if value is None:
        return "Unavailable"
    try:
        return time.strftime("%Y-%m-%d %H:%M:%S UTC", time.gmtime(value))
    except (ValueError, OverflowError, OSError):
        return "Invalid timestamp"


def handoff_text(checkpoint, snapshot, decision, reason, pending_tool=None):
    lines = ["# Usage handoff", "", "Status: incomplete; stopped for usage protection.", "",
             "Created: " + format_time(time.time()),
             "Checkpoint saved: " + format_time(checkpoint.get("saved_at")), "",
             "Stop reason: " + reason]
    if pending_tool:
        lines += ["", "Tool withheld before execution: " + str(pending_tool)]
    sections = [("Task and completion criteria", "task"), ("Constraints", "constraints"),
                ("Workspace", "workspace"), ("Branch / worktree", "branch"),
                ("Completed work", "completed"), ("Artifacts / changed files", "artifacts"),
                ("Current partial or planned action", "current_action"), ("Decisions", "decisions"),
                ("Verification actually performed", "verification"), ("Running jobs / external effects", "running_jobs"),
                ("Pending approvals", "pending_approvals"), ("Ordered next steps", "next_steps"),
                ("Estimate basis", "estimate_basis"), ("Handoff estimate basis", "handoff_estimate_basis")]
    for title, key in sections:
        value = checkpoint.get(key)
        lines += ["", "## " + title, ""]
        if isinstance(value, list):
            lines += [f"{i + 1}. {item}" for i, item in enumerate(value)] if value else ["Not recorded; verify before resuming."]
        else:
            lines.append(str(value) if value else "Not recorded; verify before resuming.")
    lines += ["", "## Usage evidence", "", "Source: " + str(snapshot.get("source") or "Unavailable"),
              "Sample time: " + format_time(snapshot.get("sampled_at")),
              "Decision: " + decision["decision"]]
    for window in snapshot.get("windows") or []:
        if isinstance(window, dict):
            lines.append(f"- {window.get('id')}: {window.get('remaining')} {window.get('unit')} remaining; resets {format_time(window.get('resets_at'))}.")
    for window in decision.get("assessed_windows", []):
        lines.append(f"- {window['id']}: estimated action cost {window['estimated_action_cost']}; "
                     f"buffered action cost {window['buffered_action_cost']}; "
                     f"estimated handoff cost {window['estimated_handoff_cost']}; "
                     f"protected handoff reserve {window['handoff_reserve']:g}; "
                     f"total required {window['required']:g} {window['unit']} (heuristic).")
    lines += ["- " + item for item in decision.get("uncertainties", [])]
    lines += ["", "## Resume prompt", "", str(checkpoint.get("resume_prompt") or
              "Read this handoff, confirm the original task and workspace, inspect running jobs and pending effects, obtain fresh usage, and continue from the next steps. Treat unrecorded information as unknown."), ""]
    return "\n".join(lines)


def create_handoff(paths, checkpoint, snapshot, decision, reason, pending_tool=None):
    filename = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime()) + "-" + uuid.uuid4().hex[:8] + ".md"
    output = paths["directory"] / "handoffs" / filename
    text = handoff_text(checkpoint, snapshot, decision, reason, pending_tool)
    atomic_text(output, text)
    if output.read_text(encoding="utf-8") != text:
        raise OSError("Handoff read-back failed")
    return output


def load_optional(path, fallback):
    return read_json(path) if Path(path).exists() else fallback


def hook(data):
    paths = state_paths(data["cwd"], data["session_id"])
    event = data["hook_event_name"]
    if event == "UserPromptSubmit":
        if not paths["checkpoint"].exists():
            write_json(paths["checkpoint"], {"saved_at": time.time(), "workspace": data["cwd"],
                       "task": "Not recorded yet. Read the original conversation before resuming.",
                       "current_action": "Not recorded yet.", "next_steps": ["Confirm the original task, then inspect workspace state."],
                       "action_estimates": {}})
        skill = Path(__file__).resolve().parents[1] / "SKILL.md"
        context = (f"Apply usage-handoff during this task. Read {skill}. Keep a concise current checkpoint at "
                   f"{paths['checkpoint']}, including saved_at (Unix seconds), current_action, next_steps, and "
                   "action_estimates and optional handoff_estimates keyed to the usage windows. "
                   "Reserve enough usage to save and verify a handoff before starting another costly step. "
                   "Update the checkpoint after milestones and before long actions. "
                   f"Usage snapshot: {paths['snapshot']}. Unknown usage means checkpoint, not unlimited budget. "
                   "If usage protection stops a tool, do not retry or bypass it; use the saved handoff.")
        return {"hookSpecificOutput": {"hookEventName": event, "additionalContext": context}}
    if event != "PreToolUse":
        return {}
    snapshot = load_optional(paths["snapshot"], {})
    checkpoint = load_optional(paths["checkpoint"], {})
    config = load_optional(paths["config"], {})
    decision = evaluate(snapshot, checkpoint, config)
    if decision["decision"] == "handoff":
        reason = "; ".join(decision["reasons"])
        # Repeated calls after the same stop reuse a verified artifact.
        fingerprint = hashlib.sha256(json.dumps([snapshot, checkpoint, reason], sort_keys=True).encode()).hexdigest()
        marker = paths["directory"] / "last-stop.json"
        previous = load_optional(marker, {})
        output = Path(previous.get("handoff", ""))
        expected_parent = paths["directory"] / "handoffs"
        if (previous.get("fingerprint") != fingerprint or not output.is_file()
                or output.resolve().parent != expected_parent.resolve()):
            output = create_handoff(paths, checkpoint, snapshot, decision, reason, data.get("tool_name"))
            write_json(marker, {"fingerprint": fingerprint, "handoff": str(output)})
        return {"continue": False, "stopReason": f"Usage protection stopped before the tool ran. {reason}. Handoff saved: {output}"}
    if decision["decision"] == "checkpoint":
        context = "Usage guard cannot forecast safely: " + "; ".join(decision["uncertainties"])
        context += ". Use only short usage checks and checkpoint writes until a fresh forecast protects the handoff reserve. Do not start costly task work on this result."
        return {"hookSpecificOutput": {"hookEventName": event, "additionalContext": context}}
    return {}


def statusline(data, delegate=None):
    root = (data.get("workspace") or {}).get("current_dir") or data.get("cwd")
    snapshot = normalize(data, "claude")
    paths = state_paths(root, data["session_id"])
    write_json(paths["snapshot"], snapshot)
    if delegate:
        configured = read_json(delegate)
        command = configured.get("command")
        if command:
            # This is the pre-existing user-configured command, never incoming tool data.
            result = subprocess.run(command, input=json.dumps(data), text=True, shell=True,
                                    capture_output=True, timeout=5, encoding="utf-8", errors="replace",
                                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            if result.returncode == 0 and result.stdout.strip():
                return result.stdout.rstrip()
    parts = [f"{item['id']}: {item['remaining']:g}% left" for item in snapshot["windows"] if item["remaining"] is not None]
    return "Usage handoff | " + (" | ".join(parts) if parts else "usage unknown; checkpoint required")


def read_codex(executable="codex", timeout=15, limit_id="codex"):
    process = subprocess.Popen([executable, "app-server"], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                               stderr=subprocess.DEVNULL, text=True, encoding="utf-8", errors="replace",
                               creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    messages = queue.Queue()

    def reader():
        for line in process.stdout:
            try:
                messages.put(json.loads(line))
            except json.JSONDecodeError:
                continue
        messages.put(None)

    thread = threading.Thread(target=reader, daemon=True)
    thread.start()
    deadline = time.monotonic() + timeout

    def send(message):
        process.stdin.write(json.dumps(message) + "\n")
        process.stdin.flush()

    def receive(identifier):
        while True:
            left = deadline - time.monotonic()
            if left <= 0:
                raise TimeoutError("Codex usage read timed out")
            try:
                message = messages.get(timeout=left)
            except queue.Empty:
                raise TimeoutError("Codex usage read timed out") from None
            if message is None:
                raise OSError("Codex app-server closed before returning usage")
            if message.get("id") == identifier:
                if "error" in message:
                    raise OSError("Codex usage read rejected; verify CLI authentication and account")
                return message.get("result") or {}

    try:
        send({"method": "initialize", "id": 0,
              "params": {"clientInfo": {"name": "usage_handoff", "version": "1.0.0"}}})
        receive(0)
        send({"method": "initialized", "params": {}})
        send({"method": "account/rateLimits/read", "id": 1})
        return normalize(receive(1), "codex", limit_id=limit_id)
    finally:
        process.stdin.close()
        try:
            process.wait(timeout=2)
        except subprocess.TimeoutExpired:
            process.terminate()
            try:
                process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=2)
        thread.join(timeout=1)
        process.stdout.close()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    normalize_parser = sub.add_parser("normalize")
    normalize_parser.add_argument("--provider", choices=("codex", "claude", "manual"), required=True)
    normalize_parser.add_argument("--input")
    normalize_parser.add_argument("--output")
    normalize_parser.add_argument("--sampled-at", type=float)
    normalize_parser.add_argument("--limit-id", default="codex")
    check_parser = sub.add_parser("check")
    check_parser.add_argument("--snapshot", required=True)
    check_parser.add_argument("--checkpoint")
    check_parser.add_argument("--config")
    for name in ("paths", "handoff"):
        item = sub.add_parser(name)
        item.add_argument("--root", required=True)
        item.add_argument("--session-id", required=True)
        if name == "handoff":
            item.add_argument("--reason", required=True)
    sub.add_parser("claude-hook")
    item = sub.add_parser("claude-statusline")
    item.add_argument("--delegate")
    item = sub.add_parser("read-codex")
    item.add_argument("--executable", default="codex")
    item.add_argument("--limit-id", default="codex")
    item.add_argument("--output")
    args = parser.parse_args(argv)
    try:
        if args.command == "normalize":
            data = read_json(args.input) if args.input else json.load(sys.stdin)
            result = normalize(data, args.provider, args.sampled_at, args.limit_id)
        elif args.command == "check":
            result = evaluate(read_json(args.snapshot), read_json(args.checkpoint) if args.checkpoint else {},
                              read_json(args.config) if args.config else {})
            print(json.dumps(result, ensure_ascii=False))
            return {"continue": 0, "handoff": 10, "checkpoint": 20}[result["decision"]]
        elif args.command in ("paths", "handoff"):
            paths = state_paths(args.root, args.session_id)
            if args.command == "paths":
                result = {key: str(value) for key, value in paths.items()}
            else:
                checkpoint = load_optional(paths["checkpoint"], {})
                snapshot = load_optional(paths["snapshot"], {})
                decision = evaluate(snapshot, checkpoint, load_optional(paths["config"], {}))
                result = {"handoff": str(create_handoff(paths, checkpoint, snapshot, decision, args.reason))}
        elif args.command == "claude-hook":
            result = hook(json.load(sys.stdin))
        elif args.command == "claude-statusline":
            print(statusline(json.load(sys.stdin), args.delegate))
            return 0
        else:
            try:
                result = read_codex(args.executable, limit_id=args.limit_id)
            except (OSError, TimeoutError) as error:
                result = {"provider": "codex", "source": "Codex CLI usage unavailable: " + str(error),
                          "sampled_at": None, "windows": [], "exhausted": False}
        if getattr(args, "output", None):
            write_json(args.output, result)
        print(json.dumps(result, ensure_ascii=False, allow_nan=False))
        return 0
    except (ValueError, TypeError, KeyError, AttributeError, OSError, subprocess.SubprocessError) as error:
        if args.command == "claude-hook":
            # Stop visibly if the guard or handoff persistence failed.
            print(json.dumps({"continue": False, "stopReason": "Usage guard failed; a handoff may not have been saved: " + str(error)}))
            return 0
        if args.command == "claude-statusline":
            print("Usage handoff | capture failed; usage unknown")
            return 0
        print(json.dumps({"error": str(error)}))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
