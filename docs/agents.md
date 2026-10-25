# `docs/` Development Guide

`docs/ARCHITECTURE.md` is the detailed architecture reference. `README.md` is the English user-facing contract and `README_zh.md` is its Chinese counterpart. Keep all three aligned with implemented behavior.

Document the behavior that exists in source and tests. The current implementation is Linux-oriented: pyserial, Unix-domain sockets, Linux sysfs USB identity, POSIX raw terminal APIs, classic double-fork daemonization, and optional local SSH PTY transport.

When a user-visible command, option, message, runtime file, fallback rule, exit code, or persistence invariant changes:

1. Update the English README.
2. Update the Chinese README with equivalent examples, fields, and semantics.
3. Update `ARCHITECTURE.md` for component boundaries and lifecycle behavior.
4. Add or update tests before describing behavior as supported.

Preserve these documented contracts: alias recovery uses `usb_port`; stale TTY names are not recovery keys; same-boot replug invalidates mappings; SSH prefers/falls back to serial; interactive history is replayed before live output; terminal ownership protects remote TUI query handling; `--send` sends once without automatic retry; explicit `stop` removes saved alias state while daemon cleanup retains it for recovery.

Keep command examples consistent with `serial_mux/cli.py` and `serial_mux/client.py`. Do not advertise modules or commands that are absent (for example, there is currently no `transfer.py`).
