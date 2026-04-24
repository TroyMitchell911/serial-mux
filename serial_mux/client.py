"""smtty / smtty-hermes: interactive and non-interactive serial-mux client."""

import argparse
import json
import os
import select
import socket
import sys
import termios
import tty
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


def connect(config: Config, alias: str) -> socket.socket:
    """Connect to daemon and perform handshake."""
    sock_path = resolve_socket(config, alias)
    if not sock_path:
        print(f"Error: No daemon found for '{alias}'", file=sys.stderr)
        print(f"Start one with: serial-mux start <device> --alias {alias}", file=sys.stderr)
        sys.exit(1)

    if not Path(sock_path).exists():
        print(f"Error: Socket {sock_path} not found. Daemon may have crashed.", file=sys.stderr)
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

    return sock


_TS_RE = re.compile(r"^\[\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}\] ")


def _strip_timestamp(line: str) -> str:
    """Remove leading [YYYY-MM-DD HH:MM:SS] prefix from a log line."""
    return _TS_RE.sub("", line)


def interactive_mode(config: Config, alias: str, timestamps: bool = False):
    """Interactive attach mode — like tio/minicom but multiplexed."""
    sock = connect(config, alias)

    # Read history and print to stdout
    msg = sync_read_msg(sock)
    if msg and msg.get("type") == "history":
        lines = msg.get("lines", [])
        for line in lines:
            print(line if timestamps else _strip_timestamp(line))
        if lines:
            sys.stdout.flush()

    # Save terminal state and switch to raw mode
    old_settings = termios.tcgetattr(sys.stdin.fileno())
    try:
        tty.setraw(sys.stdin.fileno())
        sock.setblocking(False)

        print(f"\r\n--- serial-mux: attached to {alias} (Ctrl+] to detach) ---\r\n",
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
                    print(f" [{ts}]", end="", flush=True)

                # Send to daemon
                try:
                    sync_write_msg(sock, {"type": "input", "data": b64(ch)})
                except (BrokenPipeError, ConnectionResetError):
                    print("\r\n--- connection lost ---\r\n", end="", flush=True)
                    break

            if sock in readable:
                try:
                    msg = sync_read_msg(sock)
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

                except BlockingIOError:
                    pass
                except (ConnectionResetError, BrokenPipeError):
                    print("\r\n--- connection lost ---\r\n", end="", flush=True)
                    break
                except Exception:
                    pass

    finally:
        termios.tcsetattr(sys.stdin.fileno(), termios.TCSADRAIN, old_settings)
        sock.close()


def noninteractive_mode(config: Config, alias: str,
                         send_cmd: str, wait_pattern: str = None,
                         timeout: float = 10.0):
    """Non-interactive mode: send command, verify echo, wait for pattern."""
    import time

    sock = connect(config, alias)

    # Drain history
    msg = sync_read_msg(sock)  # history message

    sock.setblocking(False)

    max_retries = 5
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
        # Check if pattern already in what we received during echo phase
        if wait_pattern in all_output:
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
                        if wait_pattern in all_output:
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
