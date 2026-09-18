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
from .state import info_is_running, reconcile_info


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
        try:
            return reconcile_info(info_path, json.loads(info_path.read_text()))
        except (json.JSONDecodeError, OSError):
            return None
    # Try scanning all info files for matching device
    for f in config.run_dir.glob("*.json"):
        try:
            info = reconcile_info(f, json.loads(f.read_text()))
            if info and info.get("device") == alias_or_device:
                return info
        except (json.JSONDecodeError, OSError):
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

    device = getattr(args, 'device', None)
    alias = args.alias
    ssh_target = getattr(args, 'ssh', None)
    saved = resolve_alias(config, alias) if alias else None

    if saved and info_is_running(saved):
        print(
            f"Error: Alias '{saved.get('alias', alias)}' already running "
            f"(PID {saved.get('pid')})"
        )
        sys.exit(1)

    # ``start --alias NAME`` restores a saved mapping. This is also what the
    # client uses after a reboot. For USB records, resolve_alias() has derived
    # this device from usb_port; the old persisted TTY name is never used.
    if saved:
        if device is None:
            device = saved.get("device")
        if ssh_target is None:
            ssh_target = saved.get("ssh")

    # Starting by device without an explicit alias reuses the name previously
    # assigned to the same physical USB port.
    if not alias and device:
        saved = resolve_alias(config, device)
        if saved:
            alias = saved.get("alias")

    baud = (
        args.baud
        or (saved.get("baud") if saved else None)
        or config.default_baud
    )

    if not device and not ssh_target:
        if saved and saved.get("_device_status") == "unavailable":
            print(
                f"Error: Saved USB port for '{saved.get('alias')}' "
                "is not available"
            )
            sys.exit(1)
        print("Error: At least one of DEVICE or --ssh must be specified")
        sys.exit(1)

    if not alias:
        if device:
            # Derive alias from device name
            alias = Path(device).name  # e.g., ttyUSB0
        else:
            print("Error: --alias is required when starting without a serial device")
            sys.exit(1)

    # Check device exists
    if device and not Path(device).exists():
        print(f"Error: Device {device} not found")
        sys.exit(1)

    # Check if alias already in use
    existing = resolve_alias(config, alias)
    if existing:
        pid = existing.get("pid", 0)
        if info_is_running(existing):
            print(f"Error: Alias '{alias}' already running (PID {pid})")
            sys.exit(1)
        else:
            # Keep the JSON recovery record until the replacement daemon has
            # successfully opened its transport.
            (config.run_dir / f"{alias}.pid").unlink(missing_ok=True)
            (config.sock_dir / f"{alias}.sock").unlink(missing_ok=True)

    # Import and start daemon
    from .daemon import start_daemon
    start_daemon(
        device,
        baud,
        alias,
        foreground=args.foreground,
        ssh_target=ssh_target,
    )


def _remove_alias_files(config: Config, alias: str, remove_info: bool = True):
    """Remove transient daemon files and, for an explicit stop, its mapping."""
    paths = [
        config.run_dir / f"{alias}.pid",
        config.sock_dir / f"{alias}.sock",
    ]
    if remove_info:
        paths.append(config.run_dir / f"{alias}.json")
    for path in paths:
        path.unlink(missing_ok=True)


def cmd_stop(args):
    """Stop a daemon."""
    config = Config.load()
    info = resolve_alias(config, args.alias)
    if not info:
        print(f"Error: No daemon found for '{args.alias}'")
        sys.exit(1)

    pid = info.get("pid", 0)
    alias = info.get("alias", args.alias)

    if not info_is_running(info):
        print(f"Daemon '{alias}' not running; removing saved mapping")
        _remove_alias_files(config, alias)
        return

    os.kill(pid, signal.SIGTERM)
    # Wait for process to exit
    for _ in range(30):
        if not is_running(pid):
            _remove_alias_files(config, alias)
            print(f"Daemon '{alias}' stopped")
            return
        time.sleep(0.1)
    print(f"Warning: Daemon '{alias}' (PID {pid}) did not stop in 3s, sending SIGKILL")
    try:
        os.kill(pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    _remove_alias_files(config, alias)
    print(f"Daemon '{alias}' killed")


def _prune_stale(config: Config):
    """Remove stale PID/socket files while retaining recoverable mappings."""
    for f in list(config.run_dir.glob("*.json")):
        try:
            info = reconcile_info(f, json.loads(f.read_text()))
            if info and not info_is_running(info):
                alias = info.get("alias", f.stem)
                _remove_alias_files(config, alias, remove_info=False)
        except (json.JSONDecodeError, OSError):
            pass


def cmd_list(args):
    """List running daemons and saved mappings."""
    config = Config.load()
    _prune_stale(config)
    infos = []
    for f in sorted(config.run_dir.glob("*.json")):
        try:
            info = reconcile_info(f, json.loads(f.read_text()))
            if info:
                info["_running"] = info_is_running(info)
                infos.append(info)
        except (json.JSONDecodeError, OSError):
            pass

    if not infos:
        print("No daemons or saved mappings")
        return

    print(f"{'ALIAS':<12} {'DEVICE':<20} {'BAUD':<10} {'PID':<8} {'CLIENTS':<9} {'UPTIME':<12} {'STATUS':<10} {'SSH':<20}")
    print("-" * 101)
    for info in infos:
        alias = info.get("alias", "?")
        device = info.get("device") or "-"
        baud = info.get("baud", "?")
        pid = info.get("pid", "?") if info["_running"] else "-"
        status = "running" if info["_running"] else "saved"
        clients = info.get("clients_count", "?") if info["_running"] else "-"
        start_time = info.get("start_time")
        uptime = format_uptime(start_time) if start_time and info["_running"] else "-"
        ssh = info.get("ssh") or "-"
        print(f"{alias:<12} {device:<20} {baud:<10} {pid:<8} {clients:<9} {uptime:<12} {status:<10} {ssh:<20}")


def cmd_status(args):
    """Show status of a specific daemon."""
    config = Config.load()
    info = resolve_alias(config, args.alias)
    if not info:
        print(f"Error: No daemon found for '{args.alias}'")
        sys.exit(1)

    alias = info.get("alias", args.alias)
    pid = info.get("pid", 0)
    running = info_is_running(info)

    print(f"Alias:   {alias}")
    print(f"Device:  {info.get('device') or 'none'}")
    print(f"Baud:    {info.get('baud', '?')}")
    print(f"PID:     {pid if running else 'none'}")
    print(f"Status:  {'running' if running else 'saved'}")
    print(f"Clients: {info.get('clients_count', '?') if running else '-'}")
    start_time = info.get("start_time")
    if start_time and running:
        print(f"Uptime:  {format_uptime(start_time)}")
    print(f"Socket:  {info.get('socket', '?')}")
    usb_port = info.get("usb_port")
    if usb_port:
        print(f"USB Port: {usb_port}")
    ssh = info.get("ssh")
    print(f"SSH:     {ssh if ssh else 'none'}")

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

    if not info_is_running(info):
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
        sync_write_msg(sock, {"type": "hello", "interactive": False})
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


def _send_daemon_msg(alias: str, msg: dict, expect_type: str = None) -> dict:
    """Connect to daemon, handshake, send a message, return response.

    If expect_type is set, skip intermediate broadcast messages until we get
    a message of that type (or timeout).
    """
    config = Config.load()
    info = resolve_alias(config, alias)
    if not info:
        print(f"Error: No daemon found for '{alias}'")
        sys.exit(1)
    pid = info.get("pid", 0)
    if not info_is_running(info):
        print(f"Error: Daemon '{info.get('alias', alias)}' is not running")
        sys.exit(1)
    sock_path = info.get("socket")
    if not sock_path or not Path(sock_path).exists():
        print(f"Error: Socket not found for '{info.get('alias', alias)}'")
        sys.exit(1)
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        sock.connect(sock_path)
        sock.settimeout(15.0)
        sync_write_msg(sock, {"type": "hello", "interactive": False})
        resp = sync_read_msg(sock)
        if not resp or resp.get("type") != "hello_ack":
            print(f"Error: Unexpected response from daemon")
            sys.exit(1)
        sync_read_msg(sock)  # drain history
        sync_write_msg(sock, msg)
        if expect_type:
            # Skip broadcast messages until we get the expected response type
            for _ in range(10):
                resp = sync_read_msg(sock)
                if not resp:
                    return None
                if resp.get("type") == expect_type:
                    return resp
            return resp  # give up, return last message
        resp = sync_read_msg(sock)
        return resp
    except socket.timeout:
        print(f"Error: Timeout communicating with daemon")
        sys.exit(1)
    except ConnectionRefusedError:
        print(f"Error: Cannot connect to daemon")
        sys.exit(1)
    finally:
        sock.close()


def cmd_ssh_bind(args):
    """Bind SSH to a running daemon."""
    resp = _send_daemon_msg(args.alias, {"type": "ssh_bind", "target": args.ssh_target},
                            expect_type="ssh_bind_ack")
    if resp and resp.get("ok"):
        print(resp.get("message", "SSH bound"))
    elif resp and resp.get("type") == "ssh_bind_ack":
        print(f"Error: {resp.get('message', 'SSH bind failed')}")
        sys.exit(1)
    else:
        print(f"Error: Unexpected response: {resp}")
        sys.exit(1)


def cmd_ssh_unbind(args):
    """Unbind SSH from a running daemon."""
    resp = _send_daemon_msg(args.alias, {"type": "ssh_unbind"})
    if resp and resp.get("ok"):
        print(resp.get("message", "SSH unbound"))
    else:
        print(f"Error: Unexpected response: {resp}")
        sys.exit(1)


def cmd_serial_bind(args):
    """Bind a serial port to a running daemon."""
    msg = {"type": "serial_bind", "device": args.device}
    if args.baud:
        msg["baud"] = args.baud
    resp = _send_daemon_msg(args.alias, msg)
    if resp and resp.get("ok"):
        print(resp.get("message", "Serial bound"))
    elif resp and resp.get("type") == "serial_bind_ack":
        print(f"Error: {resp.get('message', 'Serial bind failed')}")
        sys.exit(1)
    else:
        print(f"Error: Unexpected response: {resp}")
        sys.exit(1)


def cmd_serial_unbind(args):
    """Unbind serial port from a running daemon."""
    resp = _send_daemon_msg(args.alias, {"type": "serial_unbind"})
    if resp and resp.get("ok"):
        print(resp.get("message", "Serial unbound"))
    else:
        print(f"Error: Unexpected response: {resp}")
        sys.exit(1)


def main():
    parser = argparse.ArgumentParser(prog="serial-mux", description="Serial port multiplexer")
    sub = parser.add_subparsers(dest="command", required=True)

    # start
    p_start = sub.add_parser("start", help="Start a daemon for a serial port")
    p_start.add_argument("device", nargs="?", default=None, help="Serial device path (e.g., /dev/ttyUSB0). Optional if --ssh is given.")
    p_start.add_argument("--baud", "-b", type=int, help="Baud rate (default from config)")
    p_start.add_argument("--alias", "-a", help="Alias name (default: device basename, required if no device)")
    p_start.add_argument("--foreground", "-f", action="store_true", help="Run in foreground")
    p_start.add_argument("--ssh", default=None, help="SSH target (user@host or ssh-config hostname)")
    p_start.set_defaults(func=cmd_start)

    # stop
    p_stop = sub.add_parser("stop", help="Stop a daemon")
    p_stop.add_argument("alias", help="Alias or device path")
    p_stop.set_defaults(func=cmd_stop)

    # list
    p_list = sub.add_parser("list", help="List daemons and saved mappings")
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

    # ssh-bind
    p_ssh_bind = sub.add_parser("ssh-bind", help="Bind SSH to a running daemon")
    p_ssh_bind.add_argument("alias", help="Alias or device path")
    p_ssh_bind.add_argument("ssh_target", help="SSH target (user@host or ssh-config hostname)")
    p_ssh_bind.set_defaults(func=cmd_ssh_bind)

    # ssh-unbind
    p_ssh_unbind = sub.add_parser("ssh-unbind", help="Unbind SSH from a running daemon")
    p_ssh_unbind.add_argument("alias", help="Alias or device path")
    p_ssh_unbind.set_defaults(func=cmd_ssh_unbind)

    # serial-bind
    p_serial_bind = sub.add_parser("serial-bind", help="Bind a serial port to a running daemon")
    p_serial_bind.add_argument("alias", help="Alias")
    p_serial_bind.add_argument("device", help="Serial device path (e.g., /dev/ttyUSB0)")
    p_serial_bind.add_argument("--baud", "-b", type=int, default=None, help="Baud rate (default: daemon's current)")
    p_serial_bind.set_defaults(func=cmd_serial_bind)

    # serial-unbind
    p_serial_unbind = sub.add_parser("serial-unbind", help="Unbind serial port from a running daemon")
    p_serial_unbind.add_argument("alias", help="Alias")
    p_serial_unbind.set_defaults(func=cmd_serial_unbind)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
