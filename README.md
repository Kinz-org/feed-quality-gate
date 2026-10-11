# feed-quality-gate (`fqg`)

[![CI](https://github.com/Kinz-org/feed-quality-gate/actions/workflows/ci.yml/badge.svg)](https://github.com/Kinz-org/feed-quality-gate/actions/workflows/ci.yml)

A declarative quality-and-freshness gate for tabular product/price feeds. Point
it at a feed and a rules file; it profiles the feed, scores it, splits the safe
rows from the unsafe ones, and exits non-zero when the feed should not be
consumed.

Nothing in this repository is specific to any one dataset. Column names come
from the rules file, never from the code, and no real catalogue, competitor or
customer data is committed here.

## Why this exists

In a scrape → normalise → decide pipeline, there is usually nothing between the
scraper and the things that trust it. Two failure modes follow, and both are
silent:

- **A feed goes stale and nothing says so.** Every row is individually valid, so
  every row-level check passes, while dashboards keep rendering week-over-week
  deltas against a frozen baseline. Freshness is a property of the *feed*, not
  of any row, so row-level validation structurally cannot catch it.
- **Bad rows pass a shallow assertion.** A `price > 0` check says nothing about
  a row whose price is the empty string, whose category is missing, or which was
  never a product at all.

This gate is the missing layer. It runs *before* the consumer, treats the batch
as the unit of analysis, and emits a machine-readable verdict rather than a
boolean.

## Install

```bash
pip install -e ".[dev]"
```

Python 3.11+. Core dependencies are `pandas`, `pydantic`, `PyYAML` and `typer`.
Extras: `[parquet]` for Parquet feeds, `[astk]` (see below), `[dev]` for tests
and linting.

## Try it

The repo ships a deliberately broken example feed - stale, with two missing
prices (one empty, one whitespace-only) and one missing category:

```bash
fqg check -i examples/sample_feed.csv -r rules.example.yaml
```

Real output:

```
feed:   example-competitor-prices
rows:   8
score:  30/100
gate:   FAILED
held:   2 row(s) quarantined

  FAIL [fail] freshness: newest row is 34.1d old (limit 7d, newest 2026-07-30T17:07:00+00:00)
  FAIL [fail] completeness:price: 2/8 rows (25.0%) missing 'price' (limit 2.0%)
         e.g. P003, P005
  FAIL [warn] completeness:category: 1/8 rows (12.5%) missing 'category' (limit 5.0%)
         e.g. P004
  PASS completeness:description: 0/8 rows (0.0%) missing 'description' (limit 10.0%)
```

Exit code is `1`. The freshness age grows as real time passes, so the exact
number above will differ - the verdict will not.

Write the artifacts out:

```bash
fqg check -i examples/sample_feed.csv -r rules.example.yaml \
  --report report.json --clean clean.csv --quarantine held.csv --html report.html
```

`--html` writes a single, dependency-free page - a score gauge plus a
colour-coded PASS/FAIL/WARN table per check - for anyone who would rather open
a file than pipe JSON through `jq`.

`fqg explain -r rules.example.yaml` prints the rule set as the gate actually
resolved it, defaults included.

## Use it as a library

```python
import pandas as pd
from feed_quality_gate import gate, load_rules

rules = load_rules("rules.yaml")
report, clean_df, held_df = gate(pd.read_csv("feed.csv"), rules)

if not report.gate_passed:
    log.warning(report.to_json())

load_into_warehouse(clean_df)   # the good rows are still usable
```

## Concepts

| Concept | What it is |
| --- | --- |
| **Feed** | Any tabular batch of rows - a DataFrame, CSV, Parquet or JSON file. |
| **Rules** | A YAML file naming the id/timestamp columns and the per-column tolerances. |
| **Check** | A pure function `(df, rule) -> CheckResult`. No I/O, no globals. |
| **Severity** | `fail` blocks the gate, `warn` only lowers the score, `info` is advisory. |
| **FeedReport** | The verdict: results, score out of 100, `gate_passed`, quarantined ids. |
| **Quarantine** | Row-level hold-back, independent of whether the gate passed. |

### Two questions, deliberately kept separate

`evaluate()` answers *"is this feed trustworthy?"* - a feed-level verdict.
`gate()` also answers *"which rows are safe?"* - a row-level split.

Conflating them is what turns a gate into a trip-wire. A feed can be 99%
complete and still contain rows with no price; those rows must not reach a
calculation just because their cohort was small enough to clear the threshold.
So quarantine selection ignores whether the ratio check passed, and the clean
partition is produced even when the overall gate fails.

### Severity and exit codes

| Exit | Meaning |
| --- | --- |
| `0` | Gate passed. `warn` failures may still be present in the report. |
| `1` | A `fail`-severity check failed. |
| `2` | The rules file is missing or invalid - a config error, not a data verdict. |

`warn` deliberately does not change the exit code. A gate that breaks the build
over a cosmetic gap gets switched off, and a gate that is switched off catches
nothing.

## Rules

See [`rules.example.yaml`](rules.example.yaml), which is exercised by the test
suite so it cannot drift out of date. Onboarding a new feed means writing a
YAML file, never editing this package.

## What v1 does and does not do

**Ships now:** `freshness` (feed-level staleness), `completeness` (per-column
missingness, treating `null`, `""` and whitespace alike), `per_source_completeness`
(the same tolerance applied per source, so one collapsed source cannot hide
behind a healthy global average), scoring, quarantine partitioning,
JSON/console/Markdown reports, and the `fqg` CLI.

**Not built yet** - the gaps below are ranked, not hidden:

- stuck-value cluster detection (N rows sharing one suspicious price)
- non-product row detection
- zero-yield source detection
- absolute value-range bounds
- alerting integration and historical quality tracking

There is **no** live HTTP probing, **no** database writes, **no** migrations,
and **no** transformation of any kind. Normalisation and matching belong to the
consumer; this gate validates and never transforms.

## Relationship to `analytics-service-toolkit` (`astk`)

`astk` is the shared operational substrate - settings, DB engines, Slack
alerting, dashboard chrome. This gate is a natural consumer of it, and the
backlog items that need config loading, a DB source adapter or alerting are
written against it.

It is deliberately an **optional** extra (`pip install ".[astk]"`) rather than a
core dependency, for one practical reason: `astk` is a private repository, so a
hard dependency would mean CI could not install this package without
credentials. Core stays installable by anyone; the integration seam is opt-in.

## Development

```bash
pip install -e ".[dev]"
pytest          # 56 tests
ruff check .
```

Tests use synthetic fixtures exclusively and make no network calls.

CI runs `ruff` and `pytest` on Python 3.11 and 3.12, then smoke-tests the CLI
against the intentionally-broken example feed and fails if the gate passes it -
see [`.github/workflows/ci.yml`](.github/workflows/ci.yml). The `[astk]` extra is
deliberately not installed in CI (private repo, no credentials).

## Licence

MIT - see [LICENSE](LICENSE).
