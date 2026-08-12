"""Tests for CLI argument parsing and command validation."""

import json
import subprocess
import sys
from types import SimpleNamespace

import pytest

from tests.conftest import PYTHON


def test_prune_stale_keeps_mapping_and_removes_runtime_files(tmp_config):
    from serial_mux.cli import _prune_stale

    alias = "die0"
    info_path = tmp_config.run_dir / f"{alias}.json"
    info_path.write_text(json.dumps({
        "alias": alias,
        "device": "/dev/ttyUSB0",
        "baud": 115200,
        "pid": -1,
        "socket": str(tmp_config.sock_dir / f"{alias}.sock"),
    }))
    (tmp_config.run_dir / f"{alias}.pid").touch()
    (tmp_config.sock_dir / f"{alias}.sock").touch()

    _prune_stale(tmp_config)

    assert info_path.exists()
    assert not (tmp_config.run_dir / f"{alias}.pid").exists()
    assert not (tmp_config.sock_dir / f"{alias}.sock").exists()


def test_list_shows_inactive_mapping_as_saved(tmp_config, monkeypatch, capsys):
    from serial_mux import cli

    (tmp_config.run_dir / "die0.json").write_text(json.dumps({
        "alias": "die0",
        "device": "/dev/ttyUSB0",
        "baud": 115200,
        "pid": -1,
        "socket": str(tmp_config.sock_dir / "die0.sock"),
    }))
    monkeypatch.setattr(cli.Config, "load", lambda: tmp_config)

    cli.cmd_list(SimpleNamespace())

    output = capsys.readouterr().out
    assert "die0" in output
    assert "saved" in output


def test_start_by_saved_alias_restores_device(
    tmp_config, tmp_path, monkeypatch
):
    from serial_mux import cli, daemon

    device = tmp_path / "ttyUSB0"
    device.touch()
    (tmp_config.run_dir / "die0.json").write_text(json.dumps({
        "alias": "die0",
        "device": str(device),
        "baud": 9600,
        "pid": -1,
        "socket": str(tmp_config.sock_dir / "die0.sock"),
        "ssh": None,
    }))
    monkeypatch.setattr(cli.Config, "load", lambda: tmp_config)
    started = []
    monkeypatch.setattr(
        daemon,
        "start_daemon",
        lambda *args, **kwargs: started.append((args, kwargs)),
    )
    args = SimpleNamespace(
        device=None,
        baud=None,
        alias="die0",
        foreground=True,
        ssh=None,
    )

    cli.cmd_start(args)

    assert started == [
        ((str(device), 9600, "die0"), {"foreground": True, "ssh_target": None})
    ]


def test_explicit_stop_removes_saved_mapping(
    tmp_config, monkeypatch, capsys
):
    from serial_mux import cli

    info_path = tmp_config.run_dir / "die0.json"
    info_path.write_text(json.dumps({
        "alias": "die0",
        "device": "/dev/ttyUSB0",
        "baud": 115200,
        "pid": -1,
    }))
    monkeypatch.setattr(cli.Config, "load", lambda: tmp_config)

    cli.cmd_stop(SimpleNamespace(alias="die0"))

    assert not info_path.exists()
    assert "removing saved mapping" in capsys.readouterr().out


class TestCLIParsing:
    """Test CLI argument parsing without actually starting daemons."""

    def test_start_no_args_fails(self):
        """start with no device and no --ssh should fail."""
        r = subprocess.run(
            [PYTHON, "-m", "serial_mux.cli", "start"],
            capture_output=True, text=True,
        )
        # Should fail — no device or --ssh
        assert r.returncode != 0

    def test_start_ssh_without_alias_fails(self):
        """start --ssh without --alias should fail."""
        r = subprocess.run(
            [PYTHON, "-m", "serial_mux.cli", "start", "--ssh", "user@host"],
            capture_output=True, text=True,
        )
        assert r.returncode != 0
        assert "alias" in r.stdout.lower() or "alias" in r.stderr.lower()

    def test_help_works(self):
        r = subprocess.run(
            [PYTHON, "-m", "serial_mux.cli", "--help"],
            capture_output=True, text=True,
        )
        assert r.returncode == 0
        assert "serial-mux" in r.stdout.lower() or "serial" in r.stdout.lower()

    def test_start_help(self):
        r = subprocess.run(
            [PYTHON, "-m", "serial_mux.cli", "start", "--help"],
            capture_output=True, text=True,
        )
        assert r.returncode == 0
        assert "--ssh" in r.stdout
        assert "--alias" in r.stdout

    def test_list_no_daemons(self, tmp_path, monkeypatch):
        """list with no daemons or mappings should report an empty state."""
        from pathlib import Path
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        r = subprocess.run(
            [PYTHON, "-m", "serial_mux.cli", "list"],
            capture_output=True, text=True,
            env={**__import__("os").environ, "HOME": str(tmp_path)},
        )
        assert "No daemons" in r.stdout or r.returncode == 0

    def test_ssh_bind_help(self):
        r = subprocess.run(
            [PYTHON, "-m", "serial_mux.cli", "ssh-bind", "--help"],
            capture_output=True, text=True,
        )
        assert r.returncode == 0
        assert "ssh_target" in r.stdout or "SSH" in r.stdout

    def test_serial_bind_help(self):
        r = subprocess.run(
            [PYTHON, "-m", "serial_mux.cli", "serial-bind", "--help"],
            capture_output=True, text=True,
        )
        assert r.returncode == 0
        assert "device" in r.stdout.lower()

    def test_subcommands_exist(self):
        """All expected subcommands show up in help."""
        r = subprocess.run(
            [PYTHON, "-m", "serial_mux.cli", "--help"],
            capture_output=True, text=True,
        )
        for cmd in ["start", "stop", "list", "status", "set-baud",
                     "ssh-bind", "ssh-unbind", "serial-bind", "serial-unbind"]:
            assert cmd in r.stdout, f"Subcommand '{cmd}' not in help output"
