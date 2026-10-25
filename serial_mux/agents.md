# `serial_mux/` Development Guide

## Module responsibilities

- `config.py`: `Config` owns defaults, optional `~/.config/serial-mux/config.yaml` loading, and derived runtime directories. `Config.load()` also creates `run`, `sock`, and `logs` directories.
- `protocol.py`: wire framing only. Messages are a 4-byte network-order length followed by UTF-8 JSON. `MAX_MSG_SIZE` is 1 MiB. Binary fields must use `b64()`/`unb64()`.
- `state.py`: persistent alias and USB identity logic. It scans `/sys/class/tty`, normalizes USB topology, compares boot IDs and bus/device instances, and atomically writes JSON records.
- `daemon.py`: the asyncio owner of serial/SSH transports. It accepts Unix-socket clients, broadcasts output, maintains scrollback logs, handles runtime bind/unbind, and performs graceful shutdown.
- `client.py`: synchronous socket protocol plus interactive and scripted client behavior. It owns terminal raw-mode setup/restore, history sanitation, terminal-query filtering, `--send/--wait`, and `--tail`.
- `cli.py`: command parsing and synchronous control requests to an already-running daemon. It reconciles aliases before starting, listing, or recovering a daemon.
- `__main__.py`: module entry point forwarding to the CLI.

## Protocol contract

Client hello must precede all other requests. The daemon replies with `hello_ack`, then a `history` message. Output and control messages can be interleaved afterward, so control clients must skip unrelated broadcasts while waiting for their acknowledgment. Do not assume the next message is always the requested response.

Current client-to-daemon messages include `hello`, `input`, `history_request`, `set_baud`, `ssh_bind`, `ssh_unbind`, `serial_bind`, and `serial_unbind`. Current daemon-to-client messages include `hello_ack`, `history`, `output`, `error`, `baud_ack`, `ssh_bind_ack`, `serial_bind_ack`, `transport_changed`, `terminal_owner`, and `serial_lost`.

When adding a message:

1. Document it in `protocol.py`.
2. Define behavior for malformed, oversized, missing, and unexpected fields.
3. Update both sync and async paths if applicable.
4. Add framing and behavioral tests.
5. Keep old fields/messages safely ignorable where possible.

## Daemon invariants

- `SerialDaemon` owns one serial object and at most one SSH subprocess. SSH is preferred when connected; serial handles input/output when SSH is absent.
- A serial read failure unbinds the serial transport, clears the live USB metadata, broadcasts `serial_lost`, and leaves the daemon alive for another bind.
- SSH termination clears SSH state and broadcasts `transport_changed` to serial. Explicit unbind follows the same visible transport transition.
- Every connected client receives broadcast output. Failed writers are removed and the client count is persisted.
- The newest interactive client is terminal owner. Only that client should receive terminal query responses from an attached remote TUI; when it disconnects, the most recent remaining interactive client is promoted.
- History is loaded from recent logs and bounded both by configured line count and protocol message size. Never allow a history response over `MAX_MSG_SIZE`.
- Runtime metadata is written to `run/<alias>.json`; transient PID and socket files are separate. A normal daemon cleanup calls `mark_info_saved()` so USB aliases remain recoverable.
- Do not silently retry user input. A timeout or transport failure may leave delivery unknown.

## USB alias rules

`usb_port` is the persistent physical topology key. `usb_instance` detects same-boot replacement. The old `device` path must never be used to recover a USB alias after reboot. Resolve a current TTY from sysfs first; only then open it. Preserve non-USB/legacy device behavior and SSH-only aliases.

Any state change must consider these statuses: `plain`, `connected`, `rebooted`, `unavailable`, `disconnected`, and `replugged`. A same-boot disconnect or replug must not bind a later device to the old alias without explicit action.

## Client and terminal rules

Interactive mode must always restore local tty settings, normalize terminal state, and close the socket in `finally` paths. History sanitation may preserve SGR color sequences but must remove terminal state changes and terminal queries. Live output must remain below replayed history and scroll at the bottom. Avoid terminal-screen abstractions that buffer or rewrite output; stdout should remain usable by terminals and tmux.

Non-interactive mode sends `<command>\r` once, collects output after the send, optionally matches `--wait` as regex with literal fallback, and returns exit code 2 for a wait timeout. It must not rely on command echo to decide whether the command was delivered.

## Testing expectations

Use fake sockets and PTYs for client/terminal unit tests. Use the `socat` fixture for daemon serial integration. SSH integration uses a temporary key and a local SSH server. For code that touches `Path.home()`, sysfs, subprocesses, signals, or time, use monkeypatching or explicit injected paths. Run focused tests and then the full suite after changes.
