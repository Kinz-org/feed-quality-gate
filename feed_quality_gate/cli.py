"""`fqg` - run the gate from a shell, a CI step, or an Airflow BashOperator.

The important behaviour is the exit code: `fqg check` exits non-zero when a
FAIL-severity check fails, so it can be dropped in front of a sync job without
any glue code.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd
import typer

from . import report as report_mod
from .draft import draft_rules, render_yaml
from .gate import gate
from .rules import RulesError, load_rules

app = typer.Typer(
    add_completion=False,
    help="Quality-and-freshness gate for tabular product/price feeds.",
)


def _read_feed(path: Path) -> pd.DataFrame:
    """Load a feed by extension. CSV and Parquet cover every current producer."""
    suffix = path.suffix.lower()
    if suffix in {".csv", ".txt"}:
        return pd.read_csv(path)
    if suffix in {".parquet", ".pq"}:
        return pd.read_parquet(path)
    if suffix in {".json", ".jsonl"}:
        return pd.read_json(path, lines=suffix == ".jsonl")
    raise typer.BadParameter(f"unsupported feed format: {path.suffix or '(none)'}")


@app.command()
def check(
    input: Path = typer.Option(..., "--input", "-i", help="Feed file (csv/parquet/json)."),
    rules: Path = typer.Option(..., "--rules", "-r", help="Rules YAML file."),
    report: Path | None = typer.Option(None, "--report", help="Write the JSON report here."),
    clean: Path | None = typer.Option(None, "--clean", help="Write passing rows here (CSV)."),
    quarantine: Path | None = typer.Option(None, "--quarantine", help="Held-back rows (CSV)."),
    html: Path | None = typer.Option(None, "--html", help="Write a self-contained HTML report."),
    quiet: bool = typer.Option(False, "--quiet", "-q", help="Suppress the console summary."),
) -> None:
    """Evaluate a feed and exit non-zero if it fails the gate."""
    try:
        ruleset = load_rules(rules)
    except RulesError as exc:
        typer.secho(str(exc), fg=typer.colors.RED, err=True)
        raise typer.Exit(2) from exc

    df = _read_feed(input)
    result, clean_df, quarantined_df = gate(df, ruleset)

    if report:
        report.parent.mkdir(parents=True, exist_ok=True)
        report.write_text(report_mod.to_json(result), encoding="utf-8")
    if clean:
        clean.parent.mkdir(parents=True, exist_ok=True)
        clean_df.to_csv(clean, index=False)
    if quarantine:
        quarantine.parent.mkdir(parents=True, exist_ok=True)
        quarantined_df.to_csv(quarantine, index=False)
    if html:
        html.parent.mkdir(parents=True, exist_ok=True)
        html.write_text(report_mod.to_html(result), encoding="utf-8")

    if not quiet:
        typer.echo(report_mod.to_console(result))

    raise typer.Exit(report_mod.exit_code(result))


@app.command()
def explain(
    rules: Path = typer.Option(..., "--rules", "-r", help="Rules YAML file."),
) -> None:
    """Print the active rule set as the gate actually understands it."""
    try:
        ruleset = load_rules(rules)
    except RulesError as exc:
        typer.secho(str(exc), fg=typer.colors.RED, err=True)
        raise typer.Exit(2) from exc

    typer.echo(ruleset.model_dump_json(indent=2))


@app.command()
def init(
    input: Path = typer.Option(..., "--input", "-i", help="Sample feed (csv/parquet/json)."),
    output: Path | None = typer.Option(
        None, "--output", "-o", help="Write the rules here instead of printing them."
    ),
    feed_id: str | None = typer.Option(
        None, "--feed-id", help="feed_id to write. Defaults to the input file's name."
    ),
    force: bool = typer.Option(False, "--force", help="Overwrite an existing --output file."),
) -> None:
    """Draft a rules file from a sample of the feed, for a human to review."""
    if output and output.exists() and not force:
        typer.secho(
            f"{output} already exists - pass --force to overwrite it",
            fg=typer.colors.RED,
            err=True,
        )
        raise typer.Exit(2)

    draft = draft_rules(_read_feed(input), feed_id=feed_id or input.stem)
    text = render_yaml(draft, source=input.name)

    if output:
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(text, encoding="utf-8")
        typer.echo(f"wrote {output} - review it, then run: fqg check -i {input} -r {output}")
    else:
        typer.echo(text, nl=False)


def main() -> None:  # pragma: no cover - thin console-script wrapper
    app()


if __name__ == "__main__":  # pragma: no cover
    sys.exit(app())
