"""serial-mux daemon: holds the serial port, fans out to clients via Unix socket."""

import asyncio
import json
import logging
import os
import signal
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

import serial

from .config import Config
from .protocol import (
    async_read_msg,
    async_write_msg,
    b64,
    unb64,
    HEADER_FMT,
    HEADER_SIZE,
)

logger = logging.getLogger("serial-mux-daemon")


class SerialDaemon:
    def __init__(self, device: str, baud: int, alias: str, config: Config):
        self.device = device
        self.baud = baud
        self.alias = alias
        self.config = config
        self.ser: Optional[serial.Serial] = None
        self.clients: list[asyncio.StreamWriter] = []
        self.log_lines: list[str] = []  # ring buffer for scrollback
        self.log_file = None
        self.log_date: Optional[str] = None
        self.running = False
        self.start_time = time.time()

    def _info_path(self) -> Path:
        return self.config.run_dir / f"{self.alias}.json"

    def _pid_path(self) -> Path:
        return self.config.run_dir / f"{self.alias}.pid"

    def _sock_path(self) -> Path:
        return self.config.sock_dir / f"{self.alias}.sock"

    def _log_dir(self) -> Path:
        return self.config.logs_dir / self.alias

    def _write_info(self):
        """Write alias info JSON."""
        info = {
            "alias": self.alias,
            "device": self.device,
            "baud": self.baud,
            "pid": os.getpid(),
            "socket": str(self._sock_path()),
        }
        self._info_path().write_text(json.dumps(info, indent=2))

    def _write_pid(self):
        self._pid_path().write_text(str(os.getpid()))

    def _cleanup_files(self):
        """Remove PID, socket, and info files."""
        for p in [self._pid_path(), self._sock_path(), self._info_path()]:
            try:
                p.unlink(missing_ok=True)
            except Exception:
                pass

    def _open_log(self):
        """Open or rotate log file based on current date."""
        today = datetime.now().strftime("%Y-%m-%d")
        if self.log_date == today and self.log_file:
            return
        if self.log_file:
            self.log_file.close()
        log_dir = self._log_dir()
        log_dir.mkdir(parents=True, exist_ok=True)
        log_path = log_dir / f"{today}.log"
        self.log_file = open(log_path, "a", encoding="utf-8", buffering=1)
        self.log_date = today

    def _purge_old_logs(self):
        """Delete logs older than retention period."""
        log_dir = self._log_dir()
        if not log_dir.exists():
            return
        cutoff = datetime.now() - timedelta(days=self.config.log_retention_days)
        for f in log_dir.glob("*.log"):
            try:
                file_date = datetime.strptime(f.stem, "%Y-%m-%d")
                if file_date < cutoff:
                    f.unlink()
                    logger.info(f"Purged old log: {f}")
            except ValueError:
                pass

    def _log_write(self, line: str):
        """Write a line to log file and ring buffer."""
        self._open_log()
        self.log_file.write(line + "\n")
        self.log_file.flush()
        self.log_lines.append(line)
        # Trim ring buffer
        max_lines = self.config.scrollback_lines
        if len(self.log_lines) > max_lines * 2:
            self.log_lines = self.log_lines[-max_lines:]

    def _load_history(self) -> list[str]:
        """Load recent history from today's (and yesterday's) log files."""
        lines = []
        log_dir = self._log_dir()
        if not log_dir.exists():
            return lines
        # Load last 2 days of logs
        dates = []
        for i in range(2):
            d = (datetime.now() - timedelta(days=i)).strftime("%Y-%m-%d")
            dates.append(d)
        dates.reverse()
        for d in dates:
            log_path = log_dir / f"{d}.log"
            if log_path.exists():
                with open(log_path, "r", encoding="utf-8", errors="replace") as f:
                    lines.extend(f.read().splitlines())
        # Keep only scrollback_lines
        if len(lines) > self.config.scrollback_lines:
            lines = lines[-self.config.scrollback_lines:]
        return lines

    def _timestamp(self) -> str:
        return datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    def _open_serial(self):
        """Open the serial port."""
        self.ser = serial.Serial(
            port=self.device,
            baudrate=self.baud,
            timeout=0.05,  # 50ms read timeout for polling
        )
        logger.info(f"Opened {self.device} at {self.baud} baud")

    async def _serial_reader(self):
        """Read from serial port and fan out to all clients."""
        loop = asyncio.get_event_loop()
        line_buf = bytearray()

        while self.running:
            try:
                data = await loop.run_in_executor(None, self._serial_read)
                if not data:
                    continue

                # Fan out raw data to all clients
                msg = {"type": "output", "data": b64(data)}
                await self._broadcast(msg)

                # Log line by line
                for byte in data:
                    if byte == ord("\n"):
                        line = line_buf.decode("utf-8", errors="replace").rstrip("\r")
                        ts = self._timestamp()
                        self._log_write(f"[{ts}] {line}")
                        line_buf.clear()
                    else:
                        line_buf.append(byte)

            except serial.SerialException as e:
                logger.error(f"Serial error: {e}")
                self.running = False
                break
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Serial reader error: {e}")
                await asyncio.sleep(0.1)

    def _serial_read(self) -> bytes:
        """Blocking serial read (called in executor)."""
        if self.ser and self.ser.is_open:
            try:
                data = self.ser.read(4096)
                return data if data else b""
            except Exception:
                return b""
        return b""

    async def _broadcast(self, msg: dict):
        """Send message to all connected clients."""
        dead = []
        for writer in list(self.clients):
            try:
                await async_write_msg(writer, msg)
            except Exception:
                dead.append(writer)
        for w in dead:
            self._remove_client(w)

    def _remove_client(self, writer: asyncio.StreamWriter):
        """Remove a disconnected client."""
        if writer in self.clients:
            self.clients.remove(writer)
        try:
            writer.close()
        except Exception:
            pass
        logger.info(f"Client disconnected. Active: {len(self.clients)}")

    async def _handle_client(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
        """Handle a single client connection."""
        try:
            # Expect hello message
            msg = await asyncio.wait_for(async_read_msg(reader), timeout=5.0)
            if msg.get("type") != "hello":
                await async_write_msg(writer, {"type": "error", "message": "Expected hello"})
                writer.close()
                return

            self.clients.append(writer)
            logger.info(f"Client connected. Active: {len(self.clients)}")

            # Send hello ack
            await async_write_msg(writer, {
                "type": "hello_ack",
                "alias": self.alias,
                "device": self.device,
                "baud": self.baud,
            })

            # Send history
            history = self._load_history()
            # Also include current ring buffer lines not yet in log
            all_lines = history
            if self.log_lines:
                # Merge: use ring buffer as it's more current
                all_lines = self.log_lines[-self.config.scrollback_lines:]
            await async_write_msg(writer, {"type": "history", "lines": all_lines})

            # Main client loop
            while self.running:
                try:
                    msg = await async_read_msg(reader)
                    if msg is None:
                        break
                    await self._handle_client_msg(msg, writer)
                except asyncio.IncompleteReadError:
                    break
                except Exception as e:
                    logger.error(f"Client error: {e}")
                    break

        except asyncio.TimeoutError:
            logger.warning("Client hello timeout")
        except asyncio.IncompleteReadError:
            pass
        except Exception as e:
            logger.error(f"Client handler error: {e}")
        finally:
            self._remove_client(writer)

    async def _handle_client_msg(self, msg: dict, writer: asyncio.StreamWriter):
        """Process a message from a client."""
        msg_type = msg.get("type")

        if msg_type == "input":
            data = unb64(msg["data"])
            # Write to serial
            if self.ser and self.ser.is_open:
                self.ser.write(data)
                self.ser.flush()

            # Log the command (detect newline to log as a line)
            text = data.decode("utf-8", errors="replace")
            if "\r" in text or "\n" in text:
                cmd = text.strip("\r\n")
                if cmd:
                    ts = self._timestamp()
                    self._log_write(f"[{ts}] {cmd}")

    async def run(self):
        """Main daemon entry point."""
        self.running = True
        self.config.ensure_dirs()
        self._write_info()
        self._write_pid()
        self._purge_old_logs()

        # Load existing history into ring buffer
        self.log_lines = self._load_history()

        # Open serial
        self._open_serial()

        # Remove stale socket
        sock_path = self._sock_path()
        sock_path.unlink(missing_ok=True)

        # Start Unix socket server
        server = await asyncio.start_unix_server(
            self._handle_client, path=str(sock_path)
        )
        # Make socket accessible
        os.chmod(str(sock_path), 0o660)

        logger.info(f"Daemon started: {self.alias} -> {self.device} @ {self.baud}")
        logger.info(f"Socket: {sock_path}")

        # Start serial reader
        serial_task = asyncio.create_task(self._serial_reader())

        # Setup signal handlers
        loop = asyncio.get_event_loop()
        for sig in (signal.SIGTERM, signal.SIGINT):
            loop.add_signal_handler(sig, lambda: self._shutdown())

        try:
            await server.serve_forever()
        except asyncio.CancelledError:
            pass
        finally:
            self.running = False
            serial_task.cancel()
            try:
                await serial_task
            except asyncio.CancelledError:
                pass
            server.close()
            await server.wait_closed()
            if self.ser and self.ser.is_open:
                self.ser.close()
            if self.log_file:
                self.log_file.close()
            self._cleanup_files()
            logger.info("Daemon stopped.")

    def _shutdown(self):
        """Signal handler to initiate shutdown."""
        logger.info("Shutdown signal received")
        self.running = False
        # Cancel the server
        for task in asyncio.all_tasks():
            task.cancel()


def daemonize():
    """Fork to background (classic double-fork)."""
    # Flush all buffers before forking
    sys.stdout.flush()
    sys.stderr.flush()

    # First fork
    pid = os.fork()
    if pid > 0:
        # Parent exits
        sys.exit(0)

    # New session
    os.setsid()

    # Second fork
    pid = os.fork()
    if pid > 0:
        sys.exit(0)

    # Redirect stdio to /dev/null
    sys.stdin.close()
    devnull = os.open(os.devnull, os.O_RDWR)
    os.dup2(devnull, 0)
    os.dup2(devnull, 1)
    os.dup2(devnull, 2)
    os.close(devnull)


def start_daemon(device: str, baud: int, alias: str, foreground: bool = False):
    """Start the daemon process."""
    config = Config.load()

    if not foreground:
        # Check if already running
        pid_path = config.run_dir / f"{alias}.pid"
        if pid_path.exists():
            try:
                pid = int(pid_path.read_text().strip())
                os.kill(pid, 0)  # Check if process exists
                print(f"Daemon for '{alias}' already running (PID {pid})")
                sys.exit(1)
            except (ProcessLookupError, ValueError):
                # Stale PID file
                pid_path.unlink(missing_ok=True)

        # Validate serial port access BEFORE daemonizing so errors are visible
        try:
            test_ser = serial.Serial(port=device, baudrate=baud, timeout=0.05)
            test_ser.close()
        except serial.SerialException as e:
            print(f"Error: Cannot open {device}: {e}")
            sys.exit(1)

        print(f"Starting daemon: {alias} -> {device} @ {baud}", flush=True)
        daemonize()

    # Setup logging
    log_level = logging.DEBUG if foreground else logging.INFO
    if foreground:
        logging.basicConfig(level=log_level, format="%(asctime)s %(levelname)s %(message)s")
    else:
        log_path = config.base_dir / "daemon.log"
        logging.basicConfig(
            level=log_level,
            format="%(asctime)s %(levelname)s %(message)s",
            filename=str(log_path),
        )

    daemon = SerialDaemon(device, baud, alias, config)
    try:
        asyncio.run(daemon.run())
    except Exception as e:
        logger.error(f"Daemon fatal error: {e}")
        daemon._cleanup_files()
        sys.exit(1)
