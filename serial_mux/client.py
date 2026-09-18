"""smtty / smtty-hermes: interactive and non-interactive serial-mux client."""

import argparse
import json
import os
import select
import socket
import subprocess
import sys
import time
import termios
import tty
from collections import deque
from pathlib import Path

import re
from datetime import datetime

from .config import Config
from .protocol import sync_read_msg, sync_write_msg, b64, unb64
from .state import info_is_running, reconcile_info


def _load_alias_info(config: Config, alias: str) -> dict | None:
    """Load and reconcile an alias record before it is used for recovery."""
    info_path = config.run_dir / f"{alias}.json"
    if not info_path.exists():
        return None
    try:
        return reconcile_info(info_path, json.loads(info_path.read_text()))
    except (json.JSONDecodeError, OSError):
        return None


def resolve_socket(config: Config, alias: str) -> str:
    """Resolve alias to socket path."""
    info = _load_alias_info(config, alias)
    if info:
        return info.get("socket", "")
    # Try as device path
    for f in config.run_dir.glob("*.json"):
        try:
            info = reconcile_info(f, json.loads(f.read_text()))
            if info and info.get("device") == alias:
                return info.get("socket", "")
        except (json.JSONDecodeError, OSError):
            pass
    return ""


def _is_daemon_dead(config: Config, alias: str) -> bool:
    """Check if an alias has saved state but no daemon in this boot."""
    info = _load_alias_info(config, alias)
    return bool(info and not info_is_running(info))


def _auto_resume_daemon(config: Config, alias: str) -> bool:
    """Attempt to restart a dead daemon from its saved metadata. Returns True on success."""
    info = _load_alias_info(config, alias)
    if not info:
        return False

    device = info.get("device")
    baud = info.get("baud", 115200)
    ssh_target = info.get("ssh")
    has_usb_mapping = bool(info.get("usb_port"))

    if (
        has_usb_mapping
        and info.get("_device_status") == "unavailable"
        and not ssh_target
    ):
        print(
            f"Error: Saved USB port for '{alias}' is not available",
            file=sys.stderr,
        )
        return False

    if not has_usb_mapping and not device and not ssh_target:
        return False

    # Clean up only transient files. Keep the mapping until the replacement
    # daemon has successfully opened its transport.
    (config.run_dir / f"{alias}.pid").unlink(missing_ok=True)
    sock_file = config.sock_dir / f"{alias}.sock"
    sock_file.unlink(missing_ok=True)

    # Build the serial-mux start command
    cmd = [sys.executable, "-m", "serial_mux.cli", "start", "--alias", alias]
    # USB aliases are restored by name so the CLI performs a fresh sysfs
    # reverse lookup. Never feed the last /dev/ttyUSB* name back into start.
    if device and not has_usb_mapping:
        cmd.append(device)
        cmd.extend(["--baud", str(baud)])
    if ssh_target:
        cmd.extend(["--ssh", ssh_target])

    print(f"Resuming dead daemon '{alias}'...", file=sys.stderr, flush=True)
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=15)
    except subprocess.TimeoutExpired:
        print(f"Error: Timed out resuming daemon '{alias}'", file=sys.stderr)
        return False

    if result.returncode != 0:
        stderr = result.stderr.strip() or result.stdout.strip()
        print(f"Error: Failed to resume daemon '{alias}': {stderr}", file=sys.stderr)
        return False

    # Wait for socket to appear
    sock_path = str(config.sock_dir / f"{alias}.sock")
    for _ in range(30):
        if Path(sock_path).exists():
            return True
        time.sleep(0.1)

    print(f"Error: Daemon resumed but socket not ready", file=sys.stderr)
    return False


def connect(
    config: Config,
    alias: str,
    interactive: bool = False,
) -> tuple[socket.socket, str]:
    """Connect to daemon and perform handshake. Auto-resumes dead daemons."""
    sock_path = resolve_socket(config, alias)

    need_resume = False
    if not sock_path:
        # No metadata at all — check if there's a dead daemon to resume
        if _is_daemon_dead(config, alias):
            need_resume = True
        else:
            print(f"Error: No daemon found for '{alias}'", file=sys.stderr)
            print(f"Start one with: serial-mux start <device> --alias {alias}", file=sys.stderr)
            sys.exit(1)
    elif not Path(sock_path).exists():
        need_resume = _is_daemon_dead(config, alias)
        if not need_resume:
            print(f"Error: Socket {sock_path} not found. Daemon may have crashed.", file=sys.stderr)
            sys.exit(1)

    if not need_resume and sock_path:
        # Try connecting — may get ConnectionRefused if socket file is stale
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            sock.connect(sock_path)
        except (ConnectionRefusedError, OSError):
            sock.close()
            need_resume = _is_daemon_dead(config, alias)
            if not need_resume:
                print(f"Error: Connection refused to '{alias}'. Daemon may have crashed.", file=sys.stderr)
                sys.exit(1)

    if need_resume:
        if not _auto_resume_daemon(config, alias):
            sys.exit(1)
        # Re-resolve socket after restart
        sock_path = resolve_socket(config, alias)
        if not sock_path:
            print(f"Error: Daemon resumed but socket path not found", file=sys.stderr)
            sys.exit(1)
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.connect(sock_path)

    # Send hello
    sync_write_msg(
        sock,
        {"type": "hello", "interactive": interactive},
    )

    # Read hello_ack
    msg = sync_read_msg(sock)
    if not msg or msg.get("type") != "hello_ack":
        print(f"Error: Unexpected response from daemon", file=sys.stderr)
        sys.exit(1)

    # Store transport type for later use
    transport_type = msg.get("transport", "serial")

    return sock, transport_type


_TS_RE = re.compile(r"^\[\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}\] ")
_SGR_RE = re.compile(r"\x1b\[[0-?]*[ -/]*m")

_TERMINAL_NORMALIZE = (
    b"\x1b[?1049l"  # Leave an alternate screen opened by the remote TUI.
    b"\x0f"  # Select G0; SO may have selected the line-drawing G1 set.
    b"\x1b(B"  # Designate ASCII for G0.
    b"\x1b)B"  # Designate ASCII for G1.
    b"\x1b[0m"  # Reset colors and attributes.
    b"\x1b[?1l"  # Restore normal cursor keys.
    b"\x1b>"  # Restore the numeric keypad.
    b"\x1b[?6l"  # Disable origin mode before resetting the margins.
    b"\x1b[r"  # Restore the full-screen scrolling region.
    b"\x1b[?7h"  # Restore autowrap.
    b"\x1b[?25h"  # Show the cursor.
    # DECOM and DECSTBM home the cursor. Start a fresh line at the bottom
    # afterward so the next output cannot overwrite existing screen text.
    # CUP clamps this row to the screen height without a terminal query.
    b"\x1b[9999;1H\r\n"
)


class _TerminalQueryFilter:
    """Remove terminal queries from an observer's output stream.

    A terminal query sent to every attached client would produce one response
    per terminal.  The remote TUI can then calculate its geometry from the
    wrong response.  Only the active terminal owner receives these queries.
    """

    _WINDOW_REPORTS = {
        11,
        13,
        14,
        15,
        16,
        18,
        19,
        20,
        21,
    }

    def __init__(self):
        self._pending = bytearray()

    @staticmethod
    def _is_query(sequence: bytes) -> bool:
        payload = sequence[2:-1]
        final = sequence[-1:]

        if final in (b"c", b"n", b"x"):
            return True
        if final == b"p" and b"$" in payload:
            return True
        if final != b"t":
            return False

        first_param = payload.split(b";", 1)[0].lstrip(b"?>")
        try:
            operation = int(first_param)
        except ValueError:
            return False
        return operation in _TerminalQueryFilter._WINDOW_REPORTS

    def reset(self):
        self._pending.clear()

    def feed(self, data: bytes) -> bytes:
        """Return data with complete CSI terminal queries removed."""
        source = bytes(self._pending) + data
        self._pending.clear()
        output = bytearray()
        index = 0

        while index < len(source):
            escape = source.find(b"\x1b", index)
            if escape < 0:
                output.extend(source[index:])
                break

            output.extend(source[index:escape])
            if escape + 1 >= len(source):
                self._pending.extend(source[escape:])
                break
            if source[escape + 1] != ord("["):
                output.append(source[escape])
                index = escape + 1
                continue

            final = escape + 2
            while final < len(source):
                if 0x40 <= source[final] <= 0x7E:
                    break
                final += 1
            if final >= len(source):
                self._pending.extend(source[escape:])
                break

            sequence = source[escape:final + 1]
            if not self._is_query(sequence):
                output.extend(sequence)
            index = final + 1

        return bytes(output)


def _strip_timestamp(line: str) -> str:
    """Remove leading [YYYY-MM-DD HH:MM:SS] prefix from a log line."""
    return _TS_RE.sub("", line)


def _sanitize_history_line(line: str) -> str:
    """Keep printable history and SGR while dropping terminal state changes."""
    output = []
    index = 0

    while index < len(line):
        char = line[index]
        code = ord(char)

        if char == "\x1b":
            index += 1
            if index >= len(line):
                break

            introducer = line[index]
            if introducer == "[":
                final = index + 1
                while final < len(line):
                    if 0x40 <= ord(line[final]) <= 0x7E:
                        break
                    final += 1
                if final >= len(line):
                    break
                if line[final] == "m":
                    output.append(line[index - 1:final + 1])
                index = final + 1
                continue

            if introducer in "]PX^_":
                index += 1
                while index < len(line):
                    if line[index] == "\x07":
                        index += 1
                        break
                    if (
                        line[index] == "\x1b"
                        and index + 1 < len(line)
                        and line[index + 1] == "\\"
                    ):
                        index += 2
                        break
                    index += 1
                continue

            while index < len(line):
                code = ord(line[index])
                if not 0x20 <= code <= 0x2F:
                    break
                index += 1
            if index < len(line):
                code = ord(line[index])
                if 0x30 <= code <= 0x7E:
                    index += 1
            continue

        if char == "\r" or code == 0x7F:
            index += 1
            continue
        if code < 0x20 and char != "\t":
            index += 1
            continue

        output.append(char)
        index += 1

    return "".join(output)


def _write_terminal_sequence(fd: int, sequence: bytes):
    """Write a complete control sequence to a terminal when one is present."""
    if not os.isatty(fd):
        return

    remaining = memoryview(sequence)
    try:
        while remaining:
            written = os.write(fd, remaining)
            if written <= 0:
                break
            remaining = remaining[written:]
    except OSError:
        pass


def _normalize_local_terminal(output_fd: int):
    """Put the terminal emulator in a neutral state before rendering."""
    _write_terminal_sequence(output_fd, _TERMINAL_NORMALIZE)


def _restore_local_terminal(
    input_fd: int,
    output_fd: int,
    settings,
):
    """Restore emulator state and the local tty line discipline."""
    try:
        _normalize_local_terminal(output_fd)
    finally:
        termios.tcsetattr(input_fd, termios.TCSADRAIN, settings)


def interactive_mode(config: Config, alias: str, timestamps: bool = False):
    """Interactive attach mode — like tio/minicom but multiplexed."""
    sock, transport = connect(config, alias, interactive=True)

    # Read history (will be replayed after entering raw mode)
    history_msg = sync_read_msg(sock)

    # Save terminal state and switch to raw mode
    input_fd = sys.stdin.fileno()
    output_fd = sys.stdout.fileno()
    old_settings = termios.tcgetattr(input_fd)
    exit_message = None
    terminal_owner = True
    query_filter = _TerminalQueryFilter()

    try:
        tty.setraw(input_fd)
        _normalize_local_terminal(output_fd)
        sock.setblocking(False)

        # Replay history in raw mode so terminal handles it cleanly
        if history_msg and history_msg.get("type") == "history":
            raw_lines = history_msg.get("lines", [])
            # Sanitize and deduplicate history lines
            cleaned = []
            seen_window = deque(maxlen=5)
            for line in raw_lines:
                text = line if timestamps else _strip_timestamp(line)
                text = _sanitize_history_line(text)
                # Compare by plain text (no SGR, no trailing whitespace)
                plain = _SGR_RE.sub("", text).strip()
                if not plain or plain in seen_window:
                    continue
                seen_window.append(plain)
                cleaned.append(text)
            for text in cleaned:
                data = (text + "\r\n").encode(
                    "utf-8",
                    errors="replace",
                )
                os.write(output_fd, data)

        # Sanitized history can only change SGR. Reset attributes here, not
        # margins/origin mode: those controls home the cursor and would make
        # the banner and live output overwrite the history just displayed.
        _write_terminal_sequence(output_fd, b"\x1b[0m")

        print(
            f"\r\n--- serial-mux: attached to {alias} "
            f"[{transport}] (Ctrl+] to detach) ---\r\n",
            end="",
            flush=True,
        )

        last_output_was_newline = True

        while True:
            readable, _, _ = select.select([sys.stdin, sock], [], [], 0.1)

            if sys.stdin in readable:
                try:
                    ch = os.read(input_fd, 1)
                except OSError:
                    exit_message = "detached"
                    break
                if not ch:
                    exit_message = "detached"
                    break
                # Ctrl+] to detach
                if ch == b"\x1d":
                    exit_message = "detached"
                    break

                # If timestamps enabled, handle input newline
                if timestamps and ch in (b"\r", b"\n"):
                    ts = datetime.now().strftime("%H:%M:%S")
                    # Use \r\n to ensure proper cursor movement in raw mode
                    os.write(output_fd, f" [{ts}]\r\n".encode("utf-8"))

                # Send to daemon
                try:
                    sync_write_msg(sock, {"type": "input", "data": b64(ch)})
                except (BrokenPipeError, ConnectionResetError):
                    exit_message = "connection lost"
                    break

            if sock in readable:
                try:
                    # Switch to blocking with timeout for reliable message framing.
                    # Non-blocking reads can lose partial header bytes on EAGAIN,
                    # corrupting the stream and causing hangs.
                    sock.setblocking(True)
                    sock.settimeout(2.0)
                    msg = sync_read_msg(sock)
                    sock.setblocking(False)
                    if msg is None:
                        exit_message = "daemon disconnected"
                        break

                    message_type = msg.get("type")
                    if message_type == "terminal_owner":
                        terminal_owner = bool(msg.get("active"))
                        query_filter.reset()
                    elif message_type == "output":
                        data = unb64(msg["data"])
                        if not terminal_owner:
                            data = query_filter.feed(data)
                        if not data:
                            continue
                        if not timestamps:
                            os.write(output_fd, data)
                        else:
                            # Insert timestamps at the beginning of each line.
                            for b in data:
                                if last_output_was_newline:
                                    ts = datetime.now().strftime(
                                        "[%H:%M:%S] "
                                    )
                                    os.write(output_fd, ts.encode("utf-8"))
                                    last_output_was_newline = False

                                char_bytes = bytes([b])
                                os.write(output_fd, char_bytes)
                                if char_bytes == b"\n":
                                    last_output_was_newline = True

                except (TimeoutError, socket.timeout):
                    # Timeout reading a complete message — switch back to non-blocking
                    sock.setblocking(False)
                except (ConnectionResetError, BrokenPipeError):
                    exit_message = "connection lost"
                    break
                except Exception:
                    sock.setblocking(False)

    finally:
        try:
            _restore_local_terminal(input_fd, output_fd, old_settings)
        finally:
            sock.close()

    if exit_message:
        print(f"--- {exit_message} ---\r\n", end="", flush=True)


def noninteractive_mode(config: Config, alias: str,
                         send_cmd: str, wait_pattern: str = None,
                         timeout: float = 10.0):
    """Non-interactive mode: send a command once, then wait for output."""
    sock, _transport = connect(config, alias)

    # Drain history
    msg = sync_read_msg(sock)  # history message

    sock.setblocking(False)
    all_output = ""  # Accumulate ALL output from the moment we send

    # Send command + CR exactly once.  Output handling below is independent of
    # terminal echo, so devices with echo disabled do not trigger a resend.
    cmd_bytes = (send_cmd + "\r").encode("utf-8")
    try:
        sock.setblocking(True)
        sync_write_msg(sock, {"type": "input", "data": b64(cmd_bytes)})
        sock.setblocking(False)
    except (BrokenPipeError, ConnectionResetError):
        print("Error: Connection lost", file=sys.stderr)
        sock.close()
        sys.exit(1)

    # Now wait for pattern if specified and accumulate output.
    if wait_pattern:
        # Compile regex pattern (fall back to literal match on invalid regex)
        try:
            wait_re = re.compile(wait_pattern)
        except re.error:
            wait_re = re.compile(re.escape(wait_pattern))

        deadline = time.time() + timeout
        while time.time() < deadline:
            readable, _, _ = select.select([sock], [], [], 0.1)
            if sock in readable:
                try:
                    msg = sync_read_msg(sock)
                    if msg and msg["type"] == "output":
                        all_output += unb64(msg["data"]).decode("utf-8", errors="replace")
                        if wait_re.search(all_output):
                            _print_output(all_output, send_cmd)
                            sock.close()
                            return
                except BlockingIOError:
                    pass
                except Exception:
                    pass

        # Timeout
        _print_output(all_output, send_cmd)
        print(f"\nError: Timeout waiting for '{wait_pattern}'", file=sys.stderr)
        sock.close()
        sys.exit(2)
    else:
        # No wait pattern — just collect output for a short time
        deadline = time.time() + 1.0
        while time.time() < deadline:
            readable, _, _ = select.select([sock], [], [], 0.1)
            if sock in readable:
                try:
                    msg = sync_read_msg(sock)
                    if msg and msg["type"] == "output":
                        all_output += unb64(msg["data"]).decode("utf-8", errors="replace")
                except BlockingIOError:
                    pass
                except Exception:
                    pass

        _print_output(all_output, send_cmd)
        sock.close()


def _print_output(output: str, cmd: str):
    """Print command output, stripping the echo of the command itself."""
    lines = output.split("\n")
    # Find the line with our command and skip it
    found_cmd = False
    for i, line in enumerate(lines):
        if not found_cmd and cmd in line:
            found_cmd = True
            continue
        if found_cmd:
            print(line, end="" if i == len(lines) - 1 else "\n")


def tail_mode(config: Config, alias: str, lines: int = 50):
    """Print the last N lines from the daemon's log files and exit."""
    log_dir = config.logs_dir / alias
    if not log_dir.exists():
        print(f"Error: No logs found for '{alias}'", file=sys.stderr)
        sys.exit(1)

    log_files = sorted(log_dir.glob("*.log"))
    if not log_files:
        print(f"Error: No log files in {log_dir}", file=sys.stderr)
        sys.exit(1)

    # Collect lines from the most recent log files (newest last)
    collected = []
    for log_file in reversed(log_files):
        try:
            with open(log_file, "r", errors="replace") as f:
                file_lines = f.readlines()
            collected = file_lines + collected
            if len(collected) >= lines:
                break
        except OSError:
            continue

    # Take the last N lines
    tail = collected[-lines:]
    for line in tail:
        sys.stdout.write(line if line.endswith("\n") else line + "\n")


def main():
    parser = argparse.ArgumentParser(
        prog="smtty",
        description="serial-mux interactive client"
    )
    parser.add_argument("alias", help="Alias or device path")
    parser.add_argument("--send", "-s", help="Send command (non-interactive mode)")
    parser.add_argument("--wait", "-w", help="Wait for pattern after sending")
    parser.add_argument("--timeout", "-t", type=float, default=10.0,
                        help="Timeout in seconds for --wait (default: 10)")
    parser.add_argument("--timestamps", "-T", action="store_true", default=False,
                        help="Show timestamps on history, input and output lines")
    parser.add_argument("--tail", type=int, nargs="?", const=50, default=None,
                        metavar="N",
                        help="Print last N lines from log and exit (default: 50)")

    args = parser.parse_args()
    config = Config.load()

    if args.tail is not None:
        tail_mode(config, args.alias, lines=args.tail)
    elif args.send:
        noninteractive_mode(config, args.alias,
                           send_cmd=args.send,
                           wait_pattern=args.wait,
                           timeout=args.timeout)
    else:
        if not sys.stdin.isatty():
            print("Error: Interactive mode requires a terminal", file=sys.stderr)
            sys.exit(1)
        interactive_mode(config, args.alias, timestamps=args.timestamps)


if __name__ == "__main__":
    main()
