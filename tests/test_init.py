"""Tests for `fqg init` / feed_quality_gate.draft."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pandas as pd
import yaml
from typer.testing import CliRunner

from feed_quality_gate.cli import app
from feed_quality_gate.draft import draft_rules, render_yaml
from feed_quality_gate.rules import Rules, load_rules

runner = CliRunner()


def _recent_clean_feed(n: int = 20) -> pd.DataFrame:
    now = datetime.now(UTC) - timedelta(hours=1)
    return pd.DataFrame(
        {
            "sku": [f"S{i:03d}" for i in range(n)],
            "title": [f"Product {i}" for i in range(n)],
            "price": [10.0 + i for i in range(n)],
            "updated_at": [(now - timedelta(minutes=i)).isoformat() for i in range(n)],
        }
    )


class TestDraftRules:
    def test_guesses_id_and_timestamp_on_the_sample_feed(self):
        draft = draft_rules(pd.read_csv("examples/sample_feed.csv"), feed_id="x")
        assert draft.id_column == "product_id"
        assert draft.timestamp_column == "scraped_at"
        assert [c.name for c in draft.columns] == ["name", "price", "category", "description"]

    def test_tolerance_leaves_headroom_above_observed_gaps(self):
        draft = draft_rules(pd.read_csv("examples/sample_feed.csv"))
        by_name = {c.name: c for c in draft.columns}
        price = by_name["price"]  # 2 of 8 blank, one of them whitespace-only
        assert price.blank_ratio == 0.25
        assert price.max_null_ratio > 0.25
        assert price.severity == "warn" and price.quarantine is False
        name = by_name["name"]
        assert name.blank_ratio == 0.0
        assert name.severity == "fail" and name.quarantine is True

    def test_numeric_columns_are_never_taken_for_timestamps(self):
        df = pd.DataFrame({"code": ["a", "b", "c"], "price": [18.0, 19.5, 20.0]})
        assert draft_rules(df).timestamp_column is None

    def test_repeated_or_gappy_columns_are_not_ids(self):
        df = pd.DataFrame({"grp": ["a", "a", "b"], "ref": ["x", None, "z"]})
        assert draft_rules(df).id_column is None

    def test_no_timestamp_disables_freshness(self):
        df = pd.DataFrame({"id": ["a", "b"], "v": ["1", "2"]})
        rules = Rules.from_mapping(yaml.safe_load(render_yaml(draft_rules(df))))
        assert rules.timestamp_column is None
        assert rules.freshness.enabled is False

    def test_awkward_column_names_survive_the_round_trip(self):
        df = pd.DataFrame(
            {"id": ["a", "b"], "price: TND": ["1", "2"], "yes": ["y", "n"], "# note": ["", "x"]}
        )
        rules = Rules.from_mapping(yaml.safe_load(render_yaml(draft_rules(df))))
        assert set(rules.completeness.columns) == {"price: TND", "yes", "# note"}

    def test_rendered_yaml_matches_the_draft_mapping(self):
        draft = draft_rules(pd.read_csv("examples/sample_feed.csv"), feed_id="f")
        assert Rules.from_mapping(yaml.safe_load(render_yaml(draft))) == Rules.from_mapping(
            draft.to_mapping()
        )


class TestInitCli:
    def test_prints_yaml_to_stdout(self):
        res = runner.invoke(app, ["init", "-i", "examples/sample_feed.csv"])
        assert res.exit_code == 0, res.output
        assert "id_column: product_id" in res.output
        assert "timestamp_column: scraped_at" in res.output
        assert "feed_id: sample_feed" in res.output

    def test_written_file_loads_and_explains(self, tmp_path):
        out = tmp_path / "rules.yaml"
        res = runner.invoke(app, ["init", "-i", "examples/sample_feed.csv", "-o", str(out)])
        assert res.exit_code == 0, res.output
        rules = load_rules(out)
        assert set(rules.completeness.columns) == {"name", "price", "category", "description"}
        assert runner.invoke(app, ["explain", "-r", str(out)]).exit_code == 0

    def test_clean_recent_feed_passes_the_rules_drafted_from_it(self, tmp_path):
        feed = tmp_path / "feed.csv"
        _recent_clean_feed().to_csv(feed, index=False)
        out = tmp_path / "rules.yaml"
        assert runner.invoke(app, ["init", "-i", str(feed), "-o", str(out)]).exit_code == 0

        res = runner.invoke(app, ["check", "-i", str(feed), "-r", str(out)])
        assert res.exit_code == 0, res.output
        assert "gate:   PASSED" in res.output

    def test_drafted_rules_still_catch_a_new_gap(self, tmp_path):
        feed = tmp_path / "feed.csv"
        df = _recent_clean_feed()
        df.to_csv(feed, index=False)
        out = tmp_path / "rules.yaml"
        runner.invoke(app, ["init", "-i", str(feed), "-o", str(out)])

        df.loc[0, "title"] = ""  # 1 of 20 = 5%, above the 2% allowance
        df.to_csv(feed, index=False)
        res = runner.invoke(app, ["check", "-i", str(feed), "-r", str(out)])
        assert res.exit_code == 1, res.output

    def test_refuses_to_overwrite_without_force(self, tmp_path):
        out = tmp_path / "rules.yaml"
        out.write_text("feed_id: keep-me\n")
        res = runner.invoke(app, ["init", "-i", "examples/sample_feed.csv", "-o", str(out)])
        assert res.exit_code == 2
        assert out.read_text() == "feed_id: keep-me\n"

        res = runner.invoke(
            app, ["init", "-i", "examples/sample_feed.csv", "-o", str(out), "--force"]
        )
        assert res.exit_code == 0, res.output
        assert load_rules(out).id_column == "product_id"

    def test_feed_id_option(self):
        res = runner.invoke(
            app, ["init", "-i", "examples/sample_feed.csv", "--feed-id", "my-feed"]
        )
        assert "feed_id: my-feed" in res.output
