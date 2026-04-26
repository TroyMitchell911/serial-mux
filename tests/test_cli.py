"""Tests for CLI argument parsing and command validation."""

import subprocess
import sys

import pytest

from tests.conftest import PYTHON


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
        """list with no daemons should print 'No daemons running'."""
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
