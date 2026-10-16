"""Readers that turn tool logs into `Usage` records.

To add a new tool: write a function that yields `Usage` objects and register it
in `cli.load_records`. See CONTRIBUTING.md.
"""
from __future__ import annotations

import datetime as dt
import json
import os
from pathlib import Path
from typing import Iterator

from .core import Usage


def parse_ts(value: object) -> dt.datetime | None:
    """Parse ISO-8601 strings or epoch seconds/milliseconds into an aware datetime."""
    try:
        if isinstance(value, bool):
            return None
        if isinstance(value, (int, float)):
            seconds = value / 1000 if value > 1e11 else value
            return dt.datetime.fromtimestamp(seconds, dt.timezone.utc)
        if isinstance(value, str) and value:
            ts = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
            return ts if ts.tzinfo else ts.replace(tzinfo=dt.timezone.utc)
    except (ValueError, OverflowError, OSError):
        return None
    return None


def _int(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return 0
    return max(int(value), 0)


def default_claude_dirs() -> list[Path]:
    env = os.environ.get("CLAUDE_CONFIG_DIR")
    roots = [Path(p) for p in env.split(os.pathsep) if p] if env else []
    roots += [Path.home() / ".config" / "claude", Path.home() / ".claude"]
    seen, out = set(), []
    for root in roots:
        projects = root / "projects"
        if projects.is_dir() and projects not in seen:
            seen.add(projects)
            out.append(projects)
    return out


def read_claude(dirs: list[Path]) -> Iterator[Usage]:
    """Claude Code writes one JSONL file per session under <dir>/<project>/.

    A single API response can appear on several lines (one per content block),
    each repeating the usage numbers. We de-duplicate by message id + request id
    and keep the entry with the largest output count.
    """
    best: dict[str, Usage] = {}
    anonymous: list[Usage] = []
    for root in dirs:
        for file in sorted(root.rglob("*.jsonl")):
            try:
                handle = file.open(encoding="utf-8", errors="replace")
            except OSError:
                continue
            with handle:
                for line in handle:
                    if '"usage"' not in line:  # cheap pre-filter, files can be large
                        continue
                    try:
                        entry = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    usage = _claude_entry(entry, file, root)
                    if usage is None:
                        continue
                    msg = entry["message"]
                    key = f"{msg.get('id')}:{entry.get('requestId')}" if msg.get("id") else None
                    if key is None:
                        anonymous.append(usage)
                    elif key not in best or usage.output >= best[key].output:
                        best[key] = usage
    yield from best.values()
    yield from anonymous


def _claude_entry(entry: object, file: Path, root: Path) -> Usage | None:
    if not isinstance(entry, dict) or entry.get("type") != "assistant":
        return None
    msg = entry.get("message")
    if not isinstance(msg, dict):
        return None
    usage = msg.get("usage")
    model = msg.get("model")
    ts = parse_ts(entry.get("timestamp"))
    if not isinstance(usage, dict) or not isinstance(model, str) or ts is None:
        return None
    if model.startswith("<"):  # e.g. "<synthetic>" placeholder messages
        return None
    cwd = entry.get("cwd") if isinstance(entry.get("cwd"), str) else ""
    project = Path(cwd).name if cwd else file.relative_to(root).parts[0]
    return Usage(
        ts=ts,
        model=model,
        project=project or "unknown",
        path=cwd,
        input=_int(usage.get("input_tokens")),
        output=_int(usage.get("output_tokens")),
        cache_read=_int(usage.get("cache_read_input_tokens")),
        cache_write=_int(usage.get("cache_creation_input_tokens")),
        source="claude",
    )


def read_generic(files: list[Path]) -> Iterator[Usage]:
    """Normalised JSONL, one object per line. Only `timestamp` and `model` are required.

    Fields: timestamp, model, project, user, input_tokens, output_tokens,
    cache_read_tokens, cache_write_tokens, cost_usd
    """
    for file in files:
        with file.open(encoding="utf-8", errors="replace") as handle:
            for number, line in enumerate(handle, 1):
                line = line.strip()
                if not line:
                    continue
                try:
                    row = json.loads(line)
                except json.JSONDecodeError:
                    raise ValueError(f"{file}:{number}: not valid JSON") from None
                ts = parse_ts(row.get("timestamp")) if isinstance(row, dict) else None
                if ts is None or not isinstance(row.get("model"), str):
                    raise ValueError(f"{file}:{number}: needs a valid 'timestamp' and a 'model'")
                cost = row.get("cost_usd")
                yield Usage(
                    ts=ts,
                    model=row["model"],
                    project=str(row.get("project") or "unknown"),
                    user=str(row.get("user") or ""),
                    input=_int(row.get("input_tokens")),
                    output=_int(row.get("output_tokens")),
                    cache_read=_int(row.get("cache_read_tokens")),
                    cache_write=_int(row.get("cache_write_tokens")),
                    cost=float(cost) if isinstance(cost, (int, float)) and not isinstance(cost, bool) else None,
                    source="generic",
                )


def to_generic_row(u: Usage, user: str = "") -> dict:
    row = {
        "timestamp": u.ts.astimezone(dt.timezone.utc).isoformat().replace("+00:00", "Z"),
        "model": u.model,
        "project": u.project,
        "input_tokens": u.input,
        "output_tokens": u.output,
        "cache_read_tokens": u.cache_read,
        "cache_write_tokens": u.cache_write,
    }
    if user or u.user:
        row["user"] = user or u.user
    if u.cost is not None:
        row["cost_usd"] = u.cost
    return row
