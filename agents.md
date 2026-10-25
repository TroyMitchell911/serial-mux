# serial-mux Development Guide

## Scope

This repository is a Python 3.10+ serial-port multiplexer. One daemon owns a serial device (or optional SSH PTY), writes dated logs, and multiplexes multiple clients through a Unix-domain socket. `serial-mux` manages daemons; `smtty` is the client.

Read the nearest `agents.md` before editing a file. The directory guides refine this file:

- `serial_mux/agents.md` — runtime modules and invariants
- `tests/agents.md` — fixtures, integration coverage, and test hygiene
- `docs/agents.md` — documentation synchronization
- `.github/agents.md` — CI contract
- `.claude/agents.md` — local assistant configuration
- `logs/`, `run/`, `sock/` — generated runtime directories; do not edit their contents
- `serial_mux.egg-info/` — generated packaging metadata; do not hand-edit it
- `.pytest_cache/` — generated test cache; do not edit it

## Source map

- `serial_mux/config.py`: optional YAML configuration and `~/.serial-mux` derived paths.
- `serial_mux/protocol.py`: 4-byte big-endian length-prefixed UTF-8 JSON; binary payloads use base64.
- `serial_mux/state.py`: persistent alias records, Linux sysfs USB identity, boot/replug detection, and atomic JSON writes.
- `serial_mux/daemon.py`: asyncio daemon, serial/SSH transports, Unix socket server, fan-out, terminal ownership, logging, and lifecycle.
- `serial_mux/client.py`: alias recovery, synchronous protocol client, interactive raw-terminal mode, non-interactive `--send/--wait`, and log tailing.
- `serial_mux/cli.py`: `start`, `stop`, `list`, `status`, baud changes, and runtime serial/SSH bind commands.
- `README.md`, `README_zh.md`, `docs/ARCHITECTURE.md`: user-facing and architecture documentation. Keep English and Chinese usage documentation synchronized.

## Required invariants

1. The daemon is the exclusive owner of a serial device; clients only use the Unix socket.
2. Alias recovery is keyed by `usb_port`, not by a stale `/dev/ttyUSB*` name. `device` is runtime state and is cleared when a daemon exits. Same-boot replug/disconnect invalidates a mapping rather than silently reusing it.
3. SSH is optional and preferred when connected; serial is the fallback. Transport changes must be visible to clients.
4. Interactive clients replay history, then receive live output below that history. The newest interactive client owns terminal-query responses; observers must not answer remote TUI geometry queries.
5. Non-interactive `smtty --send` sends exactly once. Do not add echo-based retries or automatic retransmission after timeout/disconnect.
6. Protocol reads must respect framing and `MAX_MSG_SIZE`; changes to message types or fields require protocol tests and compatible client/daemon handling.
7. Data sent to clients is raw bytes encoded with base64. Logs are line-oriented timestamped text and are not a substitute for the byte stream.
8. Daemon shutdown keeps a recoverable alias record unless the explicit CLI `stop` removes it.

## Development workflow

Install editable package and test extras:

```bash
pip install -e ".[test]"
python3 -m pytest tests/ -v --timeout=60
```

CI currently tests Python 3.10 and 3.12 on Ubuntu and exercises virtual serial ports plus localhost SSH. `socat` and an SSH server are required for the integration fixtures. Run focused tests while iterating, then run the complete suite.

Before changing terminal behavior, inspect `tests/test_terminal_screen.py` and `tests/test_client.py`; cursor position, scrolling margins, alternate-screen cleanup, SGR preservation, and split escape sequences are intentional compatibility surfaces.

Before changing USB aliases, inspect `tests/test_state.py`, `tests/test_cli.py`, and `tests/test_client.py`. Test both reboot resolution and same-boot unplug/re-enumeration.

Before changing SSH, inspect `tests/test_ssh.py`. Those tests create a temporary key and modify the current user's `~/.ssh/authorized_keys` during the fixture; use an isolated test account or disposable environment when appropriate.

## Change discipline

- Keep changes narrow and match the surrounding Python style and comment density.
- Prefer dependency injection/monkeypatchable paths over real devices, real home directories, or global process state in unit tests.
- Update both README files and the architecture document whenever user-visible behavior changes.
- Add regression tests with every bug fix, especially for protocol framing, daemon lifecycle, alias recovery, transport fallback, and terminal rendering.
- Do not add credentials, device output, socket files, logs, PID files, or generated packaging/cache files to commits.
- Do not treat `serial_mux.egg-info`, `.pytest_cache`, `logs`, `run`, or `sock` as source of truth.
- Preserve signed-off and GPG-signed commits when committing: `git commit -s -S -m "..."`.
