"""Persistent daemon state and USB serial-port identity helpers."""

from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Optional


BOOT_ID_PATH = Path("/proc/sys/kernel/random/boot_id")
SYS_CLASS_TTY = Path("/sys/class/tty")
SYS_DEVICES = Path("/sys/devices")
DEV_DIR = Path("/dev")


def get_boot_id() -> Optional[str]:
    """Return the Linux boot ID, or ``None`` when it is unavailable."""
    try:
        value = BOOT_ID_PATH.read_text().strip()
        return value or None
    except OSError:
        return None


def info_is_running(info: dict) -> bool:
    """Check whether an info record belongs to a live daemon in this boot.

    The boot-ID check prevents a recycled PID after reboot from making an old
    saved mapping look like a running daemon.
    """
    if not isinstance(info, dict):
        return False

    saved_boot_id = info.get("boot_id")
    current_boot_id = get_boot_id()
    if saved_boot_id and current_boot_id and saved_boot_id != current_boot_id:
        return False

    pid = info.get("pid")
    if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0:
        return False
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def _read_sysfs_number(path: Path) -> Optional[str]:
    try:
        return str(int(path.read_text().strip()))
    except (OSError, ValueError):
        return None


def _read_sysfs_str(path: Path) -> Optional[str]:
    """Read a sysfs text attribute, returning None when absent or empty."""
    try:
        value = path.read_text().strip()
    except (OSError, ValueError):
        return None
    return value or None


def inspect_usb_device(device: Optional[str]) -> dict[str, str]:
    """Describe the physical USB port and current USB enumeration instance.

    ``usb_port`` identifies the controller/port/interface topology and remains
    stable across a host reboot. ``usb_instance`` uses the USB bus/device
    numbers and changes when the adapter is unplugged and plugged back in.
    """
    if not device:
        return {}

    try:
        tty_name = Path(device).resolve(strict=False).name
    except (OSError, RuntimeError):
        return {}
    return inspect_usb_tty(tty_name)


def inspect_usb_tty(tty_name: str) -> dict[str, str]:
    """Describe a TTY by its sysfs class name."""
    sysfs_link = SYS_CLASS_TTY / tty_name / "device"
    if not sysfs_link.exists():
        return {}

    try:
        tty_device = sysfs_link.resolve(strict=True)
    except (OSError, RuntimeError):
        return {}

    ancestors = [tty_device, *tty_device.parents]
    usb_device = next(
        (
            path
            for path in ancestors
            if (path / "busnum").is_file() and (path / "devnum").is_file()
        ),
        None,
    )
    if usb_device is None:
        return {}

    # A multi-interface USB adapter may expose more than one TTY. Include the
    # interface directory (for example 1-2:1.0) so those ports stay distinct.
    usb_interface = next(
        (
            path
            for path in ancestors
            if path.parent == usb_device and ":" in path.name
        ),
        usb_device,
    )
    try:
        usb_path = usb_interface.relative_to(SYS_DEVICES)
    except ValueError:
        usb_path = usb_interface

    # Retain the controller path so equal port numbers on different controllers
    # stay distinct, but normalize root-hub bus numbers that may change on boot.
    parts = usb_path.parts
    first_usb_part = next(
        (
            index
            for index, part in enumerate(parts)
            if re.fullmatch(r"\d+-\d+(?:\.\d+)*", part)
        ),
        None,
    )
    if first_usb_part is None:
        return {}
    prefix_parts = tuple(
        "usb" if re.fullmatch(r"usb\d+", part) else part
        for part in parts[:first_usb_part]
    )
    port_parts = parts[first_usb_part:]
    root_bus = port_parts[0].split("-", 1)[0]
    normalized_port = tuple(
        re.sub(rf"^{re.escape(root_bus)}-", "usb-", part)
        for part in port_parts
    )
    usb_port = "/".join((*prefix_parts, *normalized_port))

    busnum = _read_sysfs_number(usb_device / "busnum")
    devnum = _read_sysfs_number(usb_device / "devnum")
    if busnum is None or devnum is None:
        return {}

    result = {
        "usb_port": usb_port,
        "usb_instance": f"{busnum}:{devnum}",
    }
    # Device identity fields used to distinguish two adapters that may share a
    # physical port over time. ``serial`` is often absent (e.g. an FT232 with an
    # unprogrammed EEPROM); such devices can only be matched best-effort.
    vid = _read_sysfs_str(usb_device / "idVendor")
    pid = _read_sysfs_str(usb_device / "idProduct")
    serial_no = _read_sysfs_str(usb_device / "serial")
    if vid:
        result["usb_vid"] = vid
    if pid:
        result["usb_pid"] = pid
    if serial_no:
        result["usb_serial"] = serial_no
    return result


def find_usb_device(usb_port: str) -> tuple[Optional[str], dict[str, str]]:
    """Find the current ``/dev/tty*`` node attached to a saved USB port.

    Returns ``(None, {})`` when the port is absent or the match is ambiguous,
    so callers never silently pick an arbitrary device.
    """
    try:
        tty_entries = sorted(
            SYS_CLASS_TTY.iterdir(), key=lambda path: path.name
        )
    except OSError:
        return None, {}

    matches = []
    for entry in tty_entries:
        metadata = inspect_usb_tty(entry.name)
        if metadata.get("usb_port") == usb_port:
            matches.append((entry.name, metadata))

    if len(matches) == 1:
        name, metadata = matches[0]
        return str(DEV_DIR / name), metadata
    return None, {}


def resolve_recorded_device(info: dict) -> tuple[Optional[str], str]:
    """Resolve a saved USB port to its current TTY and classify its state.

    The returned status is one of ``plain``, ``connected``, ``rebooted``,
    ``unavailable``, ``disconnected``, or ``replugged``. The last two mean a
    hotplug event happened during the same boot and the mapping must be
    invalidated.
    """
    usb_port = info.get("usb_port")
    if not usb_port:
        # Legacy and non-USB serial devices still use their recorded path.
        return info.get("device"), "plain"

    current_device, current_usb = find_usb_device(usb_port)
    saved_boot_id = info.get("boot_id")
    current_boot_id = get_boot_id()
    same_boot = bool(
        saved_boot_id and current_boot_id and saved_boot_id == current_boot_id
    )

    if current_device is None:
        return None, "disconnected" if same_boot else "unavailable"

    if not same_boot and saved_boot_id and current_boot_id:
        return current_device, "rebooted"

    saved_instance = info.get("usb_instance")
    current_instance = current_usb.get("usb_instance")
    if same_boot and saved_instance and current_instance != saved_instance:
        return None, "replugged"
    return current_device, "connected"


def write_info(path: Path, info: dict) -> None:
    """Atomically write an alias info record."""
    tmp_path = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    tmp_path.write_text(json.dumps(info, indent=2))
    os.replace(tmp_path, path)


def mark_info_saved(path: Path) -> None:
    """Convert runtime metadata into an inactive, recoverable record.

    A USB mapping persists only its physical port identity. The old
    ``/dev/ttyUSB*`` name is deliberately discarded and will be rediscovered
    from sysfs the next time the alias is used.
    """
    try:
        info = json.loads(path.read_text())
    except (json.JSONDecodeError, OSError):
        return
    if not isinstance(info, dict):
        return

    if info.get("usb_port"):
        info["device"] = None
    elif not info.get("device") and not info.get("ssh"):
        # A live daemon may remain as an empty container after its USB mapping
        # is invalidated. There is nothing recoverable once that daemon exits.
        path.unlink(missing_ok=True)
        return
    info["pid"] = None
    info["start_time"] = None
    info["clients_count"] = 0
    write_info(path, info)


def clear_serial_mapping(path: Path, info: dict) -> Optional[dict]:
    """Remove a stale USB mapping, retaining a live/SSH alias container."""
    updated = dict(info)
    updated["device"] = None
    updated.pop("usb_port", None)
    updated.pop("usb_instance", None)

    if not updated.get("ssh") and not info_is_running(updated):
        path.unlink(missing_ok=True)
        return None

    write_info(path, updated)
    return updated


def reconcile_info(path: Path, info: dict) -> Optional[dict]:
    """Validate a saved USB mapping and resolve its current device node.

    A resolved TTY is returned only to the caller and is never persisted as a
    recovery key. The daemon writes a fresh boot ID and USB instance only
    after it has reopened the serial port successfully.
    """
    if not isinstance(info, dict):
        return None
    result = dict(info)
    if info_is_running(result):
        result["_device_status"] = "running"
        return result

    # A crash or power loss may have left the last runtime TTY in the JSON.
    # Sanitize it before doing any recovery; usb_port remains the sole key.
    if result.get("usb_port") and (
        result.get("device") is not None
        or result.get("pid") is not None
        or result.get("start_time") is not None
        or result.get("clients_count") not in (None, 0)
    ):
        mark_info_saved(path)
        result["device"] = None
        result["pid"] = None
        result["start_time"] = None
        result["clients_count"] = 0

    device, status = resolve_recorded_device(result)
    if status in {"disconnected", "replugged"}:
        result = clear_serial_mapping(path, result)
        if result is not None:
            result["_device_status"] = status
        return result

    if status == "unavailable":
        result["device"] = None
    elif device:
        result["device"] = device
    result["_device_status"] = status
    return result
