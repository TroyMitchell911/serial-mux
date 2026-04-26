"""Tests for serial_mux.config — defaults, yaml loading, directory creation."""

import pytest
from pathlib import Path
from serial_mux.config import Config


class TestConfigDefaults:
    def test_default_values(self):
        cfg = Config()
        assert cfg.log_retention_days == 7
        assert cfg.default_baud == 115200
        assert cfg.scrollback_lines == 5000
        assert cfg.ssh_connect_timeout == 3
        assert cfg.ssh_probe_timeout == 5

    def test_derived_paths(self):
        cfg = Config()
        assert cfg.run_dir == cfg.base_dir / "run"
        assert cfg.sock_dir == cfg.base_dir / "sock"
        assert cfg.logs_dir == cfg.base_dir / "logs"


class TestConfigLoad:
    def test_load_without_file(self, tmp_path, monkeypatch):
        """Config.load() works even without a config file."""
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        cfg = Config.load()
        assert cfg.default_baud == 115200

    def test_load_with_yaml(self, tmp_path, monkeypatch):
        """Config.load() reads values from config.yaml."""
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        config_dir = tmp_path / ".config" / "serial-mux"
        config_dir.mkdir(parents=True)
        config_file = config_dir / "config.yaml"
        config_file.write_text(
            "log_retention_days: 14\n"
            "default_baud: 9600\n"
            "scrollback_lines: 1000\n"
            "ssh_connect_timeout: 10\n"
            "ssh_probe_timeout: 15\n"
        )
        cfg = Config.load()
        assert cfg.log_retention_days == 14
        assert cfg.default_baud == 9600
        assert cfg.scrollback_lines == 1000
        assert cfg.ssh_connect_timeout == 10
        assert cfg.ssh_probe_timeout == 15

    def test_load_partial_yaml(self, tmp_path, monkeypatch):
        """Partial config.yaml — missing keys use defaults."""
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        config_dir = tmp_path / ".config" / "serial-mux"
        config_dir.mkdir(parents=True)
        (config_dir / "config.yaml").write_text("default_baud: 57600\n")
        cfg = Config.load()
        assert cfg.default_baud == 57600
        assert cfg.log_retention_days == 7  # default
        assert cfg.ssh_connect_timeout == 3  # default

    def test_load_empty_yaml(self, tmp_path, monkeypatch):
        """Empty config.yaml — all defaults."""
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        config_dir = tmp_path / ".config" / "serial-mux"
        config_dir.mkdir(parents=True)
        (config_dir / "config.yaml").write_text("")
        cfg = Config.load()
        assert cfg.default_baud == 115200


class TestEnsureDirs:
    def test_creates_directories(self, tmp_path):
        cfg = Config()
        cfg.base_dir = tmp_path / "serial-mux"
        cfg.ensure_dirs()
        assert cfg.run_dir.exists()
        assert cfg.sock_dir.exists()
        assert cfg.logs_dir.exists()

    def test_idempotent(self, tmp_path):
        cfg = Config()
        cfg.base_dir = tmp_path / "serial-mux"
        cfg.ensure_dirs()
        cfg.ensure_dirs()  # no error on second call
        assert cfg.run_dir.exists()
