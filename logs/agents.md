# `logs/` Development Guide

This is a runtime directory for per-alias, date-partitioned daemon logs under `~/.serial-mux/logs` (or a test-injected equivalent). The daemon writes timestamped line-oriented text here and purges files older than `log_retention_days` on startup.

Do not hand-edit, parse as protocol data, or commit generated logs. Use `smtty --tail` or tests to inspect behavior. Changes to log format, retention, history loading, or sensitive-data handling belong in `serial_mux/daemon.py`, `serial_mux/client.py`, and synchronized documentation/tests—not in this directory.
