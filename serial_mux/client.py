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


def resolve_socket(config: Config, alias: str) -> str:
    """Resolve alias to socket path."""
    info_path = config.run_dir / f"{alias}.json"
    if info_path.exists():
        info = json.loads(info_path.read_text())
        return info.get("socket", "")
    # Try as device path
    for f in config.run_dir.glob("*.json"):
        try:
            info = json.loads(f.read_text())
            if info.get("device") == alias:
                return info.get("socket", "")
        except Exception:
            pass
    return ""


def _is_daemon_dead(config: Config, alias: str) -> bool:
    """Check if daemon for alias has a metadata file but process is dead."""
    info_path = config.run_dir / f"{alias}.json"
    if not info_path.exists():
        return False
    try:
        info = json.loads(info_path.read_text())
        pid = info.get("pid", 0)
        os.kill(pid, 0)
        return False  # still alive
    except ProcessLookupError:
        return True
    except (PermissionError, ValueError, json.JSONDecodeError):
        return False


def _auto_resume_daemon(config: Config, alias: str) -> bool:
    """Attempt to restart a dead daemon from its saved metadata. Returns True on success."""
    info_path = config.run_dir / f"{alias}.json"
    if not info_path.exists():
        return False
    try:
        info = json.loads(info_path.read_text())
    except (json.JSONDecodeError, OSError):
        return False

    device = info.get("device")
    baud = info.get("baud", 115200)
    ssh_target = info.get("ssh")

    if not device and not ssh_target:
        return False

    # Clean up stale files before restarting
    for suffix in [".json", ".pid"]:
        p = config.run_dir / f"{alias}{suffix}"
        p.unlink(missing_ok=True)
    sock_file = config.sock_dir / f"{alias}.sock"
    sock_file.unlink(missing_ok=True)

    # Build the serial-mux start command
    cmd = [sys.executable, "-m", "serial_mux.cli", "start", "--alias", alias]
    if device:
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


def connect(config: Config, alias: str) -> socket.socket:
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
    sync_write_msg(sock, {"type": "hello"})

    # Read hello_ack
    msg = sync_read_msg(sock)
    if not msg or msg.get("type") != "hello_ack":
        print(f"Error: Unexpected response from daemon", file=sys.stderr)
        sys.exit(1)

    # Store transport type for later use
    transport_type = msg.get("transport", "serial")

    return sock, transport_type


_TS_RE = re.compile(r"^\[\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}\] ")
_ANSI_RE = re.compile(
    r"\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)"   # OSC: \x1b]...(BEL or ST)
    r"|\x1bP[^\x1b]*\x1b\\"                 # DCS: \x1bP...\x1b\\
    r"|\x1b\[[!-?]*[0-9;]*[ -/]*[A-la-ln-~]"   # CSI except SGR (SGR ends with 'm')
    r"|\x1b\([0-9;]*[A-Za-z@-~]"            # ESC ( charset select
    r"|\x1b[^\[\(\]P]"                       # ESC + single char
    r"|[\x00-\x08\x0e-\x1a\x1c-\x1f]"                # C0 control chars except \t \n \r \x1b(ESC)
)


def _strip_timestamp(line: str) -> str:
    """Remove leading [YYYY-MM-DD HH:MM:SS] prefix from a log line."""
    return _TS_RE.sub("", line)


def _sanitize_history_line(line: str) -> str:
    """Strip ANSI escapes, backspaces, and stray \\r from a history line."""
    line = _ANSI_RE.sub("", line)
    line = line.replace("\r", "")
    return line


def interactive_mode(config: Config, alias: str, timestamps: bool = False):
    """Interactive attach mode — like tio/minicom but multiplexed."""
    sock, transport = connect(config, alias)

    # Read history (will be replayed after entering raw mode)
    history_msg = sync_read_msg(sock)

    # Save terminal state and switch to raw mode
    old_settings = termios.tcgetattr(sys.stdin.fileno())
    try:
        tty.setraw(sys.stdin.fileno())
        sock.setblocking(False)

        # Replay history in raw mode so terminal handles it cleanly
        if history_msg and history_msg.get("type") == "history":
            raw_lines = history_msg.get("lines", [])
            # Sanitize and deduplicate history lines
            cleaned = []
            seen_window = deque(maxlen=5)
            _sgr_re = re.compile(r"\x1b\[[0-9;]*m")
            for line in raw_lines:
                text = line if timestamps else _strip_timestamp(line)
                text = _sanitize_history_line(text)
                # Compare by plain text (no SGR, no trailing whitespace)
                plain = _sgr_re.sub("", text).strip()
                if not plain or plain in seen_window:
                    continue
                seen_window.append(plain)
                cleaned.append(text)
            for text in cleaned:
                os.write(sys.stdout.fileno(), (text + "\r\n").encode("utf-8", errors="replace"))

        print(f"\r\n--- serial-mux: attached to {alias} [{transport}] (Ctrl+] to detach) ---\r\n",
              end="", flush=True)

        last_output_was_newline = True

        while True:
            readable, _, _ = select.select([sys.stdin, sock], [], [], 0.1)

            if sys.stdin in readable:
                try:
                    ch = os.read(sys.stdin.fileno(), 1)
                except OSError:
                    break
                if not ch:
                    break
                # Ctrl+] to detach
                if ch == b"\x1d":
                    print("\r\n--- detached ---\r\n", end="", flush=True)
                    break

                # If timestamps enabled, handle input newline
                if timestamps and ch in (b"\r", b"\n"):
                    ts = datetime.now().strftime("%H:%M:%S")
                    # Use \r\n to ensure proper cursor movement in raw mode
                    os.write(sys.stdout.fileno(), f" [{ts}]\r\n".encode("utf-8"))

                # Send to daemon
                try:
                    sync_write_msg(sock, {"type": "input", "data": b64(ch)})
                except (BrokenPipeError, ConnectionResetError):
                    print("\r\n--- connection lost ---\r\n", end="", flush=True)
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
                        print("\r\n--- daemon disconnected ---\r\n", end="", flush=True)
                        break

                    if msg["type"] == "output":
                        data = unb64(msg["data"])
                        if not timestamps:
                            os.write(sys.stdout.fileno(), data)
                        else:
                            # Character-by-character processing for timestamp insertion
                            for b in data:
                                if last_output_was_newline:
                                    ts = datetime.now().strftime("[%H:%M:%S] ")
                                    os.write(sys.stdout.fileno(), ts.encode("utf-8"))
                                    last_output_was_newline = False
                                
                                char_bytes = bytes([b])
                                os.write(sys.stdout.fileno(), char_bytes)
                                if char_bytes == b"\n":
                                    last_output_was_newline = True

                except (TimeoutError, socket.timeout):
                    # Timeout reading a complete message — switch back to non-blocking
                    sock.setblocking(False)
                except (ConnectionResetError, BrokenPipeError):
                    print("\r\n--- connection lost ---\r\n", end="", flush=True)
                    break
                except Exception:
                    sock.setblocking(False)

    finally:
        termios.tcsetattr(sys.stdin.fileno(), termios.TCSADRAIN, old_settings)
        sock.close()


def noninteractive_mode(config: Config, alias: str,
                         send_cmd: str, wait_pattern: str = None,
                         timeout: float = 10.0):
    """Non-interactive mode: send command, verify echo, wait for pattern."""
    import time

    sock, transport = connect(config, alias)

    # Drain history
    msg = sync_read_msg(sock)  # history message

    sock.setblocking(False)

    use_echo_verify = (transport != "ssh")

    max_retries = 5 if use_echo_verify else 1
    send_success = False
    all_output = ""  # Accumulate ALL output from the moment we send

    for attempt in range(max_retries):
        # Send command + CR
        cmd_bytes = (send_cmd + "\r").encode("utf-8")
        try:
            sock.setblocking(True)
            sync_write_msg(sock, {"type": "input", "data": b64(cmd_bytes)})
            sock.setblocking(False)
        except (BrokenPipeError, ConnectionResetError):
            print(f"Error: Connection lost", file=sys.stderr)
            sys.exit(1)

        if not use_echo_verify:
            # SSH transport: no echo verification needed
            send_success = True
            break

        # Wait for echo and verify
        all_output = ""
        deadline = time.time() + 3.0  # 3s to see echo

        while time.time() < deadline:
            readable, _, _ = select.select([sock], [], [], 0.1)
            if sock in readable:
                try:
                    msg = sync_read_msg(sock)
                    if msg and msg["type"] == "output":
                        all_output += unb64(msg["data"]).decode("utf-8", errors="replace")
                        # Check if our command appeared in echo
                        if send_cmd in all_output:
                            send_success = True
                            break
                except BlockingIOError:
                    pass
                except Exception:
                    pass

        if send_success:
            break
        else:
            if attempt < max_retries - 1:
                print(f"Warning: Echo mismatch (attempt {attempt + 1}/{max_retries}), retrying...",
                      file=sys.stderr)
                time.sleep(0.5)

    if not send_success:
        print(f"Error: Failed to verify command echo after {max_retries} attempts", file=sys.stderr)
        sys.exit(1)

    # Now wait for pattern if specified, continuing to accumulate output
    if wait_pattern:
        # Compile regex pattern (fall back to literal match on invalid regex)
        try:
            wait_re = re.compile(wait_pattern)
        except re.error:
            wait_re = re.compile(re.escape(wait_pattern))

        # Check if pattern already in what we received during echo phase
        if wait_re.search(all_output):
            _print_output(all_output, send_cmd)
            sock.close()
            return

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

    args = parser.parse_args()
    config = Config.load()

    if args.send:
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
