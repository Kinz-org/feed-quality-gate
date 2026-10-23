# Improvements backlog

Findings from reading `feed_quality_gate/`, `tests/`, `examples/`,
`rules.example.yaml`, `README.md` and `.github/workflows/ci.yml` on 2026-10-09.
Each item was verified against the actual code, not assumed from an older
note. Tick an item in place (`[x] ... (done YYYY-MM-DD)`) when it ships, in
the same PR that fixes it.

- [x] **`to_markdown()` doesn't escape feed-derived text, so a `|` or newline
  corrupts the table.** (done 2026-10-09) `per_source_completeness`'s message
  is built straight from the feed's own source-column values
  (`checks.py::check_per_source_completeness`), so a source name containing a
  literal `|` (e.g. `"Acme | Legacy Feed"`) splits one logical cell into
  extra columns, and a newline ends the row early and corrupts every row
  rendered after it. `to_html()` already escapes this exact class of
  untrusted content for this exact reason (see its docstring and
  `test_html_escapes_untrusted_feed_content`); `to_markdown()`, whose own
  docstring says it's "for pasting into a findings file or a PR body," had no
  equivalent guard. Fixed with a `_md_escape()` helper in
  `feed_quality_gate/report.py` applied to each row's `name`/`message`.
  Verify: `pytest tests/test_gate.py -k test_markdown_escapes` - the new test
  was confirmed to fail against the pre-fix code (`git stash` the fix and
  re-run) before being restored.

- [x] **`check_freshness`/`evaluate(..., now=...)` crashes with a raw
  `TypeError` on a timezone-naive `now`.** (done 2026-10-14) Fixed with
  `checks.py::as_utc()`, used by both `check_freshness` and `evaluate()`: a
  naive `now` is treated as UTC (the same assumption `pd.to_datetime(...,
  utc=True)` already makes for the feed's naive timestamps), an aware one is
  converted to UTC. `evaluate()` normalising too also stops
  `FeedReport.to_dict()` reading a naive `evaluated_at` as machine-local time.
  Regression tests in `tests/test_checks.py::TestFreshness` and
  `tests/test_gate.py::TestEvaluate` fail (3 of 4) against the pre-fix code.
  Original finding: `now` is typed `datetime | None`
  with nothing in the signature or docstring saying it must be tz-aware, but
  `checks.py::check_freshness` does `pd.Timestamp(now).tz_convert("UTC")`,
  which raises `TypeError: Cannot convert tz-naive Timestamp, use
  tz_localize to localize` for any naive value - a confusing crash instead of
  a readable error, for a parameter every caller of the public `evaluate()`/
  `gate()` API can pass. Reproduced directly:
  `check_freshness(df, FreshnessRule(), "scraped_at", now=datetime(2026, 1, 2))`
  (no `tzinfo`) raises instead of returning a `CheckResult`. Fix should
  either localize a naive `now` to UTC with a clear assumption stated, or
  raise a `RulesError`-style message naming the problem. Verify: add a test
  passing a naive `now` and assert a clean result or a readable error, not a
  bare pandas `TypeError`.

- [ ] **`stuck_value_cluster` detection is still not built.** Verified by
  reading `feed_quality_gate/checks.py` end to end and grepping the whole
  tree for "stuck"/"cluster" - the only occurrence is the one line in
  `README.md`'s "Not built yet" list. This is a real, not stale, backlog
  item: it would flag N rows sharing one suspicious value (e.g. the same
  price repeated across unrelated products - a common scraper-fallback
  symptom), which none of the three shipped checks (`freshness`,
  `completeness`, `per_source_completeness`) catch. Verify once built: a
  fixture with, say, 20 distinct products all priced at exactly the same
  value should fail the new check while passing every existing one.

- [ ] **`_read_feed`'s `.parquet`/`.pq` and `.json`/`.jsonl` branches have
  zero test coverage.** `feed_quality_gate/cli.py::_read_feed` has four
  format branches; `tests/test_rules_and_cli.py` only ever writes and reads
  back a `.csv` feed (`_write_feed` always writes `pd.DataFrame.to_csv`). A
  regression in the JSON or Parquet branch - wrong `lines=` value, wrong
  pandas call - would ship silently. (Parquet needs the optional `[parquet]`
  extra; CI would need to install it for that one test, same pattern the
  Shopify-style optional-extra problem in this portfolio's sibling repos
  solves by scoping the extra to one job/step.) Verify: add CLI tests that
  round-trip a feed through `.json` and (marked to skip without `pyarrow`)
  `.parquet`.
  *Partly done 2026-10-23:* `.json`, `.jsonl` and the unsupported-extension
  error are now covered (`TestReadFeedFormats`); `.parquet` is still open.

- [ ] **`gate()`'s row split quarantines every row sharing a duplicated
  `id_column` value, not just the offending one.** `gate()` selects bad rows
  with `df[id_col].isin(report.quarantined_ids)` (`feed_quality_gate/gate.py`).
  Reproduced: a feed with two rows both `product_id="P1"`, one with a null
  price (correctly quarantined) and one fully valid, puts *both* rows in
  `held` and *neither* in `clean` - the valid `P1` row is wrongly excluded
  from the clean partition solely because it shares an id with a bad row.
  `rules.example.yaml` documents `id_column` only as "used to name offending
  rows," not as a uniqueness assumption, so this is a real, silent
  correctness gap for any feed whose id column isn't actually unique. Verify:
  a fixture with a duplicated id where only one of the two rows is bad should
  keep the good one in `clean`, e.g. by splitting on index positions matched
  to quarantined index values rather than quarantined id values.
