"""Tests for validate_ssh_target and CLI utility functions."""

import pytest
from pathlib import Path

from serial_mux.daemon import validate_ssh_target
from serial_mux.cli import format_uptime


class TestValidateSSHTarget:
    def test_user_at_host(self):
        ok, err = validate_ssh_target("root@192.168.1.1")
        assert ok is True
        assert err == ""

    def test_user_at_hostname(self):
        ok, err = validate_ssh_target("user@my-server.example.com")
        assert ok is True

    def test_empty_string(self):
        ok, err = validate_ssh_target("")
        assert ok is False
        assert "Empty" in err

    def test_none(self):
        ok, err = validate_ssh_target(None)
        assert ok is False

    def test_whitespace_only(self):
        ok, err = validate_ssh_target("   ")
        assert ok is False

    def test_bare_hostname_found(self, tmp_path, monkeypatch):
        """Bare hostname found in ~/.ssh/config."""
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        ssh_dir = tmp_path / ".ssh"
        ssh_dir.mkdir()
        (ssh_dir / "config").write_text(
            "Host myserver\n"
            "  HostName 10.0.0.1\n"
            "  User root\n"
            "\n"
            "Host another\n"
            "  HostName 10.0.0.2\n"
        )
        ok, err = validate_ssh_target("myserver")
        assert ok is True

    def test_bare_hostname_not_found(self, tmp_path, monkeypatch):
        """Bare hostname not in ~/.ssh/config."""
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        ssh_dir = tmp_path / ".ssh"
        ssh_dir.mkdir()
        (ssh_dir / "config").write_text("Host other\n  HostName 10.0.0.1\n")
        ok, err = validate_ssh_target("nonexistent")
        assert ok is False
        assert "not found" in err

    def test_bare_hostname_no_ssh_config(self, tmp_path, monkeypatch):
        """No ~/.ssh/config file at all."""
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        ok, err = validate_ssh_target("myhost")
        assert ok is False
        assert "not found" in err.lower() or "config" in err.lower()

    def test_multiple_hosts_on_one_line(self, tmp_path, monkeypatch):
        """Host line with multiple aliases."""
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        ssh_dir = tmp_path / ".ssh"
        ssh_dir.mkdir()
        (ssh_dir / "config").write_text("Host alpha beta gamma\n  HostName 10.0.0.1\n")
        assert validate_ssh_target("beta")[0] is True
        assert validate_ssh_target("gamma")[0] is True
        assert validate_ssh_target("delta")[0] is False

    def test_case_insensitive_host_keyword(self, tmp_path, monkeypatch):
        """'host' keyword should be case-insensitive."""
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        ssh_dir = tmp_path / ".ssh"
        ssh_dir.mkdir()
        (ssh_dir / "config").write_text("host myserver\n  HostName 10.0.0.1\n")
        ok, _ = validate_ssh_target("myserver")
        assert ok is True


class TestFormatUptime:
    def test_seconds(self):
        import time
        now = time.time()
        assert format_uptime(now - 30) == "30s"

    def test_minutes(self):
        import time
        now = time.time()
        assert format_uptime(now - 90) == "1m 30s"

    def test_hours(self):
        import time
        now = time.time()
        result = format_uptime(now - 7200)
        assert result.startswith("2h")

    def test_days(self):
        import time
        now = time.time()
        result = format_uptime(now - 90000)
        assert result.startswith("1d")
