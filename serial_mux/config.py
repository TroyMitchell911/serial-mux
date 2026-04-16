"""Configuration management for serial-mux."""

import os
from dataclasses import dataclass, field
from pathlib import Path

import yaml


@dataclass
class Config:
    log_retention_days: int = 7
    default_baud: int = 115200
    scrollback_lines: int = 5000

    # Derived paths
    base_dir: Path = field(default_factory=lambda: Path.home() / ".serial-mux")
    config_dir: Path = field(default_factory=lambda: Path.home() / ".config" / "serial-mux")

    @property
    def run_dir(self) -> Path:
        return self.base_dir / "run"

    @property
    def sock_dir(self) -> Path:
        return self.base_dir / "sock"

    @property
    def logs_dir(self) -> Path:
        return self.base_dir / "logs"

    def ensure_dirs(self):
        """Create all required directories."""
        for d in [self.run_dir, self.sock_dir, self.logs_dir]:
            d.mkdir(parents=True, exist_ok=True)

    @classmethod
    def load(cls) -> "Config":
        """Load config from ~/.config/serial-mux/config.yaml, falling back to defaults."""
        config_path = Path.home() / ".config" / "serial-mux" / "config.yaml"
        cfg = cls()
        if config_path.exists():
            with open(config_path) as f:
                data = yaml.safe_load(f) or {}
            if "log_retention_days" in data:
                cfg.log_retention_days = int(data["log_retention_days"])
            if "default_baud" in data:
                cfg.default_baud = int(data["default_baud"])
            if "scrollback_lines" in data:
                cfg.scrollback_lines = int(data["scrollback_lines"])
        cfg.ensure_dirs()
        return cfg
