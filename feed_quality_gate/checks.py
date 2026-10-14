"""The checks themselves.

Every check is a pure function of ``(DataFrame, rule)`` returning a
``CheckResult``. No I/O, no database, no network, no global state - which is
what makes them trivially testable against synthetic fixtures and safe to run
inside an Airflow task or a CLI alike.

v1 ships the two founding checks; each additional check is an independent
pure function and so is a self-contained pull request.
"""

from __future__ import annotations

import warnings
from datetime import UTC, datetime
from typing import Any

import pandas as pd

from .models import CheckResult
from .rules import ColumnRule, CompletenessRule, FreshnessRule, PerSourceCompletenessRule

#: How many offending ids to carry in a CheckResult before truncating.
SAMPLE_LIMIT = 10


def _row_ids(df: pd.DataFrame, id_column: str | None) -> list:
    """Identify rows by the configured id column, falling back to the index."""
    if id_column and id_column in df.columns:
        return df[id_column].tolist()
    return df.index.tolist()


def _is_blank(series: pd.Series) -> pd.Series:
    """True where a value is missing in the way a scraped feed goes missing.

    Nulls are the obvious case, but a scraper that fails to find a field far
    more often writes an empty or whitespace-only string than a true null.
    Treating those as present is how a 'complete' feed still has no prices.
    """
    blank = series.isna()

    # Test for "text-like" rather than `dtype == object`: pandas 2.x types a CSV
    # string column as `object`, pandas 3.x types it as `str`, and an equality
    # check against `object` silently stops matching on 3.x - letting every
    # whitespace-only value through as if it were populated.
    text_like = pd.api.types.is_object_dtype(series) or pd.api.types.is_string_dtype(
        series
    )
    if text_like:
        as_str = series.astype("string")
        blank = blank | as_str.str.strip().eq("").fillna(False)

    return blank


def as_utc(now: datetime | None) -> datetime:
    """Return ``now`` as a timezone-aware UTC datetime (current time if None).

    A naive ``now`` is taken to be UTC - the same assumption
    ``pd.to_datetime(..., utc=True)`` makes for the feed's own naive
    timestamps - so the two sides of the age subtraction agree. Without this a
    naive value crashed with a bare pandas ``TypeError`` ("Cannot convert
    tz-naive Timestamp"), and ``datetime.astimezone`` would silently read it
    as the machine's local time when the report is serialised.
    """
    if now is None:
        return datetime.now(UTC)
    if now.tzinfo is None or now.tzinfo.utcoffset(now) is None:
        return now.replace(tzinfo=UTC)
    return now.astimezone(UTC)


def check_freshness(
    df: pd.DataFrame,
    rule: FreshnessRule,
    timestamp_column: str | None,
    now: datetime | None = None,
) -> CheckResult:
    """Fail when the newest row is older than the configured maximum age.

    This is the check that would have caught a feed sitting 33 days stale while
    dashboards kept rendering week-over-week deltas against it.

    ``now`` defaults to the current time; a naive value is treated as UTC.
    """
    now = as_utc(now)

    if not timestamp_column:
        return CheckResult(
            name="freshness",
            passed=False,
            severity=rule.severity,
            message=(
                "freshness is enabled but no `timestamp_column` is set in the "
                "rules - cannot tell whether this feed is stale"
            ),
        )

    if timestamp_column not in df.columns:
        return CheckResult(
            name="freshness",
            passed=False,
            severity=rule.severity,
            message=(
                f"timestamp column {timestamp_column!r} is not present in the "
                "feed - a column the scraper used to emit may have been dropped"
            ),
        )

    if df.empty:
        return CheckResult(
            name="freshness",
            passed=False,
            severity=rule.severity,
            message="feed is empty - no rows to date",
        )

    # A feed's timestamps may be mixed or unparseable; that is exactly what this
    # check exists to report. `coerce` turns junk into NaT and the all-NaT branch
    # below reports it, so pandas' "could not infer format" warning is noise here.
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", UserWarning)
        ts = pd.to_datetime(df[timestamp_column], errors="coerce", utc=True)
    if ts.isna().all():
        return CheckResult(
            name="freshness",
            passed=False,
            severity=rule.severity,
            message=(
                f"no parseable timestamps in {timestamp_column!r} - "
                "every value failed to parse as a date"
            ),
        )

    newest = ts.max()
    age_days = (pd.Timestamp(now) - newest).total_seconds() / 86400
    passed = age_days <= rule.max_age_days

    return CheckResult(
        name="freshness",
        passed=passed,
        severity=rule.severity,
        message=(
            f"newest row is {age_days:.1f}d old "
            f"(limit {rule.max_age_days:g}d, newest {newest.isoformat()})"
        ),
        details={
            "age_days": round(age_days, 3),
            "max_age_days": rule.max_age_days,
            "newest": newest.isoformat(),
        },
    )


def check_column_completeness(
    df: pd.DataFrame,
    column: str,
    rule: ColumnRule,
    id_column: str | None = None,
) -> CheckResult:
    """Fail when a column is missing on more rows than the rule tolerates."""
    name = f"completeness:{column}"

    if column not in df.columns:
        return CheckResult(
            name=name,
            passed=False,
            severity=rule.severity,
            total=len(df),
            message=(
                f"column {column!r} is required by the rules but absent from "
                "the feed entirely"
            ),
            details={"missing_column": True},
        )

    blank = _is_blank(df[column])
    count = int(blank.sum())
    total = len(df)
    ratio = count / total if total else 0.0
    passed = ratio <= rule.max_null_ratio

    offending = _row_ids(df[blank], id_column) if count else []

    return CheckResult(
        name=name,
        passed=passed,
        severity=rule.severity,
        count=count,
        total=total,
        message=(
            f"{count}/{total} rows ({ratio:.1%}) missing {column!r} "
            f"(limit {rule.max_null_ratio:.1%})"
        ),
        offending=offending[:SAMPLE_LIMIT],
        details={"max_null_ratio": rule.max_null_ratio, "quarantine": rule.quarantine},
    )


def check_completeness(
    df: pd.DataFrame,
    rule: CompletenessRule,
    id_column: str | None = None,
) -> list[CheckResult]:
    """Run the per-column completeness rules, one CheckResult per column."""
    return [
        check_column_completeness(df, column, column_rule, id_column)
        for column, column_rule in rule.columns.items()
    ]


def check_per_source_completeness(
    df: pd.DataFrame,
    rule: PerSourceCompletenessRule,
    id_column: str | None = None,
) -> CheckResult:
    """Fail when any one source's missing rate exceeds the tolerance.

    Grouping is the point: a feed-wide missing-price rate of ~6% can be one
    source missing 79% of its own catalogue averaged against everything else
    that is fine. That source is invisible to ``check_column_completeness``,
    which only ever sees the global ratio, and its rows then silently vanish
    from every downstream comparison. This check applies the same tolerance
    per source instead of once to the whole feed.
    """
    name = "per_source_completeness"

    if not rule.column or rule.column not in df.columns:
        return CheckResult(
            name=name,
            passed=False,
            severity=rule.severity,
            total=len(df),
            message=(
                f"column {rule.column!r} is required by per_source_completeness "
                "but absent from the feed"
            ),
            details={"missing_column": rule.column},
        )

    if not rule.source_column or rule.source_column not in df.columns:
        return CheckResult(
            name=name,
            passed=False,
            severity=rule.severity,
            total=len(df),
            message=(
                f"source column {rule.source_column!r} is required by "
                "per_source_completeness but absent from the feed"
            ),
            details={"missing_column": rule.source_column},
        )

    if df.empty:
        return CheckResult(
            name=name,
            passed=True,
            severity=rule.severity,
            message="feed is empty - no sources to check",
        )

    blank = _is_blank(df[rule.column])
    work = pd.DataFrame({"__source": df[rule.source_column], "__blank": blank})

    offending: dict[str, dict[str, Any]] = {}
    offending_ids: list = []
    for source, sub in work.groupby("__source", dropna=False):
        total = len(sub)
        if total < rule.min_source_rows:
            continue
        count = int(sub["__blank"].sum())
        ratio = count / total
        if ratio > rule.max_null_ratio:
            label = "<missing>" if pd.isna(source) else str(source)
            offending[label] = {"count": count, "total": total, "ratio": round(ratio, 6)}
            blank_ids_in_group = df.loc[sub.index[sub["__blank"]]]
            offending_ids.extend(_row_ids(blank_ids_in_group, id_column))

    passed = not offending
    total_offending_rows = sum(v["count"] for v in offending.values())

    if passed:
        message = f"no source exceeds the {rule.max_null_ratio:.1%} tolerance for {rule.column!r}"
    else:
        detail = ", ".join(
            f"{source} ({v['count']}/{v['total']}, {v['ratio']:.1%})"
            for source, v in offending.items()
        )
        message = (
            f"{len(offending)} source(s) exceed the {rule.max_null_ratio:.1%} "
            f"tolerance for {rule.column!r}: {detail}"
        )

    return CheckResult(
        name=name,
        passed=passed,
        severity=rule.severity,
        count=total_offending_rows,
        total=len(df),
        message=message,
        offending=offending_ids[:SAMPLE_LIMIT],
        details={
            "column": rule.column,
            "source_column": rule.source_column,
            "max_null_ratio": rule.max_null_ratio,
            "offending_sources": offending,
        },
    )


def quarantine_ids_for(
    df: pd.DataFrame,
    rule: CompletenessRule,
    id_column: str | None = None,
) -> list:
    """Row ids to hold back: any row blank in a column marked ``quarantine``.

    Deliberately independent of whether the column's *ratio* check passed. A
    feed can be 99% complete and still contain rows with no price, and those
    rows must not reach a margin calculation just because their cohort was
    small enough to pass.
    """
    if not len(df):
        return []

    mask = pd.Series(False, index=df.index)
    for column, column_rule in rule.columns.items():
        if column_rule.quarantine and column in df.columns:
            mask = mask | _is_blank(df[column])

    return _row_ids(df[mask], id_column)
