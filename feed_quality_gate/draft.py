"""Draft a starter rules file from an existing feed (`fqg init`).

Onboarding a feed means writing a rules YAML. Most of what goes in it can be
read off a sample of the feed itself: which column identifies a row, which one
dates it, and how often each column is actually missing. This module profiles a
DataFrame and renders a commented rules file a human can review and commit.

The draft is a starting point, not a verdict. Every guess is written as a
comment next to the value it produced, and nothing here is clever enough to
know a feed's real scrape cadence, so freshness keeps the package default.
"""

from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass, field
from typing import Any

import pandas as pd
import yaml

from .checks import _is_blank
from .rules import Rules

#: Column-name hints, checked case-insensitively. A hint only breaks ties
#: between columns that already qualify on their values.
_ID_HINT = re.compile(r"(^|_)(id|sku|ean|gtin|code|key)$|^id", re.IGNORECASE)
_TS_HINT = re.compile(r"(_at$|date|time|timestamp|updated|scraped|created)", re.IGNORECASE)

#: Share of a column's non-blank values that must parse as dates for it to be
#: offered as the timestamp column.
TIMESTAMP_PARSE_SHARE = 0.95

#: Headroom added on top of the observed missing ratio, so one more gap than
#: the sample had does not fail the gate on day one.
MIN_HEADROOM = 0.02


@dataclass
class ColumnProfile:
    name: str
    blank_ratio: float
    max_null_ratio: float
    severity: str
    quarantine: bool


@dataclass
class Draft:
    feed_id: str
    row_count: int
    id_column: str | None
    timestamp_column: str | None
    newest_timestamp: str | None
    columns: list[ColumnProfile] = field(default_factory=list)

    def to_mapping(self) -> dict[str, Any]:
        """The plain rules mapping this draft stands for (no comments)."""
        return {
            "feed_id": self.feed_id,
            "id_column": self.id_column,
            "timestamp_column": self.timestamp_column,
            "freshness": {"enabled": self.timestamp_column is not None},
            "completeness": {
                "enabled": True,
                "columns": {
                    c.name: {
                        "max_null_ratio": c.max_null_ratio,
                        "severity": c.severity,
                        "quarantine": c.quarantine,
                    }
                    for c in self.columns
                },
            },
        }


def _ceil2(x: float) -> float:
    return min(1.0, math.ceil(round(x * 100, 6)) / 100)


def _guess_id_column(df: pd.DataFrame) -> str | None:
    """A column with no blanks and no repeats. Float columns are never ids."""
    if df.empty:
        return None
    candidates = [
        c
        for c in df.columns
        if not pd.api.types.is_float_dtype(df[c])
        and not _is_blank(df[c]).any()
        and df[c].astype("string").str.strip().nunique() == len(df)
    ]
    if not candidates:
        return None
    hinted = [c for c in candidates if _ID_HINT.search(str(c))]
    return str((hinted or candidates)[0])


def _parse_share(series: pd.Series) -> tuple[float, pd.Timestamp | None]:
    values = series[~_is_blank(series)]
    if values.empty:
        return 0.0, None
    parsed = pd.to_datetime(values.astype("string"), errors="coerce", utc=True, format="mixed")
    share = float(parsed.notna().mean())
    return share, (parsed.max() if parsed.notna().any() else None)


def _guess_timestamp_column(
    df: pd.DataFrame, exclude: str | None
) -> tuple[str | None, pd.Timestamp | None]:
    """A text or datetime column whose values parse as dates.

    Numeric columns are skipped on purpose: pandas reads a price of 18.0 as
    18 nanoseconds after the epoch, so every number would "parse".
    """
    found: list[tuple[str, pd.Timestamp | None]] = []
    for c in df.columns:
        if c == exclude or pd.api.types.is_numeric_dtype(df[c]):
            continue
        share, newest = _parse_share(df[c])
        if share >= TIMESTAMP_PARSE_SHARE:
            found.append((str(c), newest))
    if not found:
        return None, None
    hinted = [f for f in found if _TS_HINT.search(f[0])]
    return (hinted or found)[0]


def _profile_column(df: pd.DataFrame, name: str) -> ColumnProfile:
    ratio = float(_is_blank(df[name]).mean()) if len(df) else 0.0
    # Always populated in the sample: a gap is an anomaly, so block and hold
    # the row back. Already gappy: report it, let the rows flow, and leave
    # room above what was observed.
    if ratio == 0.0:
        return ColumnProfile(name, 0.0, MIN_HEADROOM, "fail", True)
    allowance = _ceil2(ratio + max(MIN_HEADROOM, ratio * 0.5))
    return ColumnProfile(name, ratio, allowance, "warn", False)


def draft_rules(df: pd.DataFrame, feed_id: str = "unnamed-feed") -> Draft:
    """Profile ``df`` and return the rules it suggests."""
    id_column = _guess_id_column(df)
    ts_column, newest = _guess_timestamp_column(df, exclude=id_column)
    skip = {id_column, ts_column}
    return Draft(
        feed_id=feed_id,
        row_count=len(df),
        id_column=id_column,
        timestamp_column=ts_column,
        newest_timestamp=newest.isoformat() if newest is not None else None,
        columns=[_profile_column(df, str(c)) for c in df.columns if c not in skip],
    )


def _scalar(value: Any) -> str:
    """Render a YAML scalar, quoting only when the bare form would change meaning."""
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int | float):
        return repr(value)
    text = str(value)
    try:
        if text and yaml.safe_load(f"k: {text}") == {"k": text}:
            return text
    except yaml.YAMLError:
        pass
    return json.dumps(text)  # a JSON string is a valid YAML double-quoted scalar


def render_yaml(draft: Draft, source: str = "") -> str:
    """Render ``draft`` as a commented rules file."""
    src = f" from {source}" if source else ""
    lines = [
        f"# Drafted by `fqg init`{src} ({draft.row_count} rows).",
        "# Review every value before relying on it: these are guesses from one sample.",
        "",
        f"feed_id: {_scalar(draft.feed_id)}",
        "",
    ]
    if draft.id_column:
        lines.append("# Guessed: fully populated and unique on every row of the sample.")
    else:
        lines.append("# No column was unique and fully populated, so rows are named by position.")
    lines += [f"id_column: {_scalar(draft.id_column)}", ""]

    if draft.timestamp_column:
        lines.append("# Guessed: its values parse as dates.")
    else:
        lines.append("# No column parsed as dates. Set one to enable the freshness check.")
    lines += [f"timestamp_column: {_scalar(draft.timestamp_column)}", "", "freshness:"]

    if draft.timestamp_column:
        lines += [
            f"  # Newest row in the sample: {draft.newest_timestamp}.",
            "  # Set max_age_days a little above your real update cadence.",
            "  enabled: true",
            "  max_age_days: 7",
        ]
    else:
        lines.append("  enabled: false")

    lines += ["", "completeness:", "  enabled: true"]
    if not draft.columns:
        lines.append("  columns: {}")
    else:
        lines.append("  columns:")
        for c in draft.columns:
            lines += [
                f"    # missing on {c.blank_ratio:.1%} of sample rows",
                f"    {_scalar(c.name)}:",
                f"      max_null_ratio: {_scalar(c.max_null_ratio)}",
                f"      severity: {c.severity}",
                f"      quarantine: {_scalar(c.quarantine)}",
            ]
    lines += [
        "",
        "# per_source_completeness and stuck_value_cluster stay off by default.",
        "# See rules.example.yaml to turn them on.",
        "",
    ]
    text = "\n".join(lines)
    # The draft must load exactly like a hand-written file would.
    Rules.from_mapping(yaml.safe_load(text))
    return text
