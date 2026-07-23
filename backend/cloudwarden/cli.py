"""Command-line entrypoint: ``python -m cloudwarden.cli <command>``.

Commands: initdb | run [--mock] | scheduler | api. This is the container
entrypoint (see backend/Dockerfile) and the local dev driver (see Makefile).
"""

from __future__ import annotations

import logging

import typer

app = typer.Typer(add_completion=False, help="CloudWarden CLI")


def _setup_logging() -> None:
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
    )


@app.command()
def initdb() -> None:
    """Create/upgrade the database schema (tables, hypertables, views)."""
    _setup_logging()
    from .storage.db import init_db

    init_db()
    typer.echo("database ready")


@app.command()
def run(mock: bool = typer.Option(False, "--mock", help="Use fixtures instead of Azure")) -> None:
    """Run the pipeline once per enabled subscription (collect -> ... -> store)."""
    _setup_logging()
    from .orchestrator import run_all_subscriptions

    result = run_all_subscriptions(mock=True if mock else None)
    typer.echo(f"run complete: {result}")


@app.command(name="run-policies")
def run_policies_cmd(
    mock: bool = typer.Option(False, "--mock", help="Use fixtures instead of Azure"),
) -> None:
    """Execute every enabled policy against every enabled subscription (pull mode)."""
    _setup_logging()
    from .orchestrator import run_all_policies

    result = run_all_policies(mock=True if mock else None)
    typer.echo(f"policy run complete: {result}")


@app.command(name="evaluate-iac")
def evaluate_iac_cmd(
    plan: str = typer.Argument(..., help="Path to a Terraform plan JSON (terraform show -json)"),
    fail_on: str = typer.Option(
        None,
        "--fail-on",
        help="Only fail (exit 1) at/above this severity: low|medium|high|critical",
    ),
) -> None:
    """Shift-left: evaluate enabled policies against a Terraform plan; exit non-zero on
    a violation so a bad plan blocks the PR/CI **before** anything is provisioned."""
    _setup_logging()
    import json

    from .custodian import shiftleft
    from .storage import repository as repo
    from .storage.db import init_db, session_scope

    try:
        with open(plan, encoding="utf-8") as handle:
            plan_json = json.load(handle)
    except (OSError, json.JSONDecodeError) as exc:
        typer.echo(f"error: cannot read plan {plan!r}: {exc}", err=True)
        raise typer.Exit(code=2) from exc

    init_db()
    with session_scope() as session:
        policies = repo.list_policies(session, enabled_only=True)

    try:
        result = shiftleft.evaluate_plan(plan_json, policies)
    except shiftleft.ShiftLeftError as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(code=2) from exc

    for match in result.matches:
        typer.echo(f"{match.severity.upper():<8} {match.policy}  {match.resource_address}")
    typer.echo(
        f"{len(result.matches)} violation(s) across {result.evaluated} resource(s); "
        f"{len(result.skipped)} unmapped type(s) skipped"
    )
    raise typer.Exit(code=result.exit_code(fail_on))


@app.command()
def scheduler() -> None:
    """Run the pipeline and policy execution on their own intervals."""
    _setup_logging()
    from .scheduler import run_scheduler

    run_scheduler()


@app.command()
def api(host: str = "0.0.0.0", port: int = 8000) -> None:
    """Serve the FastAPI app via uvicorn."""
    _setup_logging()
    import uvicorn

    uvicorn.run("cloudwarden.api.main:app", host=host, port=port, log_level="info")


if __name__ == "__main__":
    app()
