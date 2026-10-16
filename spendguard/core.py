"""Data model, pricing, aggregation and budget logic. Standard library only."""
from __future__ import annotations

import datetime as dt
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Callable, Iterable

MILLION = 1_000_000


@dataclass(frozen=True)
class Price:
    """USD per one million tokens."""

    input: float
    output: float
    cache_read: float | None = None   # default: 10% of input
    cache_write: float | None = None  # default: 125% of input

    def cache_read_price(self) -> float:
        return self.cache_read if self.cache_read is not None else self.input * 0.10

    def cache_write_price(self) -> float:
        return self.cache_write if self.cache_write is not None else self.input * 1.25


# STARTER prices only. Providers change prices and models often, so users are
# expected to override these in spendguard.toml. The longest matching pattern wins.
STARTER_PRICES: dict[str, Price] = {
    "opus": Price(15.0, 75.0),
    "sonnet": Price(3.0, 15.0),
    "haiku": Price(1.0, 5.0),
}


@dataclass(frozen=True)
class Usage:
    """One billable model call, normalised from any source."""

    ts: dt.datetime            # timezone-aware
    model: str
    project: str = "unknown"   # short name, e.g. the repo folder
    path: str = ""             # full project path if known
    user: str = ""
    input: int = 0
    output: int = 0
    cache_read: int = 0
    cache_write: int = 0
    cost: float | None = None  # explicit cost from the source, if it has one
    source: str = ""

    @property
    def total_tokens(self) -> int:
        return self.input + self.output + self.cache_read + self.cache_write


def find_price(model: str, table: dict[str, Price]) -> tuple[str, Price] | None:
    """Return (pattern, price) for the longest pattern contained in the model name."""
    name = model.lower()
    best: tuple[str, Price] | None = None
    for pattern, price in table.items():
        if pattern.lower() in name and (best is None or len(pattern) > len(best[0])):
            best = (pattern, price)
    return best


def cost_of(usage: Usage, table: dict[str, Price]) -> tuple[float, bool]:
    """Return (usd, priced). priced is False when no price could be found."""
    if usage.cost is not None:
        return usage.cost, True
    hit = find_price(usage.model, table)
    if hit is None:
        return 0.0, False
    p = hit[1]
    usd = (
        usage.input * p.input
        + usage.output * p.output
        + usage.cache_read * p.cache_read_price()
        + usage.cache_write * p.cache_write_price()
    ) / MILLION
    return usd, True


@dataclass
class Totals:
    input: int = 0
    output: int = 0
    cache_read: int = 0
    cache_write: int = 0
    cost: float = 0.0
    unpriced_tokens: int = 0
    unpriced_models: set[str] = field(default_factory=set)

    def add(self, u: Usage, usd: float, priced: bool) -> None:
        self.input += u.input
        self.output += u.output
        self.cache_read += u.cache_read
        self.cache_write += u.cache_write
        self.cost += usd
        if not priced:
            self.unpriced_tokens += u.total_tokens
            self.unpriced_models.add(u.model)


def _local(ts: dt.datetime, tz: dt.tzinfo) -> dt.datetime:
    return ts.astimezone(tz)


def week_start(day: dt.date) -> dt.date:
    return day - dt.timedelta(days=day.weekday())  # Monday


GROUPERS: dict[str, Callable[[Usage, dt.tzinfo], str]] = {
    "day": lambda u, tz: _local(u.ts, tz).date().isoformat(),
    "week": lambda u, tz: week_start(_local(u.ts, tz).date()).isoformat(),
    "month": lambda u, tz: _local(u.ts, tz).strftime("%Y-%m"),
    "project": lambda u, tz: u.project,
    "model": lambda u, tz: u.model,
    "user": lambda u, tz: u.user or "(none)",
}


def summarize(
    records: Iterable[Usage], table: dict[str, Price], tz: dt.tzinfo, group: str = "day"
) -> dict[str, Totals]:
    key_fn = GROUPERS[group]
    rows: dict[str, Totals] = defaultdict(Totals)
    for u in records:
        usd, priced = cost_of(u, table)
        rows[key_fn(u, tz)].add(u, usd, priced)
    return dict(sorted(rows.items()))


def period_start(now: dt.datetime, period: str) -> dt.datetime:
    """Start of the current day / ISO week (Monday) / month in now's timezone."""
    midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
    if period == "daily":
        return midnight
    if period == "weekly":
        return midnight - dt.timedelta(days=now.weekday())
    if period == "monthly":
        return midnight.replace(day=1)
    raise ValueError(f"unknown period: {period}")


PERIODS = ("daily", "weekly", "monthly")


@dataclass(frozen=True)
class Check:
    scope: str      # "all" or a project name
    period: str     # daily / weekly / monthly
    spent: float
    limit: float
    status: str     # ok / warn / over

    @property
    def percent(self) -> float:
        return 100.0 * self.spent / self.limit if self.limit else 0.0


def project_matches(usage: Usage, name: str) -> bool:
    n = name.lower()
    return usage.project.lower() == n or (bool(usage.path) and n in usage.path.lower())


def evaluate(
    records: list[Usage],
    table: dict[str, Price],
    global_limits: dict[str, float],
    project_limits: dict[str, dict[str, float]],
    warn_at: float,
    now: dt.datetime,
) -> tuple[list[Check], set[str]]:
    """Compare spend in the current periods to limits.

    Returns (checks, unpriced_models). Unpriced models mean spend is understated.
    """
    checks: list[Check] = []
    unpriced: set[str] = set()
    priced_records = []
    for u in records:
        usd, priced = cost_of(u, table)
        if not priced:
            unpriced.add(u.model)
        priced_records.append((u, usd))

    def spent(period: str, scope: str) -> float:
        start = period_start(now, period)
        total = 0.0
        for u, usd in priced_records:
            if start <= u.ts <= now and (scope == "all" or project_matches(u, scope)):
                total += usd
        return total

    def status(s: float, limit: float) -> str:
        if s >= limit:
            return "over"
        if s >= warn_at * limit:
            return "warn"
        return "ok"

    for period in PERIODS:
        if period in global_limits:
            s = spent(period, "all")
            checks.append(Check("all", period, s, global_limits[period], status(s, global_limits[period])))
    for name, limits in project_limits.items():
        for period in PERIODS:
            if period in limits:
                s = spent(period, name)
                checks.append(Check(name, period, s, limits[period], status(s, limits[period])))
    return checks, unpriced
