"""Smoke tests for the typer CLI surface."""

from __future__ import annotations

from typer.testing import CliRunner

from ibkr_vol_screener.cli import app

runner = CliRunner()


def test_help_works():
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0
    out = result.stdout
    assert "once" in out
    assert "watch" in out
    assert "scanner-params" in out
    assert "config-example" in out


def test_config_example_prints_toml():
    result = runner.invoke(app, ["config-example"])
    assert result.exit_code == 0
    out = result.stdout
    assert "[connection]" in out
    assert "host =" in out
    assert "STK.US.MAJOR" in out
    assert "STK.US.MINOR" in out


def test_once_dry_run_does_not_connect():
    result = runner.invoke(app, ["once", "--dry-run", "--markets", "us_major"])
    assert result.exit_code == 0
    # Dry run should print the resolved markets config without connecting.
    assert "us_major" in result.stdout
