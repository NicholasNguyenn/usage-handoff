import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock
import zipfile

ROOT = Path(__file__).resolve().parents[1]


def module(name, file):
    spec = importlib.util.spec_from_file_location(name, file)
    result = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(result)
    return result


guard = module("guard", ROOT / "usage-handoff" / "scripts" / "guard.py")
installer = module("installer", ROOT / "install.py")


class BudgetTests(unittest.TestCase):
    def snapshot(self, remaining=60, now=1000):
        return {"sampled_at": now, "source": "test", "windows": [
            {"id": "five_hour", "unit": "percent", "remaining": remaining, "resets_at": now + 5000}]}

    def checkpoint(self, estimate=2, now=1000):
        return {"saved_at": now, "action_estimates": {"five_hour": estimate}}

    def test_fresh_calibrated_action_fits(self):
        result = guard.evaluate(self.snapshot(), self.checkpoint(), now=1000)
        self.assertEqual(result["decision"], "continue")

    def test_forecast_stops_before_floor(self):
        result = guard.evaluate(self.snapshot(30), self.checkpoint(20), now=1000)
        self.assertEqual(result["decision"], "handoff")
        self.assertEqual(result["assessed_windows"][0]["required"], 35)

    def test_equal_threshold_stops(self):
        self.assertEqual(guard.evaluate(self.snapshot(10), self.checkpoint(), now=1000)["decision"], "handoff")

    def test_strictest_window_wins(self):
        data = self.snapshot()
        data["windows"].append({"id": "weekly", "unit": "percent", "remaining": 4})
        result = guard.evaluate(data, self.checkpoint(), now=1000)
        self.assertEqual(result["decision"], "handoff")

    def test_stale_reading_is_unknown_not_spare(self):
        self.assertEqual(guard.evaluate(self.snapshot(5, 800), self.checkpoint(), now=1000)["decision"], "checkpoint")

    def test_expired_reset_is_unknown(self):
        data = self.snapshot(5)
        data["windows"][0]["resets_at"] = 999
        self.assertEqual(guard.evaluate(data, self.checkpoint(), now=1000)["decision"], "checkpoint")

    def test_missing_data_and_strict_mode(self):
        self.assertEqual(guard.evaluate({}, now=1000)["decision"], "checkpoint")
        self.assertEqual(guard.evaluate({}, config={"unknown_policy": "handoff"}, now=1000)["decision"], "handoff")

    def test_uncalibrated_healthy_floor_does_not_certify_fit(self):
        self.assertEqual(guard.evaluate(self.snapshot(), now=1000)["decision"], "checkpoint")
        self.assertEqual(guard.evaluate(self.snapshot(5), now=1000)["decision"], "handoff")

    def test_action_that_fits_cannot_borrow_handoff_reserve(self):
        # The buffered action costs 9 points and fits in 13, but would consume the reserve.
        result = guard.evaluate(self.snapshot(13), self.checkpoint(6), now=1000)
        self.assertEqual(result["decision"], "handoff")
        window = result["assessed_windows"][0]
        self.assertEqual(window["buffered_action_cost"], 9)
        self.assertEqual(window["handoff_reserve"], 5)
        self.assertEqual(window["required"], 14)

    def test_larger_handoff_estimate_raises_reserve_and_boundary_stops(self):
        checkpoint = {**self.checkpoint(6), "handoff_estimates": {"five_hour": 8}}
        result = guard.evaluate(self.snapshot(21), checkpoint, now=1000)
        self.assertEqual(result["decision"], "handoff")
        self.assertEqual(result["assessed_windows"][0]["handoff_reserve"], 12)
        result = guard.evaluate(self.snapshot(21.01), checkpoint, now=1000)
        self.assertEqual(result["decision"], "continue")
        window = result["assessed_windows"][0]
        self.assertGreater(window["remaining"] - window["buffered_action_cost"], window["handoff_reserve"])

    def test_small_handoff_estimate_cannot_reduce_configured_reserve(self):
        checkpoint = {**self.checkpoint(6), "handoff_estimates": {"five_hour": 1}}
        result = guard.evaluate(self.snapshot(), checkpoint, now=1000)
        self.assertEqual(result["assessed_windows"][0]["handoff_reserve"], 5)

    def test_uncalibrated_action_still_protects_handoff_reserve(self):
        checkpoint = {"saved_at": 1000, "handoff_estimates": {"five_hour": 20}}
        result = guard.evaluate(self.snapshot(25), checkpoint, now=1000)
        self.assertEqual(result["decision"], "handoff")
        self.assertEqual(result["assessed_windows"][0]["required"], 30)

    def test_invalid_or_stale_handoff_estimate_never_certifies_action(self):
        for estimate in (None, True, "8", -1, float("nan")):
            with self.subTest(estimate=estimate):
                checkpoint = {**self.checkpoint(), "handoff_estimates": {"five_hour": estimate}}
                self.assertEqual(guard.evaluate(self.snapshot(), checkpoint, now=1000)["decision"], "checkpoint")
        checkpoint = {**self.checkpoint(now=800), "handoff_estimates": {"five_hour": 8}}
        result = guard.evaluate(self.snapshot(), checkpoint, now=1000)
        self.assertEqual(result["decision"], "checkpoint")
        self.assertIsNone(result["assessed_windows"][0]["estimated_handoff_cost"])

    def test_stale_estimate_not_reused(self):
        self.assertEqual(guard.evaluate(self.snapshot(), self.checkpoint(now=800), now=1000)["decision"], "checkpoint")

    def test_known_low_limit_wins_over_missing_other_window(self):
        data = self.snapshot(3)
        data["windows"].append({"id": "weekly", "unit": "percent", "remaining": None})
        self.assertEqual(guard.evaluate(data, now=1000)["decision"], "handoff")

    def test_absolute_budgets_require_unit_matching(self):
        data = {"sampled_at": 1000, "windows": [{"id": "prepaid", "unit": "credits", "remaining": 20}]}
        checkpoint = {"saved_at": 1000, "action_estimates": {"prepaid": 12}}
        config = {"absolute_budgets": {"prepaid": {"unit": "credits", "stop_floor": 5, "handoff_reserve": 5}}}
        self.assertEqual(guard.evaluate(data, checkpoint, now=1000)["decision"], "checkpoint")
        self.assertEqual(guard.evaluate(data, checkpoint, config, now=1000)["decision"], "handoff")
        config["absolute_budgets"]["prepaid"]["unit"] = "usd"
        self.assertEqual(guard.evaluate(data, checkpoint, config, now=1000)["decision"], "checkpoint")

    def test_absolute_budget_protects_separate_handoff_cost(self):
        data = {"sampled_at": 1000, "windows": [{"id": "prepaid", "unit": "credits", "remaining": 17}]}
        checkpoint = {"saved_at": 1000, "action_estimates": {"prepaid": 6}, "handoff_estimates": {"prepaid": 6}}
        config = {"absolute_budgets": {"prepaid": {"unit": "credits", "stop_floor": 5, "handoff_reserve": 2}}}
        result = guard.evaluate(data, checkpoint, config, now=1000)
        self.assertEqual(result["decision"], "handoff")
        self.assertEqual(result["assessed_windows"][0]["required"], 18)
        config["absolute_budgets"]["prepaid"]["handoff_reserve"] = 0
        self.assertEqual(guard.evaluate(data, checkpoint, config, now=1000)["decision"], "checkpoint")

    def test_invalid_numbers_never_become_healthy(self):
        for value in (None, True, "95", float("nan"), float("inf"), -1, 101):
            with self.subTest(value=value):
                self.assertEqual(guard.evaluate(self.snapshot(value), self.checkpoint(), now=1000)["decision"], "checkpoint")

    def test_missing_known_windows_retained(self):
        result = guard.normalize({"rate_limits": {"five_hour": {"used_percentage": 99}}}, "claude", 1000)
        self.assertEqual(result["windows"][0]["remaining"], 1)
        self.assertIsNone(result["windows"][1]["remaining"])

    def test_codex_uses_active_bucket_not_review_or_legacy(self):
        raw = {"accountId": "not-to-be-saved", "rateLimits": {"primary": {"usedPercent": 100}},
               "rateLimitsByLimitId": {"codex": {"primary": {"usedPercent": 25}},
                                      "codex_review": {"primary": {"usedPercent": 100}}}}
        result = guard.normalize(raw, "codex", 1000)
        self.assertEqual(result["windows"][0]["remaining"], 75)
        self.assertNotIn("accountId", json.dumps(result))
        self.assertEqual(guard.normalize(raw, "codex", 1000, "codex_review")["windows"][0]["remaining"], 0)

    def test_manual_snapshot_preserves_original_time(self):
        data = self.snapshot(now=800)
        result = guard.normalize(data, "manual", 1000)
        self.assertEqual(result["sampled_at"], 800)

    def test_fresh_exhaustion_flag_stops(self):
        data = self.snapshot()
        data["exhausted"] = True
        self.assertEqual(guard.evaluate(data, self.checkpoint(), now=1000)["decision"], "handoff")

    def test_invalid_configuration_rejected(self):
        for config in ({"safety_factor": 0.5}, {"percent_stop_floor": -1}, {"percent_handoff_reserve": 0}, {"absolute_budgets": []}):
            with self.assertRaises(ValueError):
                guard.evaluate(self.snapshot(), self.checkpoint(), config, now=1000)


class HookTests(unittest.TestCase):
    def test_handoff_reserve_stops_tool_and_saves_cost_evidence(self):
        with tempfile.TemporaryDirectory() as temporary:
            paths = guard.state_paths(temporary, "reserve-test")
            now = time.time()
            guard.write_json(paths["checkpoint"], {"saved_at": now, "task": "Run the large build",
                "current_action": "Launch the build", "action_estimates": {"five_hour": 6, "seven_day": 1},
                "handoff_estimates": {"five_hour": 8}, "handoff_estimate_basis": "Comparable save and delivery"})
            guard.statusline({"cwd": temporary, "session_id": "reserve-test", "rate_limits": {
                "five_hour": {"used_percentage": 80, "resets_at": now + 300},
                "seven_day": {"used_percentage": 30, "resets_at": now + 1000}}})
            result = guard.hook({"cwd": temporary, "session_id": "reserve-test", "hook_event_name": "PreToolUse", "tool_name": "Bash"})
            self.assertIs(result["continue"], False)
            output = next((paths["directory"] / "handoffs").glob("*.md"))
            text = output.read_text(encoding="utf-8")
            self.assertIn("protected handoff reserve 12", text)
            self.assertIn("Comparable save and delivery", text)
            self.assertIn("Tool withheld before execution: Bash", text)

    def test_stop_saves_checkpoint_and_preserves_user_work(self):
        with tempfile.TemporaryDirectory() as temporary:
            workspace = Path(temporary)
            work = workspace / "user-work.txt"
            work.write_text("uncommitted work")
            payload = {"cwd": temporary, "session_id": "session-A", "hook_event_name": "UserPromptSubmit"}
            activation = guard.hook(payload)
            paths = guard.state_paths(temporary, "session-A")
            self.assertIn(str(paths["checkpoint"]), activation["hookSpecificOutput"]["additionalContext"])
            guard.write_json(paths["checkpoint"], {"saved_at": time.time(), "task": "Fix the parser",
                "current_action": "Run parser tests", "completed": ["Patched parser.py"],
                "verification": ["Syntax check passed"], "running_jobs": ["build session 22 may still be running"],
                "next_steps": ["Inspect session 22", "Run parser tests"], "action_estimates": {}})
            guard.statusline({"cwd": temporary, "session_id": "session-A", "rate_limits": {
                "five_hour": {"used_percentage": 99, "resets_at": time.time() + 300},
                "seven_day": {"used_percentage": 30, "resets_at": time.time() + 1000}}})
            payload.update(hook_event_name="PreToolUse", tool_name="Bash", tool_input={"command": "must never execute"})
            result = guard.hook(payload)
            self.assertIs(result["continue"], False)
            outputs = list((paths["directory"] / "handoffs").glob("*.md"))
            self.assertEqual(len(outputs), 1)
            text = outputs[0].read_text(encoding="utf-8")
            self.assertIn("Fix the parser", text)
            self.assertIn("build session 22", text)
            self.assertNotIn("must never execute", text)
            self.assertEqual(work.read_text(), "uncommitted work")
            self.assertEqual(guard.hook(payload), result)
            self.assertEqual(len(list((paths["directory"] / "handoffs").glob("*.md"))), 1)

    def test_session_isolation_and_path_traversal(self):
        with tempfile.TemporaryDirectory() as temporary:
            first = guard.state_paths(temporary, "../../somewhere")
            second = guard.state_paths(temporary, "another-session")
            self.assertNotEqual(first["directory"], second["directory"])
            self.assertTrue(first["directory"].is_relative_to(Path(temporary).resolve()))

    def test_unknown_usage_informs_instead_of_claiming_enforcement(self):
        with tempfile.TemporaryDirectory() as temporary:
            result = guard.hook({"cwd": temporary, "session_id": "test", "hook_event_name": "PreToolUse"})
            self.assertNotIn("continue", result)
            self.assertIn("cannot forecast", result["hookSpecificOutput"]["additionalContext"])

    def test_bad_checkpoint_stops_visibly_in_cli(self):
        with tempfile.TemporaryDirectory() as temporary:
            paths = guard.state_paths(temporary, "test")
            guard.write_json(paths["checkpoint"], ["bad-shape"])
            payload = {"cwd": temporary, "session_id": "test", "hook_event_name": "PreToolUse"}
            result = subprocess.run([sys.executable, str(ROOT / "usage-handoff/scripts/guard.py"), "claude-hook"],
                                    input=json.dumps(payload), capture_output=True, text=True)
            self.assertEqual(result.returncode, 0)
            self.assertIs(json.loads(result.stdout)["continue"], False)

    def test_check_cli_exit_codes_are_decision_signals(self):
        with tempfile.TemporaryDirectory() as temporary:
            snapshot = Path(temporary) / "usage.json"
            guard.write_json(snapshot, {})
            result = subprocess.run([sys.executable, str(ROOT / "usage-handoff/scripts/guard.py"), "check", "--snapshot", str(snapshot)],
                                    capture_output=True, text=True)
            self.assertEqual(result.returncode, 20)
            self.assertEqual(json.loads(result.stdout)["decision"], "checkpoint")


class InstallTests(unittest.TestCase):
    def test_preserves_settings_instructions_and_is_idempotent(self):
        with tempfile.TemporaryDirectory() as temporary:
            user_home = Path(temporary)
            settings_path = user_home / ".claude/settings.json"
            settings_path.parent.mkdir()
            prior = {"env": {"UNCHANGED": "yes"}, "statusLine": {"type": "command", "command": "echo existing-display"},
                     "hooks": {"PreToolUse": [{"matcher": "Read", "hooks": [{"type": "command", "command": "echo existing-hook"}]}]}}
            guard.write_json(settings_path, prior)
            claude_instructions = user_home / ".claude/CLAUDE.md"
            claude_instructions.write_text("Keep existing instruction.\n")
            first = installer.install(ROOT / "usage-handoff", user_home, Path(sys.executable))
            installer.install(ROOT / "usage-handoff", user_home, Path(sys.executable))
            settings = guard.read_json(settings_path)
            self.assertEqual(settings["env"], prior["env"])
            self.assertEqual(settings["hooks"]["PreToolUse"][0], prior["hooks"]["PreToolUse"][0])
            self.assertEqual(len(settings["hooks"]["PreToolUse"]), 2)
            self.assertEqual(len(settings["hooks"]["UserPromptSubmit"]), 1)
            delegate = user_home / ".claude/skills/usage-handoff/agents/statusline-delegate.json"
            self.assertEqual(guard.read_json(delegate), prior["statusLine"])
            instructions = claude_instructions.read_text()
            self.assertIn("Keep existing instruction.", instructions)
            self.assertEqual(instructions.count(installer.START), 1)
            self.assertTrue(Path(first["settings_backup"]).is_file())
            self.assertTrue((user_home / ".agents/skills/usage-handoff/SKILL.md").is_file())

    def test_zip_has_one_skill_root_and_no_local_data(self):
        with tempfile.TemporaryDirectory() as temporary:
            target = Path(temporary) / "usage-handoff.zip"
            installer.package(ROOT / "usage-handoff", target)
            with zipfile.ZipFile(target) as archive:
                names = archive.namelist()
                self.assertIn("usage-handoff/SKILL.md", names)
                self.assertTrue(all(name.startswith("usage-handoff/") for name in names))
                self.assertFalse(any("__pycache__" in name or "statusline-delegate" in name for name in names))

    def test_existing_statusline_receives_original_payload(self):
        with tempfile.TemporaryDirectory() as temporary:
            delegate = Path(temporary) / "delegate.json"
            script = Path(temporary) / "display.py"
            script.write_text("import json,sys\ndata=json.load(sys.stdin)\nprint(data['model']['display_name'])\n")
            guard.write_json(delegate, {"command": installer.quote_command(sys.executable, script, "")})
            data = {"cwd": temporary, "session_id": "test", "model": {"display_name": "Existing display"}, "rate_limits": {}}
            self.assertEqual(guard.statusline(data, delegate), "Existing display")


class CodexProtocolTests(unittest.TestCase):
    def test_read_only_handshake_and_usage_normalization(self):
        with tempfile.TemporaryDirectory() as temporary:
            script = Path(temporary) / "fake_server.py"
            log = Path(temporary) / "requests.jsonl"
            script.write_text("""import json,sys,time
log=open(sys.argv[1], 'w')
for line in sys.stdin:
    request=json.loads(line)
    log.write(json.dumps(request)+'\\n'); log.flush()
    method=request['method']
    if method=='initialize':
        print(json.dumps({'id':request['id'],'result':{'ok':True}}),flush=True)
    elif method=='account/rateLimits/read':
        result={'accountId':'private-id','rateLimitsByLimitId':{'codex':{
            'primary':{'usedPercent':20,'resetsAt':time.time()+300},
            'secondary':{'usedPercent':35,'resetsAt':time.time()+1000}}}}
        print(json.dumps({'id':request['id'],'result':result}),flush=True)
    elif method!='initialized':
        raise RuntimeError('Unexpected mutating request')
""", encoding="utf-8")
            original = subprocess.Popen

            def launch(arguments, **kwargs):
                self.assertEqual(arguments, ["fake-codex", "app-server"])
                return original([sys.executable, str(script), str(log)], **kwargs)

            with mock.patch.object(guard.subprocess, "Popen", side_effect=launch):
                result = guard.read_codex("fake-codex", timeout=3)
            methods = [json.loads(line)["method"] for line in log.read_text().splitlines()]
            self.assertEqual(methods, ["initialize", "initialized", "account/rateLimits/read"])
            self.assertEqual(result["windows"][0]["remaining"], 80)
            self.assertEqual(result["windows"][1]["remaining"], 65)
            self.assertNotIn("private-id", json.dumps(result))


if __name__ == "__main__":
    unittest.main()
