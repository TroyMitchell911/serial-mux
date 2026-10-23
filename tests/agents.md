# `tests/` Development Guide

## Test layers

- `test_protocol.py`: base64, JSON, length framing, Unicode, socketpair round trips, EOF, and message size behavior.
- `test_config.py`: defaults, YAML overrides, partial/empty config, derived paths, and idempotent directory creation.
- `test_state.py`: sysfs USB fixtures, reboot-safe TTY resolution, same-boot replug/disconnect invalidation, boot-ID PID protection, daemon metadata cleanup, and serial loss handling.
- `test_cli.py`: CLI utility functions, stale runtime cleanup, saved alias listing/restoration, explicit stop behavior, and subprocess help/argument validation.
- `test_client.py`: history sanitation, terminal-query filtering, tty restoration, interactive detach, exact-once non-interactive sending, and automatic daemon resume.
- `test_terminal_screen.py`: pyte-based rendering regressions. It verifies history/live-output ordering, cursor placement, scrolling, margins, and cleanup after a remote TUI.
- `test_daemon.py`: foreground daemon lifecycle, handshake, serial data flow, broadcast fan-out, client count, terminal ownership, and logging.
- `test_serial_bind.py`: runtime serial bind/unbind, failed device binding, metadata, and data flow.
- `test_ssh.py`: SSH validation, bind/unbind, data flow, daemon startup binding, serial fallback, and input routing.
- `conftest.py`: temporary configuration, `socat` PTY pairs, foreground daemon subprocesses, and the common handshake helper.

## Commands

```bash
pip install -e ".[test]"
python3 -m pytest tests/ -v --timeout=60
python3 -m pytest tests/test_protocol.py tests/test_state.py -q
```

CI runs the full suite on Ubuntu with Python 3.10 and 3.12, installing `socat` and `openssh-server`. The daemon and SSH tests are integration tests, not pure unit tests; they need those system tools and may take up to the configured timeout.

## Fixtures and isolation

`tmp_config` redirects runtime files into a temporary directory. Do not let tests write daemon state under the real `~/.serial-mux`. `socat_pty` creates two linked raw PTYs and tears down the subprocess. `daemon_foreground` starts `SerialDaemon` in a subprocess so asyncio, signals, and serial I/O behave realistically.

`fake_usb_sysfs` replaces state module constants with a synthetic sysfs tree; use that pattern rather than requiring `/sys` or hardware. Client tests use fake sockets/TTYs and monkeypatch `select`, `os.read`, `os.write`, and terminal APIs.

The module-scoped SSH fixture generates a temporary key and appends it to the current user's `~/.ssh/authorized_keys`, then restores the file. Keep cleanup robust and prefer an isolated user/home for local runs.

## Writing regression tests

- Assert externally observable behavior and message order, not incidental private implementation details, unless testing a specific invariant.
- For socket protocols, drain the initial `hello_ack` and `history` before sending a request. Control responses may be preceded by broadcasts such as `transport_changed`; use a helper that waits for the expected message type.
- For daemon output, allow for scheduling and PTY timing with bounded deadlines rather than arbitrary long sleeps. Always close sockets and stop subprocesses in teardown paths.
- Test both success and failure paths for transport changes. Include disconnected, replugged, rebooted, SSH-only, and serial-only combinations when touching alias logic.
- For terminal changes, assert pyte screen state and cursor/scroll position, not only emitted escape bytes.
- Add a focused regression test for every bug fix; update fixtures only when the shared contract changes.

Do not commit logs, PID/socket files, `__pycache__`, `.pytest_cache`, real SSH keys, or generated test artifacts.
