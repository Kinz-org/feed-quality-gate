"""Tests for rule loading and the CLI surface."""

from __future__ import annotations

import json

import pytest
from typer.testing import CliRunner

from feed_quality_gate.cli import app
from feed_quality_gate.rules import RulesError, load_rules

runner = CliRunner()


class TestRuleLoading:
    def test_shipped_example_is_valid(self):
        """rules.example.yaml is documentation - it must actually parse."""
        rules = load_rules("rules.example.yaml")
        assert rules.feed_id == "example-competitor-prices"
        assert rules.timestamp_column == "scraped_at"
        assert rules.completeness.columns["price"].quarantine is True
        assert rules.completeness.columns["category"].quarantine is False

    def test_missing_file_message_names_the_path(self, tmp_path):
        with pytest.raises(RulesError, match="not found"):
            load_rules(tmp_path / "nope.yaml")

    def test_malformed_yaml_is_reported_readably(self, tmp_path):
        p = tmp_path / "bad.yaml"
        p.write_text("feed_id: [unclosed\n")
        with pytest.raises(RulesError, match="not valid YAML"):
            load_rules(p)

    def test_invalid_values_are_reported_by_field(self, tmp_path):
        p = tmp_path / "bad.yaml"
        p.write_text("freshness:\n  max_age_days: -3\n")
        with pytest.raises(RulesError, match="max_age_days"):
            load_rules(p)

    def test_top_level_must_be_a_mapping(self, tmp_path):
        p = tmp_path / "bad.yaml"
        p.write_text("- a\n- b\n")
        with pytest.raises(RulesError, match="mapping"):
            load_rules(p)

    def test_defaults_apply_to_an_empty_file(self, tmp_path):
        p = tmp_path / "empty.yaml"
        p.write_text("")
        rules = load_rules(p)
        assert rules.feed_id == "unnamed-feed"
        assert rules.freshness.enabled is True


def _write_feed(tmp_path, rows):
    import pandas as pd

    p = tmp_path / "feed.csv"
    pd.DataFrame(rows).to_csv(p, index=False)
    return p


def _write_rules(tmp_path, body):
    p = tmp_path / "rules.yaml"
    p.write_text(body)
    return p


FRESH_ROWS = [
    {"product_id": "P1", "price": 10.0, "scraped_at": "2099-01-01T00:00:00Z"},
    {"product_id": "P2", "price": 12.0, "scraped_at": "2099-01-01T00:00:00Z"},
]

RULES_BODY = """
feed_id: cli-test
id_column: product_id
timestamp_column: scraped_at
freshness:
  max_age_days: 3650000
completeness:
  columns:
    price:
      max_null_ratio: 0.0
      severity: fail
"""


class TestReadFeedFormats:
    """`_read_feed` picks its reader by extension; each branch must round-trip."""

    @pytest.mark.parametrize("suffix, lines", [(".json", False), (".jsonl", True)])
    def test_json_feeds_are_read(self, tmp_path, suffix, lines):
        import pandas as pd

        feed = tmp_path / f"feed{suffix}"
        pd.DataFrame(FRESH_ROWS).to_json(feed, orient="records", lines=lines)
        rules = _write_rules(tmp_path, RULES_BODY)
        res = runner.invoke(app, ["check", "-i", str(feed), "-r", str(rules)])
        assert res.exit_code == 0, res.output
        assert "PASSED" in res.output

    def test_unsupported_extension_is_rejected(self, tmp_path):
        feed = tmp_path / "feed.xlsx"
        feed.write_text("irrelevant", encoding="utf-8")
        rules = _write_rules(tmp_path, RULES_BODY)
        res = runner.invoke(app, ["check", "-i", str(feed), "-r", str(rules)])
        assert res.exit_code != 0
        assert "unsupported feed format" in res.output


class TestCli:
    def test_passing_feed_exits_zero(self, tmp_path):
        feed = _write_feed(tmp_path, FRESH_ROWS)
        rules = _write_rules(tmp_path, RULES_BODY)
        res = runner.invoke(app, ["check", "-i", str(feed), "-r", str(rules)])
        assert res.exit_code == 0, res.output
        assert "PASSED" in res.output

    def test_failing_feed_exits_nonzero(self, tmp_path):
        rows = FRESH_ROWS + [
            {"product_id": "P3", "price": None, "scraped_at": "2099-01-01T00:00:00Z"}
        ]
        feed = _write_feed(tmp_path, rows)
        rules = _write_rules(tmp_path, RULES_BODY)
        res = runner.invoke(app, ["check", "-i", str(feed), "-r", str(rules)])
        assert res.exit_code == 1
        assert "FAILED" in res.output

    def test_writes_report_clean_and_quarantine_files(self, tmp_path):
        rows = FRESH_ROWS + [
            {"product_id": "P3", "price": None, "scraped_at": "2099-01-01T00:00:00Z"}
        ]
        feed = _write_feed(tmp_path, rows)
        rules = _write_rules(tmp_path, RULES_BODY)
        report_p = tmp_path / "out" / "report.json"
        clean_p = tmp_path / "out" / "clean.csv"
        held_p = tmp_path / "out" / "held.csv"

        res = runner.invoke(
            app,
            [
                "check", "-i", str(feed), "-r", str(rules),
                "--report", str(report_p),
                "--clean", str(clean_p),
                "--quarantine", str(held_p),
                "--quiet",
            ],
        )
        assert res.exit_code == 1
        payload = json.loads(report_p.read_text())
        assert payload["feed_id"] == "cli-test"
        assert payload["quarantined_ids"] == ["P3"]
        assert "P3" not in clean_p.read_text()
        assert "P3" in held_p.read_text()

    def test_writes_html_report(self, tmp_path):
        rows = FRESH_ROWS + [
            {"product_id": "P3", "price": None, "scraped_at": "2099-01-01T00:00:00Z"}
        ]
        feed = _write_feed(tmp_path, rows)
        rules = _write_rules(tmp_path, RULES_BODY)
        html_p = tmp_path / "out" / "report.html"

        res = runner.invoke(
            app,
            [
                "check", "-i", str(feed), "-r", str(rules),
                "--html", str(html_p),
                "--quiet",
            ],
        )
        assert res.exit_code == 1
        content = html_p.read_text()
        assert content.startswith("<!doctype html")
        assert "cli-test" in content

    def test_bad_rules_exit_two_not_one(self, tmp_path):
        """Config error and data failure must be distinguishable by exit code."""
        feed = _write_feed(tmp_path, FRESH_ROWS)
        rules = _write_rules(tmp_path, "freshness:\n  max_age_days: -1\n")
        res = runner.invoke(app, ["check", "-i", str(feed), "-r", str(rules)])
        assert res.exit_code == 2

    def test_unsupported_format_is_rejected(self, tmp_path):
        feed = tmp_path / "feed.xlsx"
        feed.write_text("nope")
        rules = _write_rules(tmp_path, RULES_BODY)
        res = runner.invoke(app, ["check", "-i", str(feed), "-r", str(rules)])
        assert res.exit_code != 0

    def test_explain_emits_the_resolved_rules(self, tmp_path):
        rules = _write_rules(tmp_path, RULES_BODY)
        res = runner.invoke(app, ["explain", "-r", str(rules)])
        assert res.exit_code == 0
        assert json.loads(res.output)["feed_id"] == "cli-test"
