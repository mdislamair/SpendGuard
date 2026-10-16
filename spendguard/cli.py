"""Command line interface: report, check, models, export, init."""
from __future__ import annotations

import argparse
import datetime as dt
import json
import sys
from pathlib import Path

from . import __version__
from .adapters import default_claude_dirs, parse_ts, read_claude, read_generic, to_generic_row
from .config import SAMPLE_CONFIG, Config, ConfigError, load_config
from .core import GROUPERS, Check, Totals, Usage, evaluate, find_price, summarize

EXIT_OVER = 1
EXIT_HOOK_BLOCK = 2  # Claude Code hooks block an action when a hook exits with 2
EXIT_ERROR = 3


def money(value: float) -> str:
    return f"${value:,.2f}"


def render_table(headers: list[str], rows: list[list[str]]) -> str:
    widths = [max(len(str(r[i])) for r in [headers, *rows]) for i in range(len(headers))]
    def line(cells: list[str]) -> str:
        parts = [str(cells[0]).ljust(widths[0])] + [str(c).rjust(w) for c, w in zip(cells[1:], widths[1:])]
        return "  ".join(parts)
    rule = "  ".join("-" * w for w in widths)
    return "\n".join([line(headers), rule, *[line(r) for r in rows]])


# ---------------------------------------------------------------- loading

def load_records(cfg: Config, args: argparse.Namespace) -> tuple[list[Usage], list[str]]:
    """Read every configured source. Returns (records, notes_about_what_was_searched)."""
    notes: list[str] = []
    records: list[Usage] = []

    usage_files = [Path(p).expanduser() for p in (args.usage or cfg.usage_files)]
    for f in usage_files:
        if not f.is_file():
            raise ValueError(f"usage file not found: {f}")
    if usage_files:
        records += list(read_generic(usage_files))
        notes.append(f"read {len(usage_files)} usage file(s)")

    if not args.no_claude:
        explicit = args.claude_dir or cfg.claude_dirs
        dirs = [Path(p).expanduser() for p in explicit] if explicit else default_claude_dirs()
        missing = [d for d in dirs if not d.is_dir()]
        for d in missing:
            print(f"spendguard: warning: folder not found: {d}", file=sys.stderr)
        dirs = [d for d in dirs if d.is_dir()]
        if dirs:
            records += list(read_claude(dirs))
            notes.append("read Claude Code logs in " + ", ".join(str(d) for d in dirs))
        elif not usage_files:
            notes.append("no Claude Code log folder found (looked in ~/.claude/projects)")
    return records, notes


def filter_records(records: list[Usage], cfg: Config, args: argparse.Namespace) -> list[Usage]:
    tz = cfg.timezone
    out = records
    if getattr(args, "since", None):
        start = _day(args.since, tz)
        out = [u for u in out if u.ts >= start]
    if getattr(args, "until", None):
        end = _day(args.until, tz) + dt.timedelta(days=1)
        out = [u for u in out if u.ts < end]
    if getattr(args, "project", None):
        needle = args.project.lower()
        out = [u for u in out if needle in u.project.lower() or needle in u.path.lower()]
    return out


def _day(text: str, tz: dt.tzinfo) -> dt.datetime:
    try:
        return dt.datetime.strptime(text, "%Y-%m-%d").replace(tzinfo=tz)
    except ValueError:
        raise ValueError(f"bad date {text!r}, use YYYY-MM-DD") from None


# ---------------------------------------------------------------- commands

def cmd_report(args: argparse.Namespace) -> int:
    cfg = load_config(args.config)
    records, notes = load_records(cfg, args)
    records = filter_records(records, cfg, args)
    rows = summarize(records, cfg.prices, cfg.timezone, args.by)

    total = Totals()
    for t in rows.values():
        total.input += t.input
        total.output += t.output
        total.cache_read += t.cache_read
        total.cache_write += t.cache_write
        total.cost += t.cost
        total.unpriced_tokens += t.unpriced_tokens
        total.unpriced_models |= t.unpriced_models

    if args.json:
        payload = {
            "group_by": args.by,
            "rows": {k: _totals_dict(t) for k, t in rows.items()},
            "total": _totals_dict(total),
        }
        print(json.dumps(payload, indent=2))
        return 0

    if not rows:
        print("No usage found. " + "; ".join(notes))
        return 0

    table = [
        [k, f"{t.input:,}", f"{t.output:,}", f"{t.cache_read:,}", f"{t.cache_write:,}", money(t.cost)]
        for k, t in rows.items()
    ]
    table.append(["TOTAL", f"{total.input:,}", f"{total.output:,}", f"{total.cache_read:,}", f"{total.cache_write:,}", money(total.cost)])
    print(render_table([args.by, "input", "output", "cache read", "cache write", "cost"], table))
    print()
    source = cfg.source_path or "built-in starter prices"
    print(f"Estimate = tokens x prices from {source}. Not an invoice.")
    if total.unpriced_models:
        print(
            f"! {total.unpriced_tokens:,} tokens came from models with no price "
            f"({', '.join(sorted(total.unpriced_models))}), so cost is understated. "
            f"Add a [pricing.<name>] section to spendguard.toml."
        )
    return 0


def _totals_dict(t: Totals) -> dict:
    return {
        "input_tokens": t.input,
        "output_tokens": t.output,
        "cache_read_tokens": t.cache_read,
        "cache_write_tokens": t.cache_write,
        "cost_usd": round(t.cost, 6),
        "unpriced_tokens": t.unpriced_tokens,
    }


def overall_status(checks: list[Check]) -> str:
    if any(c.status == "over" for c in checks):
        return "over"
    if any(c.status == "warn" for c in checks):
        return "warn"
    return "ok"


def cmd_check(args: argparse.Namespace) -> int:
    cfg = load_config(args.config)
    if not cfg.limits and not cfg.project_limits:
        if not args.hook:
            print("No budgets configured. Run `spendguard init` to create spendguard.toml.")
        return 0

    records, notes = load_records(cfg, args)
    now = parse_ts(args.now).astimezone(cfg.timezone) if args.now else dt.datetime.now(cfg.timezone)
    checks, unpriced = evaluate(records, cfg.prices, cfg.limits, cfg.project_limits, cfg.warn_at, now)
    status = overall_status(checks)

    if args.hook:
        # Hooks must keep stdout clean (Claude Code may add stdout to the model's context).
        if status == "over":
            worst = [c for c in checks if c.status == "over"]
            names = ", ".join(f"{c.scope} {c.period} {money(c.spent)} of {money(c.limit)}" for c in worst)
            print(f"spendguard: budget reached ({names}). Raise the limit in spendguard.toml to continue.", file=sys.stderr)
            return EXIT_HOOK_BLOCK
        return 0

    if args.json:
        print(json.dumps({
            "status": status,
            "checks": [
                {"scope": c.scope, "period": c.period, "spent_usd": round(c.spent, 6),
                 "limit_usd": c.limit, "percent": round(c.percent, 1), "status": c.status}
                for c in checks
            ],
            "unpriced_models": sorted(unpriced),
        }, indent=2))
    elif not args.quiet or status != "ok":
        rows = [[c.scope, c.period, money(c.spent), money(c.limit), f"{c.percent:.0f}%", c.status.upper()] for c in checks]
        print(render_table(["scope", "period", "spent", "limit", "used", "status"], rows))
        if unpriced:
            print(f"\n! No price for: {', '.join(sorted(unpriced))}. Spend is understated; add [pricing.<name>].")
        if not records:
            print("\nNo usage found. " + "; ".join(notes))

    if args.github:
        for c in checks:
            if c.status in ("warn", "over"):
                level = "error" if c.status == "over" else "warning"
                print(f"::{level} title=spendguard::{c.scope} {c.period}: {money(c.spent)} of {money(c.limit)} ({c.percent:.0f}%)")

    if status == "over" or (status == "warn" and args.fail_on == "warn"):
        return EXIT_OVER
    return 0


def cmd_models(args: argparse.Namespace) -> int:
    cfg = load_config(args.config)
    records, _ = load_records(cfg, args)
    rows = summarize(records, cfg.prices, cfg.timezone, "model")
    if not rows:
        print("No usage found.")
        return 0
    table = []
    for model, t in rows.items():
        hit = find_price(model, cfg.prices)
        if hit:
            price = f"{hit[0]}: ${hit[1].input:g} in / ${hit[1].output:g} out per 1M"
        elif t.unpriced_tokens:
            price = "UNPRICED"
        else:
            price = "cost_usd from source"
        table.append([model, f"{t.input + t.output + t.cache_read + t.cache_write:,}", price])
    print(render_table(["model", "tokens", "price used"], table))
    return 0


def cmd_export(args: argparse.Namespace) -> int:
    cfg = load_config(args.config)
    records, _ = load_records(cfg, args)
    records = filter_records(records, cfg, args)
    for u in sorted(records, key=lambda x: x.ts):
        print(json.dumps(to_generic_row(u, args.user), separators=(",", ":")))
    return 0


def cmd_init(args: argparse.Namespace) -> int:
    target = Path("spendguard.toml")
    if target.exists() and not args.force:
        print("spendguard.toml already exists. Use --force to overwrite.", file=sys.stderr)
        return EXIT_ERROR
    target.write_text(SAMPLE_CONFIG, encoding="utf-8")
    print("Created spendguard.toml. Edit the limits and prices, then run `spendguard check`.")
    return 0


# ---------------------------------------------------------------- parser

def build_parser() -> argparse.ArgumentParser:
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--config", metavar="FILE", help="path to spendguard.toml")
    common.add_argument("--claude-dir", action="append", default=[], metavar="DIR",
                        help="Claude Code projects folder (repeatable). Default: ~/.claude/projects")
    common.add_argument("--usage", action="append", default=[], metavar="FILE",
                        help="normalised usage JSONL file (repeatable)")
    common.add_argument("--no-claude", action="store_true", help="do not read Claude Code logs")

    filters = argparse.ArgumentParser(add_help=False)
    filters.add_argument("--since", metavar="YYYY-MM-DD")
    filters.add_argument("--until", metavar="YYYY-MM-DD")
    filters.add_argument("--project", metavar="TEXT", help="only projects whose name or path contains TEXT")

    parser = argparse.ArgumentParser(prog="spendguard", description="Budget guard for AI coding spend.")
    parser.add_argument("--version", action="version", version=f"spendguard {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("report", parents=[common, filters], help="show usage and estimated cost")
    p.add_argument("--by", choices=sorted(GROUPERS), default="day")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_report)

    p = sub.add_parser("check", parents=[common], help="compare current spend to budgets; exit 1 if over")
    p.add_argument("--fail-on", choices=["over", "warn"], default="over", help="exit non-zero at this level")
    p.add_argument("--hook", action="store_true", help="silent; exit 2 when over budget (Claude Code hook mode)")
    p.add_argument("--github", action="store_true", help="print GitHub Actions annotations")
    p.add_argument("--json", action="store_true")
    p.add_argument("--quiet", action="store_true", help="print nothing when everything is ok")
    p.add_argument("--now", help=argparse.SUPPRESS)  # fixed time, for tests
    p.set_defaults(func=cmd_check)

    p = sub.add_parser("models", parents=[common], help="list models seen and the price used for each")
    p.set_defaults(func=cmd_models)

    p = sub.add_parser("export", parents=[common, filters], help="write normalised JSONL (to merge a team's usage)")
    p.add_argument("--user", default="", help="label rows with this user name")
    p.set_defaults(func=cmd_export)

    p = sub.add_parser("init", help="create a sample spendguard.toml")
    p.add_argument("--force", action="store_true")
    p.set_defaults(func=cmd_init)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except (ConfigError, ValueError) as exc:
        print(f"spendguard: {exc}", file=sys.stderr)
        return EXIT_ERROR
    except BrokenPipeError:
        return 0
