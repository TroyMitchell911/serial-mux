# `run/` Development Guide

This is a runtime directory for per-alias JSON metadata and transient PID files. JSON records preserve recoverable alias/USB/SSH state; PID files identify live daemon processes and are removed during cleanup.

Do not hand-edit or commit runtime records. Persistent USB recovery must use `usb_port`; the current `device` path is runtime-only and is cleared when a daemon exits. Inspect records through `serial-mux list` or `status`, and test state transitions with `tests/test_state.py` and `tests/test_cli.py`.
