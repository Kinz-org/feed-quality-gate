"""Evaluate a feed against a rule set and partition it.

Two entry points:

``evaluate`` answers "is this feed trustworthy?" and returns a report.
``gate`` additionally answers "which rows are safe?" and splits the frame.

Both are pure: they never write to a database and never touch the network, so
the same call works from a CLI, an Airflow task, or a test.
"""

from __future__ import annotations

from datetime import datetime

import pandas as pd

from .checks import (
    as_utc,
    check_completeness,
    check_freshness,
    check_per_source_completeness,
    quarantine_ids_for,
    quarantine_mask,
)
from .models import CheckResult, FeedReport
from .rules import Rules


def evaluate(
    df: pd.DataFrame,
    rules: Rules,
    now: datetime | None = None,
) -> FeedReport:
    """Run every enabled check and aggregate the results into a report.

    ``now`` defaults to the current time; a naive value is treated as UTC.
    """
    now = as_utc(now)
    results: list[CheckResult] = []

    if rules.freshness.enabled:
        results.append(
            check_freshness(df, rules.freshness, rules.timestamp_column, now=now)
        )

    if rules.completeness.enabled:
        results.extend(check_completeness(df, rules.completeness, rules.id_column))

    if rules.per_source_completeness.enabled:
        results.append(
            check_per_source_completeness(
                df, rules.per_source_completeness, rules.id_column
            )
        )

    quarantined = (
        quarantine_ids_for(df, rules.completeness, rules.id_column)
        if rules.completeness.enabled
        else []
    )

    return FeedReport(
        feed_id=rules.feed_id,
        evaluated_at=now,
        row_count=len(df),
        results=results,
        quarantined_ids=quarantined,
    )


def gate(
    df: pd.DataFrame,
    rules: Rules,
    now: datetime | None = None,
) -> tuple[FeedReport, pd.DataFrame, pd.DataFrame]:
    """Evaluate, then split the feed into (clean, quarantined).

    Returns ``(report, clean_df, quarantined_df)``. The split is row-level and
    happens regardless of the overall verdict, so a caller can consume the good
    rows from a feed that failed the gate - which is the difference between a
    gate and a trip-wire.
    """
    report = evaluate(df, rules, now=now)

    if not report.quarantined_ids or df.empty:
        return report, df.copy(), df.iloc[0:0].copy()

    # Split on the row mask itself, never by matching quarantined ids back
    # against the frame: an id (or index label) shared by a good row and a
    # bad one would drag the good row into quarantine with it.
    bad_mask = quarantine_mask(df, rules.completeness).to_numpy()
    return report, df[~bad_mask].copy(), df[bad_mask].copy()
