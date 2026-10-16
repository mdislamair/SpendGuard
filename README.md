# spendguard

**A budget guard for AI coding spend.** It reads the usage logs your AI coding tools already write, estimates the cost, and tells you (or your CI, or your agent) when you are over budget.

- Zero dependencies. One Python package. Runs offline.
- Daily, weekly and monthly limits, globally and per project.
- Exits non-zero over budget, so it works in scripts and CI.
- Can **block new Claude Code prompts** once a budget is reached (hook mode).
- Merge usage from a whole team with one JSONL format.

Reporting tools such as [ccusage](https://github.com/ryoppippi/ccusage) already show you what you spent. spendguard is about the next step: **setting limits and acting on them**.

## Why

AI coding spend is hard to control. Analyst firm Gartner has warned that AI coding costs could match an engineer's monthly salary within about two years, and that vendors have not yet delivered "mature, built-in cost optimization capabilities" ([InfoWorld](https://www.infoworld.com/article/4189176/ai-coding-token-costs-are-on-track-to-rival-human-payroll-2.html)). Teams end up stitching together separate vendor dashboards by hand ([Faros](https://www.faros.ai/blog/how-to-track-ai-coding-costs-across-teams)). spendguard is a small, free, local building block for limits and alerts.

## Install

Needs Python 3.11 or newer.

```bash
pip install spendguard          # once published; or: pip install .
spendguard init                 # writes spendguard.toml
spendguard report               # usage per day
spendguard check                # compare to your budgets
```

Without installing: `python -m spendguard report` from this folder.

## Quick start

1. `spendguard init`, then edit `spendguard.toml`: set your limits and **check the prices**.
2. `spendguard models` shows every model found and which price it uses. Anything marked `UNPRICED` is not counted in the cost, so add a `[pricing.<name>]` section for it.
3. `spendguard check` prints a table and exits `1` if you are over a limit.

Real output from `examples/team-usage.jsonl` with a $20 monthly limit and an $8 limit on the `shop` project:

```
scope   period   spent   limit  used  status
-----  -------  ------  ------  ----  ------
all      daily   $0.37  $20.00    2%      OK
all    monthly  $11.88  $20.00   59%      OK
shop   monthly  $11.12   $8.00  139%    OVER
```

### Exit codes

| code | meaning |
|------|---------|
| 0 | ok (or warn, unless `--fail-on warn`) |
| 1 | over budget |
| 2 | over budget in `--hook` mode (Claude Code blocks on exit code 2) |
| 3 | bad config, bad input or bad date |

## Block prompts when over budget (Claude Code)

Add a hook to `~/.claude/settings.json` so Claude Code refuses new prompts once a limit is hit:

```json
{
  "hooks": {
    "UserPromptSubmit": [
      { "hooks": [ { "type": "command", "command": "spendguard check --hook" } ] }
    ]
  }
}
```

In `--hook` mode spendguard prints nothing on success, and writes the reason to stderr when blocking. Hook settings change between Claude Code versions, so confirm the format in the [Claude Code hooks documentation](https://docs.claude.com/en/docs/claude-code/hooks). I tested the exit codes, not a live Claude Code session.

## Teams and CI

Each developer's usage lives on their own machine, so a CI runner will not have it. Collect it centrally:

```bash
# on each developer machine (cron, or a login script)
spendguard export --since 2026-10-01 --user asha >> team-usage.jsonl

# anywhere that can see the merged file (CI, a scheduled job)
spendguard check --usage team-usage.jsonl --no-claude --github
```

`--github` prints `::warning::` / `::error::` annotations for GitHub Actions. Merging files means duplicates are your job: export each developer's usage once per period, or de-duplicate before checking.

### Usage file format (works for any tool)

One JSON object per line. Only `timestamp` and `model` are required.

```json
{"timestamp":"2026-10-15T10:00:00Z","model":"claude-sonnet-4-6","project":"shop","user":"asha","input_tokens":1200,"output_tokens":340,"cache_read_tokens":8000,"cache_write_tokens":0,"cost_usd":0.012}
```

If `cost_usd` is present it is used as is. Otherwise cost = tokens x your prices.

## Commands

| command | what it does |
|---------|--------------|
| `report --by day\|week\|month\|project\|model\|user` | usage and cost table, `--json` for scripts, `--since/--until/--project` filters |
| `check` | budgets vs current day / week / month. `--fail-on warn`, `--hook`, `--github`, `--json`, `--quiet` |
| `models` | models seen and the price matched |
| `export` | normalised JSONL for merging team usage |
| `init` | sample `spendguard.toml` |

## Know the limits

- **Costs are estimates.** Tokens x the prices in your config. The built-in prices are only starter values, so edit them. This is not an invoice and ignores discounts, subscriptions and flat plans.
- **Only reads local logs.** Right now that means Claude Code logs, plus the generic JSONL format. Other tools (Codex, Gemini CLI and so on) need an adapter. [Adding one is about 30 lines](CONTRIBUTING.md).
- **Logs can be deleted.** Claude Code may clean up old session logs, so long-range totals depend on what is still on disk.
- **Duplicates are handled for Claude Code.** One response is often logged several times, so spendguard de-duplicates by message id and request id. On one real session log, 87 raw lines were 20 actual calls.
- **It only stops what hooks can stop.** It cannot cap spending inside tools that do not run hooks.

## Develop

```bash
python -m unittest discover -s tests -v
```

## Support

If this saves you money, you can sponsor the project via the Sponsor button on GitHub. Sponsorship pays for adapters for more tools and faster fixes.

## License

MIT
