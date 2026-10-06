#!/usr/bin/env python3
"""Install Usage Handoff for local Codex and Claude Code; build the desktop ZIP."""
import argparse
import json
import os
from pathlib import Path
import shutil
import sys
import time
import uuid
import zipfile

START = "<!-- usage-handoff:start -->"
END = "<!-- usage-handoff:end -->"


def backup(path):
    if path.exists():
        target = path.with_name(path.name + ".usage-handoff-" + time.strftime("%Y%m%dT%H%M%S") + "-" + uuid.uuid4().hex[:6] + ".bak")
        shutil.copy2(path, target)
        return str(target)
    return None


def write_atomic(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
    try:
        temporary.write_text(text, encoding="utf-8")
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()


def update_instructions(path, text):
    existing = path.read_text(encoding="utf-8-sig") if path.exists() else ""
    block = START + "\n" + text + "\n" + END
    if START in existing:
        start = existing.index(START)
        end = existing.find(END, start)
        if end == -1:
            raise ValueError("Unclosed usage-handoff instruction block in " + str(path))
        result = existing[:start] + block + existing[end + len(END):]
    else:
        result = existing + ("\n\n" if existing and not existing.endswith("\n\n") else "") + block + "\n"
    if result != existing:
        backup(path)
        write_atomic(path, result)


def copy_skill(source, target):
    source, target = source.resolve(), target.resolve()
    if source == target or source in target.parents or target in source.parents:
        raise ValueError("Skill source and installation target must be separate directories")
    if target.is_symlink():
        raise ValueError("Refusing to replace a symlinked skill; choose a different install location")
    if target.exists():
        existing = target / "SKILL.md"
        if not existing.exists() or "# Usage handoff" not in existing.read_text(encoding="utf-8"):
            raise ValueError("A different skill already exists at " + str(target))
    shutil.copytree(source, target, dirs_exist_ok=True,
                    ignore=shutil.ignore_patterns("__pycache__", "*.pyc", "statusline-delegate.json"))


def quote_command(executable, script, arguments):
    # Claude uses a shell; reject interpolation characters rather than assuming JSON quoting is shell quoting.
    for path in (str(executable), str(script)):
        if any(character in path for character in ('"', "`", "$", "\n", "\r", "%")):
            raise ValueError("Unsupported shell metacharacter in install path")
    return '"' + str(executable) + '" "' + str(script) + '" ' + arguments


def install(source, user_home, python_executable, codex_home=None):
    user_home = user_home.resolve()
    codex_home = Path(codex_home).resolve() if codex_home else user_home / ".codex"
    codex_skill = user_home / ".agents" / "skills" / "usage-handoff"
    claude_skill = user_home / ".claude" / "skills" / "usage-handoff"
    settings_path = user_home / ".claude" / "settings.json"
    settings = json.loads(settings_path.read_text(encoding="utf-8-sig")) if settings_path.exists() else {}
    if not isinstance(settings, dict):
        raise ValueError("Claude settings must be a JSON object")
    hooks = settings.get("hooks", {})
    if not isinstance(hooks, dict):
        raise ValueError("Claude hooks must be an object")
    for event in ("UserPromptSubmit", "PreToolUse"):
        if not isinstance(hooks.get(event, []), list):
            raise ValueError("Claude hook entries must be arrays: " + event)
    prior_status = settings.get("statusLine")
    if prior_status is not None and (not isinstance(prior_status, dict) or prior_status.get("type") != "command"):
        raise ValueError("Existing statusLine is not a delegatable command; inspect it before installing")
    # Preflight shell paths before making any changes.
    script = claude_skill / "scripts" / "guard.py"
    hook_command = quote_command(python_executable, script, "claude-hook")
    delegate_path = claude_skill / "agents" / "statusline-delegate.json"
    status_command = quote_command(python_executable, script, "claude-statusline")
    owns_status = isinstance(prior_status, dict) and "guard.py" in prior_status.get("command", "") and "claude-statusline" in prior_status.get("command", "")
    if (prior_status and not owns_status) or (owns_status and delegate_path.exists()):
        delegate_argument = quote_command(python_executable, delegate_path, "")  # validates the path
        del delegate_argument
        status_command += ' --delegate "' + str(delegate_path) + '"'
    copy_skill(source, codex_skill)
    copy_skill(source, claude_skill)
    if prior_status and not owns_status:
        write_atomic(delegate_path, json.dumps(prior_status, indent=2) + "\n")
    elif not owns_status and delegate_path.exists():
        # A previous wrapper has been removed; don't resurrect an old user command.
        status_command = quote_command(python_executable, script, "claude-statusline")
    settings["statusLine"] = {**(prior_status or {}), "type": "command", "command": status_command}
    for event in ("UserPromptSubmit", "PreToolUse"):
        entries = hooks.setdefault(event, [])
        already = any(any(item.get("command") == hook_command for item in entry.get("hooks", [])) for entry in entries)
        if not already:
            entries.append({"matcher": "" if event == "UserPromptSubmit" else ".*",
                            "hooks": [{"type": "command", "command": hook_command, "timeout": 10}]})
    settings["hooks"] = hooks
    new_text = json.dumps(settings, ensure_ascii=False, indent=2) + "\n"
    existing_text = settings_path.read_text(encoding="utf-8") if settings_path.exists() else ""
    settings_backup = None
    if new_text != existing_text:
        settings_backup = backup(settings_path)
        write_atomic(settings_path, new_text)
    update_instructions(codex_home / "AGENTS.md",
        f"Before substantial task work, load and apply the usage-handoff skill at {codex_skill / 'SKILL.md'}. "
        "Keep a current task checkpoint, check fresh account usage before long actions and after milestones, "
        "and reserve enough usage to save and verify a handoff before starting another costly step. Task work cannot spend that reserve. "
        "Stop with a saved handoff when the budget rule triggers. Prefer the app's get_usage_limits tool; "
        "use the skill's read-only CLI adapter only when the account and host match. If usage cannot be read, "
        "state that limitation, use only short usage checks and checkpoint writes, and save a handoff before another costly step. A handoff does not authorize a new chat or external messages.")
    update_instructions(user_home / ".claude" / "CLAUDE.md",
        f"Apply the usage-handoff skill at {claude_skill / 'SKILL.md'} throughout substantial work. "
        "Keep the checkpoint path supplied by the usage-handoff prompt hook current. If a usage hook stops a tool, "
        "do not retry or bypass it: use the saved handoff. Reserve enough usage to save and verify a handoff before another costly step. "
        "Unknown usage permits only short usage checks and checkpoint writes; save a handoff if a safe forecast remains unavailable.")
    return {"codex_skill": str(codex_skill), "claude_code_skill": str(claude_skill),
            "claude_settings": str(settings_path), "settings_backup": settings_backup,
            "codex_instructions": str(codex_home / "AGENTS.md")}


def package(source, output):
    output.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as archive:
        for file in sorted(source.rglob("*")):
            if (file.is_file() and "__pycache__" not in file.parts and file.suffix != ".pyc"
                    and file.name != "statusline-delegate.json"):
                archive.write(file, (Path("usage-handoff") / file.relative_to(source)).as_posix())
    with zipfile.ZipFile(output) as archive:
        if archive.testzip() is not None or "usage-handoff/SKILL.md" not in archive.namelist():
            raise OSError("Skill ZIP verification failed")
    return str(output.resolve())


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, default=Path(__file__).resolve().parent / "usage-handoff")
    parser.add_argument("--home", type=Path, default=Path.home())
    parser.add_argument("--python", type=Path, default=Path(sys.executable))
    parser.add_argument("--codex-home", type=Path)
    parser.add_argument("--package-only", action="store_true")
    parser.add_argument("--output", type=Path, default=Path(__file__).resolve().parent / "dist" / "usage-handoff.zip")
    args = parser.parse_args()
    result = {}
    if not args.package_only:
        codex_home = args.codex_home or (Path(os.environ["CODEX_HOME"]) if os.environ.get("CODEX_HOME") and args.home == Path.home() else None)
        result = install(args.source, args.home, args.python, codex_home)
    result["claude_desktop_zip"] = package(args.source, args.output)
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
