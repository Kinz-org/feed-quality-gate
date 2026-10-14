"""Tests for the individual checks."""

from __future__ import annotations

from datetime import UTC, timedelta, timezone

import pandas as pd
import pytest

from feed_quality_gate.checks import (
    check_column_completeness,
    check_freshness,
    check_per_source_completeness,
    quarantine_ids_for,
)
from feed_quality_gate.models import Severity
from feed_quality_gate.rules import ColumnRule, FreshnessRule, PerSourceCompletenessRule


class TestFreshness:
    def test_fresh_feed_passes(self, clean_feed, rules, now):
        r = check_freshness(clean_feed, rules.freshness, "scraped_at", now=now)
        assert r.passed
        assert r.details["age_days"] < 1

    def test_stale_feed_fails_with_age_in_the_message(self, stale_feed, rules, now):
        r = check_freshness(stale_feed, rules.freshness, "scraped_at", now=now)
        assert not r.passed
        assert r.severity is Severity.FAIL
        assert round(r.details["age_days"]) == 33
        assert "33.0d old" in r.message

    def test_boundary_is_inclusive(self, clean_feed, now):
        """Exactly at the limit passes; a hair over does not."""
        rule = FreshnessRule(max_age_days=7)
        df = clean_feed.copy()

        df["scraped_at"] = now - timedelta(days=7)
        assert check_freshness(df, rule, "scraped_at", now=now).passed

        df["scraped_at"] = now - timedelta(days=7, seconds=1)
        assert not check_freshness(df, rule, "scraped_at", now=now).passed

    def test_missing_timestamp_column_fails_loudly(self, clean_feed, rules, now):
        """A dropped column must not be read as 'no staleness detected'."""
        df = clean_feed.drop(columns=["scraped_at"])
        r = check_freshness(df, rules.freshness, "scraped_at", now=now)
        assert not r.passed
        assert "not present" in r.message

    def test_unconfigured_timestamp_column_fails(self, clean_feed, rules, now):
        r = check_freshness(clean_feed, rules.freshness, None, now=now)
        assert not r.passed
        assert "no `timestamp_column`" in r.message

    def test_empty_feed_fails(self, clean_feed, rules, now):
        r = check_freshness(clean_feed.iloc[0:0], rules.freshness, "scraped_at", now=now)
        assert not r.passed
        assert "empty" in r.message

    def test_naive_now_is_treated_as_utc_not_a_crash(self, clean_feed, rules, now):
        """Regression: a tz-naive ``now`` raised a bare pandas TypeError."""
        naive = now.astimezone(UTC).replace(tzinfo=None)
        aware = check_freshness(clean_feed, rules.freshness, "scraped_at", now=now)
        r = check_freshness(clean_feed, rules.freshness, "scraped_at", now=naive)
        assert r.passed
        assert r.details["age_days"] == aware.details["age_days"]

    def test_naive_now_still_detects_a_stale_feed(self, stale_feed, rules, now):
        naive = now.astimezone(UTC).replace(tzinfo=None)
        r = check_freshness(stale_feed, rules.freshness, "scraped_at", now=naive)
        assert not r.passed
        assert round(r.details["age_days"]) == 33

    def test_non_utc_aware_now_is_converted(self, clean_feed, rules, now):
        """An aware ``now`` in another zone is the same instant, same age."""
        shifted = now.astimezone(timezone(timedelta(hours=1)))
        aware = check_freshness(clean_feed, rules.freshness, "scraped_at", now=now)
        r = check_freshness(clean_feed, rules.freshness, "scraped_at", now=shifted)
        assert r.details["age_days"] == aware.details["age_days"]

    def test_unparseable_timestamps_fail(self, clean_feed, rules, now):
        df = clean_feed.copy()
        df["scraped_at"] = "not-a-date"
        r = check_freshness(df, rules.freshness, "scraped_at", now=now)
        assert not r.passed
        assert "parseable" in r.message


class TestCompleteness:
    def test_complete_column_passes(self, clean_feed):
        r = check_column_completeness(
            clean_feed, "price", ColumnRule(max_null_ratio=0.02), "product_id"
        )
        assert r.passed
        assert r.count == 0

    def test_blank_strings_count_as_missing(self, holey_feed):
        """None, "" and "   " are all a missing price in a scraped feed."""
        r = check_column_completeness(
            holey_feed, "price", ColumnRule(max_null_ratio=0.02), "product_id"
        )
        assert not r.passed
        assert r.count == 3
        assert r.ratio == 3 / 50
        assert set(r.offending) == {"P000", "P001", "P002"}

    def test_tolerance_is_respected(self, holey_feed):
        """The same 6% passes when the rule tolerates 10%."""
        r = check_column_completeness(
            holey_feed, "price", ColumnRule(max_null_ratio=0.10), "product_id"
        )
        assert r.passed
        assert r.count == 3

    def test_absent_column_is_a_failure_not_a_skip(self, clean_feed):
        df = clean_feed.drop(columns=["price"])
        r = check_column_completeness(df, "price", ColumnRule(), "product_id")
        assert not r.passed
        assert r.details["missing_column"] is True

    def test_offending_sample_is_truncated(self, clean_feed):
        df = clean_feed.copy()
        df["price"] = None
        r = check_column_completeness(df, "price", ColumnRule(), "product_id")
        assert r.count == 50
        assert len(r.offending) == 10


class TestBlankDetectionAcrossDtypes:
    """Regression guard for the pandas 2.x `object` vs 3.x `str` dtype split.

    A hand-built frame can be coerced to whichever dtype the code expects, which
    is how the original bug hid: whitespace-only values were only detected when
    the column happened to be `object`. These go through the real read path.
    """

    def test_whitespace_detected_when_read_from_csv(self, tmp_path):
        p = tmp_path / "f.csv"
        p.write_text(
            "product_id,price\n"
            "P1,10.0\n"
            "P2,\n"        # empty -> NaN
            "P3,   \n"     # whitespace -> must still count as missing
            "P4,12.0\n"
        )
        df = pd.read_csv(p)
        r = check_column_completeness(df, "price", ColumnRule(), "product_id")
        assert r.count == 2
        assert set(r.offending) == {"P2", "P3"}

    @pytest.mark.parametrize("dtype", ["object", "string"])
    def test_both_string_dtypes_behave_identically(self, dtype):
        s = pd.Series(["a", "", "   ", None], dtype=dtype)
        df = pd.DataFrame({"col": s})
        r = check_column_completeness(df, "col", ColumnRule())
        assert r.count == 3

    def test_numeric_column_is_unaffected(self):
        df = pd.DataFrame({"price": [1.0, None, 3.0]})
        r = check_column_completeness(df, "price", ColumnRule())
        assert r.count == 1


class TestQuarantineSelection:
    def test_only_quarantine_marked_columns_hold_rows_back(self, clean_feed, rules):
        df = clean_feed.copy()
        df["price"] = df["price"].astype(object)
        df["category"] = df["category"].astype(object)
        df.loc[0, "price"] = None      # price: quarantine=True
        df.loc[1, "category"] = None   # category: quarantine=False

        assert quarantine_ids_for(df, rules.completeness, "product_id") == ["P000"]

    def test_rows_are_held_even_when_the_ratio_check_passes(self, clean_feed, rules):
        """A 2%-tolerant rule passes at 2%, but that row still isn't safe."""
        df = clean_feed.copy()
        df["price"] = df["price"].astype(object)
        df.loc[0, "price"] = None

        r = check_column_completeness(
            df, "price", rules.completeness.columns["price"], "product_id"
        )
        assert r.passed  # 1/50 = 2% == the limit
        assert quarantine_ids_for(df, rules.completeness, "product_id") == ["P000"]

    def test_empty_frame_holds_nothing(self, clean_feed, rules):
        assert quarantine_ids_for(clean_feed.iloc[0:0], rules.completeness, "product_id") == []

    def test_falls_back_to_index_without_an_id_column(self, rules):
        df = pd.DataFrame({"price": [1.0, None], "category": ["a", "b"]})
        assert quarantine_ids_for(df, rules.completeness, None) == [1]


class TestPerSourceCompleteness:
    def test_collapsed_source_fails_though_global_rate_looks_survivable(
        self, multi_source_feed
    ):
        """16% missing feed-wide is one source missing 80% of its own rows."""
        rule = PerSourceCompletenessRule(
            column="price", source_column="source", max_null_ratio=0.10
        )
        r = check_per_source_completeness(multi_source_feed, rule, "product_id")
        assert not r.passed
        assert set(r.details["offending_sources"]) == {"collapsed"}
        assert r.details["offending_sources"]["collapsed"] == {
            "count": 8,
            "total": 10,
            "ratio": 0.8,
        }
        assert r.count == 8
        assert set(r.offending) == {f"C{i:03d}" for i in range(8)}
        assert "collapsed" in r.message

    def test_healthy_feed_passes(self, multi_source_feed):
        rule = PerSourceCompletenessRule(
            column="price", source_column="source", max_null_ratio=0.90
        )
        r = check_per_source_completeness(multi_source_feed, rule, "product_id")
        assert r.passed
        assert r.details["offending_sources"] == {}

    def test_boundary_is_inclusive(self, multi_source_feed):
        at_limit = PerSourceCompletenessRule(
            column="price", source_column="source", max_null_ratio=0.8
        )
        assert check_per_source_completeness(
            multi_source_feed, at_limit, "product_id"
        ).passed

        just_under = PerSourceCompletenessRule(
            column="price", source_column="source", max_null_ratio=0.79
        )
        assert not check_per_source_completeness(
            multi_source_feed, just_under, "product_id"
        ).passed

    def test_min_source_rows_skips_small_cohorts(self):
        """A 2-row source at 100% missing is a small cohort, not a collapse."""
        df = pd.DataFrame(
            {"product_id": ["A", "B"], "price": [None, None], "source": ["tiny", "tiny"]}
        )
        rule = PerSourceCompletenessRule(
            column="price", source_column="source", max_null_ratio=0.0, min_source_rows=5
        )
        r = check_per_source_completeness(df, rule, "product_id")
        assert r.passed

    def test_missing_column_fails_loudly(self, multi_source_feed):
        rule = PerSourceCompletenessRule(column="does_not_exist", source_column="source")
        r = check_per_source_completeness(multi_source_feed, rule, "product_id")
        assert not r.passed
        assert r.details["missing_column"] == "does_not_exist"

    def test_missing_source_column_fails_loudly(self, multi_source_feed):
        rule = PerSourceCompletenessRule(column="price", source_column="does_not_exist")
        r = check_per_source_completeness(multi_source_feed, rule, "product_id")
        assert not r.passed
        assert r.details["missing_column"] == "does_not_exist"

    def test_empty_feed_passes(self, multi_source_feed):
        rule = PerSourceCompletenessRule(
            column="price", source_column="source", max_null_ratio=0.0
        )
        r = check_per_source_completeness(
            multi_source_feed.iloc[0:0], rule, "product_id"
        )
        assert r.passed

    def test_falls_back_to_index_without_an_id_column(self):
        df = pd.DataFrame({"price": [None, None, 10.0], "source": ["a", "a", "a"]})
        rule = PerSourceCompletenessRule(
            column="price", source_column="source", max_null_ratio=0.5
        )
        r = check_per_source_completeness(df, rule, None)
        assert not r.passed
        assert set(r.offending) == {0, 1}

    def test_nan_source_value_is_grouped_and_labelled(self):
        df = pd.DataFrame(
            {
                "product_id": ["A", "B", "C"],
                "price": [None, None, 10.0],
                "source": [None, None, None],
            }
        )
        rule = PerSourceCompletenessRule(
            column="price", source_column="source", max_null_ratio=0.5
        )
        r = check_per_source_completeness(df, rule, "product_id")
        assert not r.passed
        assert "<missing>" in r.details["offending_sources"]
