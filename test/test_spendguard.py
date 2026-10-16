import contextlib
import datetime as dt
import io
import json
import tempfile
import unittest
from pathlib import Path
from zoneinfo import ZoneInfo

from spendguard import adapters, cli
from spendguard.config import ConfigError, parse_config
from spendguard.core import (STARTER_PRICES, Price, Usage, cost_of, evaluate,
                             find_price, period_start, summarize)

IST = ZoneInfo("Asia/Kolkata")
UTC = dt.timezone.utc


def usage(ts="2026-10-15T10:00:00Z", model="claude-sonnet-4-6", project="app", **kw):
    return Usage(ts=adapters.parse_ts(ts), model=model, project=project, **kw)


class PricingTests(unittest.TestCase):
    def test_cost_math_uses_default_cache_multipliers(self):
        u = usage(input=1_000_000, output=1_000_000, cache_read=1_000_000, cache_write=1_000_000)
        cost, priced = cost_of(u, STARTER_PRICES)
        self.assertTrue(priced)
        self.assertAlmostEqual(cost, 3 + 15 + 0.3 + 3.75)

    def test_longest_pattern_wins(self):
        table = {"sonnet": Price(3, 15), "sonnet-4": Price(1, 2)}
        self.assertEqual(find_price("claude-sonnet-4-6", table)[0], "sonnet-4")

    def test_unknown_model_is_unpriced_not_free_silently(self):
        cost, priced = cost_of(usage(model="mystery-1", input=500), STARTER_PRICES)
        self.assertEqual((cost, priced), (0.0, False))
        rows = summarize([usage(model="mystery-1", input=500)], STARTER_PRICES, UTC, "day")
        self.assertEqual(list(rows.values())[0].unpriced_tokens, 500)

    def test_explicit_cost_wins(self):
        cost, priced = cost_of(usage(model="mystery-1", cost=1.25), STARTER_PRICES)
        self.assertEqual((cost, priced), (1.25, True))


class PeriodTests(unittest.TestCase):
    def test_weekly_starts_monday_in_local_tz(self):
        now = dt.datetime(2026, 10, 16, 7, 52, tzinfo=IST)  # a Friday
        self.assertEqual(period_start(now, "weekly"), dt.datetime(2026, 10, 12, tzinfo=IST))
        self.assertEqual(period_start(now, "monthly"), dt.datetime(2026, 10, 1, tzinfo=IST))
        self.assertEqual(period_start(now, "daily"), dt.datetime(2026, 10, 16, tzinfo=IST))

    def test_timezone_decides_which_day_a_call_belongs_to(self):
        u = usage("2026-10-15T20:00:00Z", input=1_000_000)  # 01:30 on the 16th in IST
        self.assertEqual(list(summarize([u], STARTER_PRICES, IST, "day")), ["2026-10-16"])
        self.assertEqual(list(summarize([u], STARTER_PRICES, UTC, "day")), ["2026-10-15"])


class EvaluateTests(unittest.TestCase):
    NOW = dt.datetime(2026, 10, 16, 12, 0, tzinfo=UTC)

    def run_eval(self, records, **kw):
        checks, unpriced = evaluate(records, STARTER_PRICES, kw.get("g", {}), kw.get("p", {}), 0.8, self.NOW)
        return checks, unpriced

    def test_ok_warn_over(self):
        # 1M output tokens of sonnet = $15
        rec = [usage(output=1_000_000)]
        self.assertEqual(self.run_eval(rec, g={"monthly": 100})[0][0].status, "ok")
        self.assertEqual(self.run_eval(rec, g={"monthly": 18})[0][0].status, "warn")
        self.assertEqual(self.run_eval(rec, g={"monthly": 15})[0][0].status, "over")

    def test_old_usage_is_outside_the_daily_window(self):
        rec = [usage("2026-10-10T10:00:00Z", output=1_000_000)]
        self.assertEqual(self.run_eval(rec, g={"daily": 1})[0][0].spent, 0.0)
        self.assertAlmostEqual(self.run_eval(rec, g={"monthly": 100})[0][0].spent, 15.0)

    def test_project_limits_only_count_that_project(self):
        rec = [usage(project="web", output=1_000_000), usage(project="api", output=1_000_000)]
        checks, _ = self.run_eval(rec, p={"web": {"monthly": 20}})
        self.assertEqual(len(checks), 1)
        self.assertAlmostEqual(checks[0].spent, 15.0)

    def test_future_usage_is_ignored(self):
        rec = [usage("2026-10-17T10:00:00Z", output=1_000_000)]
        self.assertEqual(self.run_eval(rec, g={"monthly": 100})[0][0].spent, 0.0)

    def test_reports_unpriced_models(self):
        _, unpriced = self.run_eval([usage(model="mystery-1", input=10)], g={"monthly": 5})
        self.assertEqual(unpriced, {"mystery-1"})


def claude_line(msg_id, out, ts="2026-10-15T10:00:00.000Z", model="claude-sonnet-4-6", req="req1", cwd="/work/shop"):
    return json.dumps({
        "type": "assistant", "timestamp": ts, "cwd": cwd, "requestId": req,
        "message": {"id": msg_id, "model": model, "role": "assistant", "content": [],
                    "usage": {"input_tokens": 100, "output_tokens": out,
                              "cache_read_input_tokens": 1000, "cache_creation_input_tokens": None}},
    })


class AdapterTests(unittest.TestCase):
    def test_claude_dedupes_streamed_duplicates_and_skips_noise(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "proj").mkdir()
            lines = [
                claude_line("m1", 10), claude_line("m1", 50), claude_line("m1", 50),
                claude_line("m2", 7, req="req2"),
                claude_line("m3", 5, model="<synthetic>"),
                json.dumps({"type": "user", "message": {"usage": {}}}),
                '{"usage": broken json',
            ]
            (root / "proj" / "s.jsonl").write_text("\n".join(lines), encoding="utf-8")
            got = sorted(adapters.read_claude([root]), key=lambda u: u.output)
        self.assertEqual([u.output for u in got], [7, 50])
        self.assertEqual(got[0].project, "shop")
        self.assertEqual(got[0].cache_write, 0)  # None handled

    def test_generic_reads_and_validates(self):
        with tempfile.TemporaryDirectory() as tmp:
            f = Path(tmp) / "u.jsonl"
            f.write_text('{"timestamp":"2026-10-15T01:00:00Z","model":"x","input_tokens":5,"cost_usd":0.5,"user":"asha"}\n\n')
            (rec,) = list(adapters.read_generic([f]))
            self.assertEqual((rec.input, rec.cost, rec.user), (5, 0.5, "asha"))
            f.write_text('{"model":"x"}\n')
            with self.assertRaisesRegex(ValueError, "u.jsonl:1"):
                list(adapters.read_generic([f]))

    def test_parse_ts_variants(self):
        self.assertEqual(adapters.parse_ts("2026-10-15T01:00:00Z"), dt.datetime(2026, 10, 15, 1, tzinfo=UTC))
        self.assertEqual(adapters.parse_ts("2026-10-15T01:00:00"), dt.datetime(2026, 10, 15, 1, tzinfo=UTC))
        self.assertEqual(adapters.parse_ts(1_760_000_000_000), adapters.parse_ts(1_760_000_000))
        self.assertIsNone(adapters.parse_ts("nope"))
        self.assertIsNone(adapters.parse_ts(True))


class ConfigTests(unittest.TestCase):
    def test_valid_config(self):
        cfg = parse_config({"timezone": "Asia/Kolkata", "warn_at": 0.5, "budget": {"monthly": 10},
                            "projects": {"web": {"daily": 2}},
                            "pricing": {"sonnet-4": {"input": 1, "output": 2}}})
        self.assertEqual(cfg.limits, {"monthly": 10.0})
        self.assertIn("sonnet-4", cfg.prices)
        self.assertIn("opus", cfg.prices)  # starter prices kept

    def test_bad_values_give_clear_errors(self):
        for bad, text in [
            ({"budget": {"yearly": 5}}, "unknown period"),
            ({"budget": {"monthly": -1}}, ">= 0"),
            ({"budget": {"monthly": 0}}, "greater than 0"),
            ({"budget": {"monthly": "ten"}}, "must be a number"),
            ({"warn_at": 2}, "between 0 and 1"),
            ({"timezone": "Mars/Base"}, "not found"),
            ({"pricing": {"x": {"input": 1}}}, "needs at least"),
        ]:
            with self.subTest(bad=bad), self.assertRaisesRegex(ConfigError, text):
                parse_config(bad)


class CliTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.usage_file = self.dir / "u.jsonl"
        # $15 of sonnet output on 2026-10-15
        self.usage_file.write_text(json.dumps({
            "timestamp": "2026-10-15T10:00:00Z", "model": "claude-sonnet-4-6",
            "project": "shop", "output_tokens": 1_000_000}) + "\n")

    def tearDown(self):
        self.tmp.cleanup()

    def config(self, monthly):
        path = self.dir / "spendguard.toml"
        path.write_text(f'timezone = "UTC"\n[budget]\nmonthly = {monthly}\n')
        return str(path)

    def run_cli(self, *args):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = cli.main([*args])
        return code, out.getvalue(), err.getvalue()

    def check(self, monthly, *extra):
        return self.run_cli("check", "--config", self.config(monthly), "--usage", str(self.usage_file),
                            "--no-claude", "--now", "2026-10-16T12:00:00Z", *extra)

    def test_ok_returns_zero(self):
        code, out, _ = self.check(100)
        self.assertEqual(code, 0)
        self.assertIn("OK", out)

    def test_over_returns_one_and_github_annotation(self):
        code, out, _ = self.check(10, "--github")
        self.assertEqual(code, 1)
        self.assertIn("::error title=spendguard::", out)

    def test_warn_only_fails_when_asked(self):
        self.assertEqual(self.check(18)[0], 0)
        self.assertEqual(self.check(18, "--fail-on", "warn")[0], 1)

    def test_hook_mode_blocks_with_2_and_keeps_stdout_clean(self):
        code, out, err = self.check(10, "--hook")
        self.assertEqual((code, out), (2, ""))
        self.assertIn("budget reached", err)
        code, out, err = self.check(100, "--hook")
        self.assertEqual((code, out, err), (0, "", ""))

    def test_check_json(self):
        _, out, _ = self.check(10, "--json")
        data = json.loads(out)
        self.assertEqual(data["status"], "over")
        self.assertAlmostEqual(data["checks"][0]["spent_usd"], 15.0)

    def test_report_json_total(self):
        code, out, _ = self.run_cli("report", "--config", self.config(100), "--usage", str(self.usage_file),
                                    "--no-claude", "--by", "project", "--json")
        self.assertEqual(code, 0)
        data = json.loads(out)
        self.assertAlmostEqual(data["total"]["cost_usd"], 15.0)
        self.assertIn("shop", data["rows"])

    def test_report_filters(self):
        code, out, _ = self.run_cli("report", "--config", self.config(100), "--usage", str(self.usage_file),
                                    "--no-claude", "--since", "2026-10-16")
        self.assertIn("No usage found", out)

    def test_bad_inputs_exit_3(self):
        self.assertEqual(self.run_cli("report", "--no-claude", "--usage", "/nope.jsonl")[0], 3)
        self.assertEqual(self.run_cli("report", "--no-claude", "--since", "yesterday")[0], 3)
        self.assertEqual(self.run_cli("check", "--config", "/nope.toml")[0], 3)

    def test_export_round_trips(self):
        code, out, _ = self.run_cli("export", "--config", self.config(100), "--usage", str(self.usage_file),
                                    "--no-claude", "--user", "asha")
        self.assertEqual(code, 0)
        row = json.loads(out.strip())
        self.assertEqual((row["user"], row["project"], row["output_tokens"]), ("asha", "shop", 1_000_000))

    def test_no_budgets_configured_is_not_an_error(self):
        empty = self.dir / "empty.toml"
        empty.write_text("")
        code, out, _ = self.run_cli("check", "--config", str(empty), "--no-claude")
        self.assertEqual(code, 0)
        self.assertIn("No budgets configured", out)

    def test_init_refuses_to_overwrite(self):
        import os
        old = os.getcwd()
        os.chdir(self.dir)
        try:
            self.assertEqual(self.run_cli("init")[0], 0)
            self.assertEqual(self.run_cli("init")[0], 3)
            self.assertEqual(self.run_cli("init", "--force")[0], 0)
            cfg = parse_config(__import__("tomllib").loads((self.dir / "spendguard.toml").read_text()))
            self.assertEqual(cfg.limits["monthly"], 300.0)
        finally:
            os.chdir(old)


if __name__ == "__main__":
    unittest.main()
