"""Tests for evaluation, scoring and the clean/quarantined split."""

from __future__ import annotations

import json
import re
from datetime import UTC

import pandas as pd

from feed_quality_gate.gate import evaluate, gate
from feed_quality_gate.models import CheckResult, FeedReport, Severity
from feed_quality_gate.report import exit_code, to_console, to_html, to_json, to_markdown
from feed_quality_gate.rules import Rules


class TestEvaluate:
    def test_naive_now_is_accepted_and_reported_as_utc(self, clean_feed, rules, now):
        """Regression: evaluate(now=<naive>) crashed in the freshness check, and
        to_dict() would have read the naive value as machine-local time."""
        naive = now.astimezone(UTC).replace(tzinfo=None)
        report = evaluate(clean_feed, rules, now=naive)
        assert report.gate_passed
        assert report.evaluated_at == now
        assert report.evaluated_at.tzinfo is not None
        assert report.to_dict()["evaluated_at"] == now.astimezone(UTC).isoformat()

    def test_clean_feed_scores_full_marks(self, clean_feed, rules, now):
        report = evaluate(clean_feed, rules, now=now)
        assert report.gate_passed
        assert report.score == 100
        assert report.failures == []
        assert report.row_count == 50

    def test_stale_feed_fails_the_gate_though_every_row_is_valid(
        self, stale_feed, rules, now
    ):
        """The headline case: row-level checks all pass, the feed is worthless."""
        report = evaluate(stale_feed, rules, now=now)
        assert not report.gate_passed
        assert [r.name for r in report.failures] == ["freshness"]
        assert report.quarantined_ids == []
        assert report.score == 70  # one FAIL-severity check

    def test_missing_prices_fail_and_quarantine(self, holey_feed, rules, now):
        report = evaluate(holey_feed, rules, now=now)
        assert not report.gate_passed
        assert "completeness:price" in [r.name for r in report.failures]
        assert set(report.quarantined_ids) == {"P000", "P001", "P002"}

    def test_warn_severity_lowers_score_without_blocking(self, clean_feed, rules, now):
        """A category gap is worth knowing about; it must not stop a pipeline."""
        df = clean_feed.copy()
        df["category"] = df["category"].astype(object)
        df.loc[:9, "category"] = None  # 10/50 = 20% > the 5% warn limit

        report = evaluate(df, rules, now=now)
        assert report.gate_passed          # no FAIL-severity failure
        assert report.score == 90          # but the WARN cost 10
        assert exit_code(report) == 0
        assert [r.severity for r in report.failures] == [Severity.WARN]

    def test_every_check_failing_costs_its_weight(self, clean_feed, rules, now):
        """Drop all three columns: freshness(30) + price(30) + category(10)."""
        df = clean_feed.drop(columns=["price", "category", "scraped_at"])
        report = evaluate(df, rules, now=now)
        assert not report.gate_passed
        assert len(report.failures) == 3
        assert report.score == 30

    def test_score_floors_at_zero(self, now):
        """Penalties past 100 clamp rather than going negative."""
        report = FeedReport(
            feed_id="f",
            evaluated_at=now,
            row_count=1,
            results=[
                CheckResult(name=f"c{i}", passed=False, severity=Severity.FAIL)
                for i in range(4)  # 4 x 30 = 120 > 100
            ],
        )
        assert report.score == 0
        assert not report.gate_passed


class TestPerSourceCompletenessInEvaluate:
    def test_off_by_default(self, multi_source_feed, rules, now):
        """The default `rules` fixture has per_source_completeness disabled."""
        report = evaluate(multi_source_feed, rules, now=now)
        assert "per_source_completeness" not in [r.name for r in report.results]

    def test_catches_a_collapsed_source_a_feed_wide_tolerance_would_miss(
        self, multi_source_feed, now
    ):
        rules = Rules.from_mapping(
            {
                "feed_id": "multi-source",
                "id_column": "product_id",
                "timestamp_column": "scraped_at",
                "freshness": {"max_age_days": 7},
                "completeness": {
                    "columns": {"price": {"max_null_ratio": 0.20, "severity": "fail"}}
                },
                "per_source_completeness": {
                    "enabled": True,
                    "column": "price",
                    "source_column": "source",
                    "max_null_ratio": 0.10,
                    "severity": "fail",
                },
            }
        )
        report = evaluate(multi_source_feed, rules, now=now)
        failure_names = [r.name for r in report.failures]

        # the feed-wide rate (16%) clears the 20% column tolerance, so only
        # the per-source check - which sees the collapsed source directly -
        # catches it.
        assert "completeness:price" not in failure_names
        assert "per_source_completeness" in failure_names
        assert not report.gate_passed


class TestGateSplit:
    def test_split_separates_held_rows(self, holey_feed, rules, now):
        report, clean, held = gate(holey_feed, rules, now=now)
        assert len(clean) == 47
        assert len(held) == 3
        assert len(clean) + len(held) == len(holey_feed)
        assert set(held["product_id"]) == set(report.quarantined_ids)

    def test_clean_rows_survive_a_failed_gate(self, holey_feed, rules, now):
        """A gate, not a trip-wire: good rows remain usable."""
        report, clean, _ = gate(holey_feed, rules, now=now)
        assert not report.gate_passed
        assert not clean.empty

    def test_stale_feed_is_not_split(self, stale_feed, rules, now):
        """Freshness is feed-level - there is no such thing as a stale row."""
        _, clean, held = gate(stale_feed, rules, now=now)
        assert len(clean) == 50
        assert held.empty

    def test_split_does_not_mutate_the_input(self, holey_feed, rules, now):
        before = holey_feed.copy()
        gate(holey_feed, rules, now=now)
        assert holey_feed.equals(before)

    def test_duplicated_id_keeps_the_good_row_clean(self, clean_feed, rules, now):
        """``id_column`` names offending rows; it is not a uniqueness promise.

        Two rows share ``P001``: one has no price, the other is fully valid.
        Only the blank one may be held - the split used to match quarantined
        *ids* back against the frame, which dragged the valid ``P001`` into
        quarantine too and dropped it from ``clean``.
        """
        feed = pd.concat(
            [clean_feed, clean_feed.iloc[[1]].assign(price=None)], ignore_index=True
        )
        report, clean, held = gate(feed, rules, now=now)
        assert report.quarantined_ids == ["P001"]
        assert len(held) == 1
        assert held["price"].isna().all()
        assert len(clean) == len(clean_feed)
        assert (clean["product_id"] == "P001").sum() == 1
        assert clean.loc[clean["product_id"] == "P001", "price"].notna().all()

    def test_duplicated_index_without_id_column_keeps_the_good_row_clean(
        self, clean_feed, rules, now
    ):
        """Same defect via the index fallback: a non-unique index label."""
        rules_no_id = rules.model_copy(update={"id_column": None})
        feed = clean_feed.copy()
        feed.index = [0, 0] + list(range(2, len(feed)))
        feed.iloc[1, feed.columns.get_loc("price")] = None
        _, clean, held = gate(feed, rules_no_id, now=now)
        assert len(held) == 1
        assert len(clean) == len(feed) - 1
        assert held["price"].isna().all()


class TestReportRendering:
    def test_json_is_valid_and_carries_the_contract_fields(
        self, holey_feed, rules, now
    ):
        payload = json.loads(to_json(evaluate(holey_feed, rules, now=now)))
        for key in (
            "feed_id",
            "evaluated_at",
            "row_count",
            "score",
            "gate_passed",
            "quarantined_count",
            "quarantined_ids",
            "results",
        ):
            assert key in payload
        assert payload["feed_id"] == "test-feed"
        assert payload["gate_passed"] is False
        assert payload["quarantined_count"] == 3

    def test_console_and_markdown_render(self, stale_feed, rules, now):
        report = evaluate(stale_feed, rules, now=now)
        console = to_console(report)
        assert "FAILED" in console
        assert "freshness" in console
        assert "| check |" in to_markdown(report)

    def test_exit_code_tracks_the_verdict(self, clean_feed, stale_feed, rules, now):
        assert exit_code(evaluate(clean_feed, rules, now=now)) == 0
        assert exit_code(evaluate(stale_feed, rules, now=now)) == 1

    def test_html_is_a_self_contained_page_carrying_the_verdict(
        self, holey_feed, rules, now
    ):
        report = evaluate(holey_feed, rules, now=now)
        page = to_html(report)
        assert page.startswith("<!doctype html")
        assert "</html>" in page
        assert str(report.score) in page
        assert "FAILED" in page
        assert "completeness:price" in page
        assert "P000" in page  # a quarantined row id

    def test_html_escapes_untrusted_feed_content(self, holey_feed, rules, now):
        """A check's message/offending sample can be arbitrary feed data.

        Nothing about `evaluate()` sanitises it, so `to_html()` must, or a
        crafted row turns the report itself into an XSS vector.
        """
        report = evaluate(holey_feed, rules, now=now)
        report.results.append(
            CheckResult(
                name="probe",
                passed=False,
                severity=Severity.WARN,
                message="<script>alert(1)</script>",
                offending=["<img src=x onerror=alert(1)>"],
            )
        )
        page = to_html(report)
        assert "<script>" not in page
        assert "<img " not in page
        assert "&lt;script&gt;" in page
        assert "&lt;img src=x onerror=alert(1)&gt;" in page

    def test_markdown_escapes_pipes_and_newlines_in_feed_derived_text(
        self, holey_feed, rules, now
    ):
        """A `|` or newline from feed data must not corrupt the table.

        `per_source_completeness` builds its message from the feed's own
        source-column values, so an untrusted value containing a literal `|`
        (e.g. a source name like "Acme | Legacy Feed") would otherwise split
        one cell into extra columns, and a newline would end the row early -
        corrupting every row rendered after it. Same class of untrusted-data
        problem `to_html` already guards against in the test above.
        """
        report = evaluate(holey_feed, rules, now=now)
        report.results.append(
            CheckResult(
                name="probe",
                passed=False,
                severity=Severity.WARN,
                message="1 source(s) exceed tolerance: Acme | Legacy Feed\nSecond line",
            )
        )
        md = to_markdown(report)

        # Split on *unescaped* pipes only - an escaped `\|` is one cell's
        # content, not a column boundary, so a naive `line.split("|")` would
        # wrongly count it as one.
        cell_split = re.compile(r"(?<!\\)\|")
        table_rows = [line for line in md.splitlines() if line.startswith("|")]
        header_width = len(cell_split.split(table_rows[0]))
        for line in table_rows:
            assert len(cell_split.split(line)) == header_width
        assert "Second line" in md
        assert "Acme \\| Legacy Feed" in md
        # No bare newline inside the table - it would end the row early and
        # corrupt whatever markdown renders after it.
        assert "\n" not in table_rows[-1]
