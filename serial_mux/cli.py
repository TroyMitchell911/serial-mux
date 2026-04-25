"""serial-mux CLI: start/stop/list/status commands."""

import argparse
import json
import os
import signal
import socket
import sys
import time
from pathlib import Path

from .config import Config
from .protocol import sync_read_msg, sync_write_msg


def format_uptime(start_time: float) -> str:
    """Format uptime from a start timestamp to human-readable string."""
    elapsed = int(time.time() - start_time)
    if elapsed < 60:
        return f"{elapsed}s"
    elif elapsed < 3600:
        m, s = divmod(elapsed, 60)
        return f"{m}m {s}s"
    elif elapsed < 86400:
        h, rem = divmod(elapsed, 3600)
        m = rem // 60
        return f"{h}h {m}m"
    else:
        d, rem = divmod(elapsed, 86400)
        h = rem // 3600
        return f"{d}d {h}h"


def resolve_alias(config: Config, alias_or_device: str) -> dict:
    """Resolve an alias to its info dict. Returns None if not found."""
    # Try as alias first
    info_path = config.run_dir / f"{alias_or_device}.json"
    if info_path.exists():
        return json.loads(info_path.read_text())
    # Try scanning all info files for matching device
    for f in config.run_dir.glob("*.json"):
        try:
            info = json.loads(f.read_text())
            if info.get("device") == alias_or_device:
                return info
        except Exception:
            pass
    return None


def is_running(pid: int) -> bool:
    """Check if a process is running."""
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # Process exists but we can't signal it


def cmd_start(args):
    """Start a daemon for a serial port."""
    config = Config.load()

    device = args.device
    baud = args.baud or config.default_baud
    alias = args.alias

    if not alias:
        # Derive alias from device name
        alias = Path(device).name  # e.g., ttyUSB0

    # Check device exists
    if not Path(device).exists():
        print(f"Error: Device {device} not found")
        sys.exit(1)

    # Check if alias already in use
    existing = resolve_alias(config, alias)
    if existing:
        pid = existing.get("pid", 0)
        if is_running(pid):
            print(f"Error: Alias '{alias}' already running (PID {pid})")
            sys.exit(1)
        else:
            # Clean up stale files
            for suffix in [".json", ".pid"]:
                p = config.run_dir / f"{alias}{suffix}"
                p.unlink(missing_ok=True)
            sock = config.sock_dir / f"{alias}.sock"
            sock.unlink(missing_ok=True)

    # Import and start daemon
    from .daemon import start_daemon
    start_daemon(device, baud, alias, foreground=args.foreground)


def cmd_stop(args):
    """Stop a daemon."""
    config = Config.load()
    info = resolve_alias(config, args.alias)
    if not info:
        print(f"Error: No daemon found for '{args.alias}'")
        sys.exit(1)

    pid = info.get("pid", 0)
    alias = info.get("alias", args.alias)

    if not is_running(pid):
        print(f"Daemon '{alias}' not running (stale PID {pid}), cleaning up")
        for suffix in [".json", ".pid"]:
            p = config.run_dir / f"{alias}{suffix}"
            p.unlink(missing_ok=True)
        sock = config.sock_dir / f"{alias}.sock"
        sock.unlink(missing_ok=True)
        return

    os.kill(pid, signal.SIGTERM)
    # Wait for process to exit
    for _ in range(30):
        if not is_running(pid):
            print(f"Daemon '{alias}' stopped")
            return
        time.sleep(0.1)
    print(f"Warning: Daemon '{alias}' (PID {pid}) did not stop in 3s, sending SIGKILL")
    try:
        os.kill(pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    print(f"Daemon '{alias}' killed")


def cmd_list(args):
    """List all running daemons."""
    config = Config.load()
    infos = []
    for f in sorted(config.run_dir.glob("*.json")):
        try:
            info = json.loads(f.read_text())
            pid = info.get("pid", 0)
            info["_running"] = is_running(pid)
            infos.append(info)
        except Exception:
            pass

    if not infos:
        print("No daemons running")
        return

    print(f"{'ALIAS':<12} {'DEVICE':<20} {'BAUD':<10} {'PID':<8} {'CLIENTS':<9} {'UPTIME':<12} {'STATUS':<10}")
    print("-" * 81)
    for info in infos:
        alias = info.get("alias", "?")
        device = info.get("device", "?")
        baud = info.get("baud", "?")
        pid = info.get("pid", "?")
        status = "running" if info["_running"] else "dead"
        clients = info.get("clients_count", "?")
        start_time = info.get("start_time")
        uptime = format_uptime(start_time) if start_time and info["_running"] else "-"
        print(f"{alias:<12} {device:<20} {baud:<10} {pid:<8} {clients:<9} {uptime:<12} {status:<10}")


def cmd_status(args):
    """Show status of a specific daemon."""
    config = Config.load()
    info = resolve_alias(config, args.alias)
    if not info:
        print(f"Error: No daemon found for '{args.alias}'")
        sys.exit(1)

    alias = info.get("alias", args.alias)
    pid = info.get("pid", 0)
    running = is_running(pid)

    print(f"Alias:   {alias}")
    print(f"Device:  {info.get('device', '?')}")
    print(f"Baud:    {info.get('baud', '?')}")
    print(f"PID:     {pid}")
    print(f"Status:  {'running' if running else 'dead'}")
    print(f"Clients: {info.get('clients_count', '?')}")
    start_time = info.get("start_time")
    if start_time and running:
        print(f"Uptime:  {format_uptime(start_time)}")
    print(f"Socket:  {info.get('socket', '?')}")

    # Log info
    log_dir = config.logs_dir / alias
    if log_dir.exists():
        logs = sorted(log_dir.glob("*.log"))
        total_size = sum(f.stat().st_size for f in logs)
        print(f"Logs:    {len(logs)} files, {total_size / 1024:.1f} KB")


def cmd_set_baud(args):
    """Change baud rate of a running daemon."""
    config = Config.load()
    info = resolve_alias(config, args.alias)
    if not info:
        print(f"Error: No daemon found for '{args.alias}'")
        sys.exit(1)

    pid = info.get("pid", 0)
    alias = info.get("alias", args.alias)

    if not is_running(pid):
        print(f"Error: Daemon '{alias}' is not running")
        sys.exit(1)

    sock_path = info.get("socket")
    if not sock_path or not Path(sock_path).exists():
        print(f"Error: Socket not found for '{alias}'")
        sys.exit(1)

    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        sock.connect(sock_path)
        sock.settimeout(5.0)

        # Handshake
        sync_write_msg(sock, {"type": "hello"})
        resp = sync_read_msg(sock)
        if not resp or resp.get("type") != "hello_ack":
            print(f"Error: Unexpected response from daemon")
            sys.exit(1)

        # Drain the history message
        sync_read_msg(sock)

        # Send set_baud
        sync_write_msg(sock, {"type": "set_baud", "baud": args.baud})
        resp = sync_read_msg(sock)
        if resp and resp.get("type") == "baud_ack":
            print(f"Baud rate changed: {info.get('baud')} -> {resp['baud']}")
        elif resp and resp.get("type") == "error":
            print(f"Error: {resp.get('message')}")
            sys.exit(1)
        else:
            print(f"Error: Unexpected response: {resp}")
            sys.exit(1)
    except socket.timeout:
        print(f"Error: Timeout communicating with daemon")
        sys.exit(1)
    except ConnectionRefusedError:
        print(f"Error: Cannot connect to daemon '{alias}'")
        sys.exit(1)
    finally:
        sock.close()


def main():
    parser = argparse.ArgumentParser(prog="serial-mux", description="Serial port multiplexer")
    sub = parser.add_subparsers(dest="command", required=True)

    # start
    p_start = sub.add_parser("start", help="Start a daemon for a serial port")
    p_start.add_argument("device", help="Serial device path (e.g., /dev/ttyUSB0)")
    p_start.add_argument("--baud", "-b", type=int, help="Baud rate (default from config)")
    p_start.add_argument("--alias", "-a", help="Alias name (default: device basename)")
    p_start.add_argument("--foreground", "-f", action="store_true", help="Run in foreground")
    p_start.set_defaults(func=cmd_start)

    # stop
    p_stop = sub.add_parser("stop", help="Stop a daemon")
    p_stop.add_argument("alias", help="Alias or device path")
    p_stop.set_defaults(func=cmd_stop)

    # list
    p_list = sub.add_parser("list", help="List running daemons")
    p_list.set_defaults(func=cmd_list)

    # status
    p_status = sub.add_parser("status", help="Show daemon status")
    p_status.add_argument("alias", help="Alias or device path")
    p_status.set_defaults(func=cmd_status)

    # set-baud
    p_baud = sub.add_parser("set-baud", help="Change baud rate of a running daemon")
    p_baud.add_argument("alias", help="Alias or device path")
    p_baud.add_argument("baud", type=int, help="New baud rate")
    p_baud.set_defaults(func=cmd_set_baud)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
