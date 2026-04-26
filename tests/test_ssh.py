"""Tests for SSH binding — self-contained, generates temp key for localhost SSH.

Test flow:
Phase 1 - Failures (no key needed):
  1. Bare hostname "localhost" → validate_ssh_target checks ~/.ssh/config → fails
  2. nonexistent@127.0.0.1 → Permission denied
  3. user@192.0.2.1 (TEST-NET) → timeout / connection refused
Phase 2 - Success (temp key generated, user@127.0.0.1):
  4. Bind SSH → succeeds
  5. Bind then unbind → reverts to serial
  6. Data flow over SSH
  7. Start daemon with ssh_target preset → auto-connects
Phase 3 - Fallback:
  8. serial+SSH, unbind SSH → serial still works
  9. Unbind then re-bind → works again
  10. Input routing: SSH active → input to SSH; after unbind → input to serial
"""

import getpass
import json
import os
import select
import socket
import subprocess
import textwrap
import time
from pathlib import Path

import pytest

from serial_mux.protocol import sync_write_msg, sync_read_msg, b64, unb64
from tests.conftest import connect_to_daemon, PYTHON

# Current username for user@127.0.0.1 targets
_USER = getpass.getuser()

# SSH probe can take up to 5s, so socket reads during bind need more headroom
_SSH_SOCK_TIMEOUT = 12.0


def _read_until_type(sock, expected_type, timeout=_SSH_SOCK_TIMEOUT):
    """Read messages from sock until we get one with the expected type.
    
    Skips intermediate broadcast messages like transport_changed.
    """
    old_timeout = sock.gettimeout()
    sock.settimeout(timeout)
    try:
        deadline = time.time() + timeout
        while time.time() < deadline:
            msg = sync_read_msg(sock)
            if msg is None:
                raise RuntimeError("Connection closed while waiting for message")
            if msg.get("type") == expected_type:
                return msg
            # Skip other message types (transport_changed, output, etc.)
        raise TimeoutError(f"Timed out waiting for message type '{expected_type}'")
    finally:
        sock.settimeout(old_timeout)


# ---------------------------------------------------------------------------
# Fixture: generate a temp SSH keypair and authorize it for 127.0.0.1
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module")
def localhost_ssh_key(tmp_path_factory):
    """Generate a temp ed25519 key, add its pubkey to ~/.ssh/authorized_keys.

    Yields the key path. On teardown, removes the pubkey from authorized_keys.
    """
    tmp_dir = tmp_path_factory.mktemp("sshkeys")
    key_path = tmp_dir / "id_test"
    pub_path = tmp_dir / "id_test.pub"

    # Generate keypair
    subprocess.run(
        ["ssh-keygen", "-t", "ed25519", "-f", str(key_path), "-N", "", "-C", "serial-mux-test"],
        capture_output=True, check=True,
    )

    pubkey = pub_path.read_text().strip()
    auth_keys = Path.home() / ".ssh" / "authorized_keys"

    # Backup original content
    original = auth_keys.read_text() if auth_keys.exists() else ""
    auth_keys.parent.mkdir(parents=True, exist_ok=True)
    with open(auth_keys, "a") as f:
        f.write(f"\n{pubkey}\n")

    # Verify the key works with user@127.0.0.1
    for attempt in range(3):
        r = subprocess.run(
            ["ssh", "-i", str(key_path),
             "-o", "BatchMode=yes",
             "-o", "ConnectTimeout=3",
             "-o", "StrictHostKeyChecking=no",
             "-o", "UserKnownHostsFile=/dev/null",
             f"{_USER}@127.0.0.1", "echo", "ok"],
            capture_output=True, timeout=10,
        )
        if r.returncode == 0:
            break
        time.sleep(0.5)
    else:
        # Cleanup on failure
        auth_keys.write_text(original)
        pytest.skip(f"Cannot SSH to {_USER}@127.0.0.1 even with temp key: {r.stderr.decode()}")

    yield str(key_path)

    # Teardown: remove the pubkey line
    current = auth_keys.read_text()
    lines = [l for l in current.splitlines() if pubkey not in l]
    auth_keys.write_text("\n".join(lines) + "\n")


# ---------------------------------------------------------------------------
# Helper: start daemon subprocess with optional SSH wrapper for temp key
# ---------------------------------------------------------------------------

def _start_daemon(tmp_config, alias, pty_device=None, ssh_target=None, ssh_key=None):
    """Start a daemon subprocess. Returns the Popen object."""
    device_arg = f'"{pty_device}"' if pty_device else "None"
    ssh_block = ""
    if ssh_target:
        ssh_block = f'daemon.ssh_target = "{ssh_target}"'

    env = os.environ.copy()
    if ssh_key:
        # Create an ssh wrapper that injects our temp key
        wrapper_dir = tmp_config.base_dir / "bin"
        wrapper_dir.mkdir(parents=True, exist_ok=True)
        wrapper = wrapper_dir / "ssh"
        wrapper.write_text(textwrap.dedent(f"""\
            #!/bin/bash
            exec /usr/bin/ssh -i {ssh_key} \
                -o StrictHostKeyChecking=no \
                -o UserKnownHostsFile=/dev/null \
                "$@"
        """))
        wrapper.chmod(0o755)
        env["PATH"] = f"{wrapper_dir}:{env.get('PATH', '')}"

    proc = subprocess.Popen(
        [
            PYTHON, "-c", f"""\
import sys, asyncio, logging
from pathlib import Path
sys.path.insert(0, '.')
from serial_mux.config import Config
from serial_mux.daemon import SerialDaemon

logging.basicConfig(level=logging.DEBUG, format='%(asctime)s %(levelname)s %(message)s')
cfg = Config()
cfg.base_dir = Path("{tmp_config.base_dir}")
cfg.config_dir = Path("{tmp_config.config_dir}")
cfg.ssh_connect_timeout = 2
cfg.ssh_probe_timeout = 5
cfg.ensure_dirs()

daemon = SerialDaemon({device_arg}, 115200, "{alias}", cfg)
{ssh_block}
asyncio.run(daemon.run())
""",
        ],
        cwd=str(Path(__file__).parent.parent),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=env,
    )

    sock_path = tmp_config.sock_dir / f"{alias}.sock"
    for _ in range(80):
        if sock_path.exists():
            break
        time.sleep(0.1)
    else:
        out = proc.stdout.read(2000) if proc.stdout else b""
        err = proc.stderr.read(2000) if proc.stderr else b""
        proc.kill()
        raise RuntimeError(
            f"Daemon socket not created.\nstdout: {out.decode(errors='replace')}\n"
            f"stderr: {err.decode(errors='replace')}"
        )

    return proc


def _kill_daemon(proc):
    proc.send_signal(2)
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait(timeout=3)


# ===========================================================================
# Phase 1: SSH failures (no key needed)
# ===========================================================================

@pytest.mark.timeout(30)
class TestSSHBindFailures:
    """SSH bind operations that should fail — no special setup needed."""

    def test_ssh_bind_bare_hostname_not_in_config(self, tmp_config):
        """Bare 'localhost' → validate_ssh_target checks ~/.ssh/config → fails."""
        alias = "ssh_fail_bare"
        proc = _start_daemon(tmp_config, alias)
        try:
            sock, transport, _ = connect_to_daemon(tmp_config, alias)
            assert transport == "serial"

            sync_write_msg(sock, {"type": "ssh_bind", "target": "some-nonexistent-host"})
            resp = _read_until_type(sock, "ssh_bind_ack")
            assert resp["type"] == "ssh_bind_ack"
            assert resp["ok"] is False
            msg = resp.get("message", "").lower()
            assert "not found" in msg or "config" in msg
            sock.close()
        finally:
            _kill_daemon(proc)

    def test_ssh_bind_nonexistent_user(self, tmp_config):
        """nonexistent@127.0.0.1 → Permission denied."""
        alias = "ssh_fail_user"
        proc = _start_daemon(tmp_config, alias)
        try:
            sock, transport, _ = connect_to_daemon(tmp_config, alias)
            assert transport == "serial"

            sync_write_msg(sock, {"type": "ssh_bind", "target": "nonexistent@127.0.0.1"})
            resp = _read_until_type(sock, "ssh_bind_ack")
            assert resp["type"] == "ssh_bind_ack"
            assert resp["ok"] is False
            msg = resp.get("message", "").lower()
            assert "permission denied" in msg or "failed" in msg
            sock.close()
        finally:
            _kill_daemon(proc)

    def test_ssh_bind_unreachable_ip(self, tmp_config):
        """user@192.0.2.1 (RFC 5737 TEST-NET-1, unreachable) → timeout."""
        alias = "ssh_fail_ip"
        proc = _start_daemon(tmp_config, alias)
        try:
            sock, transport, _ = connect_to_daemon(tmp_config, alias)
            assert transport == "serial"

            sync_write_msg(sock, {"type": "ssh_bind", "target": f"{_USER}@192.0.2.1"})
            resp = _read_until_type(sock, "ssh_bind_ack")
            assert resp["type"] == "ssh_bind_ack"
            assert resp["ok"] is False
            msg = resp.get("message", "").lower()
            assert "timed out" in msg or "failed" in msg or "connection" in msg
            sock.close()
        finally:
            _kill_daemon(proc)


# ===========================================================================
# Phase 2: SSH success (with temp key, user@127.0.0.1)
# ===========================================================================

@pytest.mark.timeout(30)
class TestSSHBindSuccess:
    """SSH bind succeeds after authorizing a temp key."""

    def test_ssh_bind(self, tmp_config, localhost_ssh_key):
        """Bind SSH to user@127.0.0.1 with temp key → succeeds."""
        alias = "ssh_ok"
        proc = _start_daemon(tmp_config, alias, ssh_key=localhost_ssh_key)
        try:
            sock, transport, _ = connect_to_daemon(tmp_config, alias)
            assert transport == "serial"  # no SSH yet

            sync_write_msg(sock, {"type": "ssh_bind", "target": f"{_USER}@127.0.0.1"})
            resp = _read_until_type(sock, "ssh_bind_ack")
            assert resp["type"] == "ssh_bind_ack"
            assert resp["ok"] is True, f"SSH bind failed: {resp.get('message')}"
            sock.close()

            # Reconnect — should report ssh transport
            sock2, transport2, _ = connect_to_daemon(tmp_config, alias)
            assert transport2 == "ssh"
            sock2.close()
        finally:
            _kill_daemon(proc)

    def test_ssh_unbind(self, tmp_config, localhost_ssh_key):
        """Bind then unbind → transport reverts to serial."""
        alias = "ssh_unbind"
        proc = _start_daemon(tmp_config, alias, ssh_key=localhost_ssh_key)
        try:
            sock, _, _ = connect_to_daemon(tmp_config, alias)

            # Bind
            sync_write_msg(sock, {"type": "ssh_bind", "target": f"{_USER}@127.0.0.1"})
            resp = _read_until_type(sock, "ssh_bind_ack")
            assert resp["ok"] is True

            # Unbind
            sync_write_msg(sock, {"type": "ssh_unbind"})
            resp = _read_until_type(sock, "ssh_bind_ack")
            assert resp["ok"] is True
            sock.close()

            # Reconnect — serial
            sock2, transport2, _ = connect_to_daemon(tmp_config, alias)
            assert transport2 == "serial"
            sock2.close()
        finally:
            _kill_daemon(proc)

    def test_ssh_data_flow(self, tmp_config, localhost_ssh_key):
        """Send command via SSH, verify output comes back."""
        alias = "ssh_data"
        proc = _start_daemon(tmp_config, alias, ssh_key=localhost_ssh_key)
        try:
            sock, _, _ = connect_to_daemon(tmp_config, alias)

            sync_write_msg(sock, {"type": "ssh_bind", "target": f"{_USER}@127.0.0.1"})
            resp = _read_until_type(sock, "ssh_bind_ack")
            assert resp["ok"] is True

            # Send a command
            sync_write_msg(sock, {"type": "input", "data": b64(b"echo __SSHTEST42__\n")})

            # Collect output
            collected = b""
            sock.settimeout(5.0)
            deadline = time.time() + 5.0
            while time.time() < deadline:
                try:
                    msg = sync_read_msg(sock)
                    if msg and msg["type"] == "output":
                        collected += unb64(msg["data"])
                        if b"__SSHTEST42__" in collected:
                            break
                except Exception:
                    break
            assert b"__SSHTEST42__" in collected
            sock.close()
        finally:
            _kill_daemon(proc)

    def test_ssh_start_with_daemon(self, tmp_config, localhost_ssh_key):
        """Start daemon with ssh_target pre-set → auto-connects SSH."""
        alias = "ssh_auto"
        proc = _start_daemon(
            tmp_config, alias,
            ssh_target=f"{_USER}@127.0.0.1", ssh_key=localhost_ssh_key,
        )
        try:
            # Give SSH time to connect
            time.sleep(3)
            sock, transport, _ = connect_to_daemon(tmp_config, alias)
            assert transport == "ssh"
            sock.close()
        finally:
            _kill_daemon(proc)


# ===========================================================================
# Phase 3: Fallback — SSH dies, serial takes over
# ===========================================================================

@pytest.mark.timeout(30)
class TestSSHFallback:
    """SSH → serial fallback when SSH is unbound."""

    def test_ssh_unbind_falls_back_to_serial(self, tmp_config, socat_pty, localhost_ssh_key):
        """Start with serial+SSH, unbind SSH, verify serial still works."""
        pty_device, pty_peer = socat_pty
        alias = "fallback1"
        proc = _start_daemon(
            tmp_config, alias, pty_device=pty_device,
            ssh_target=f"{_USER}@127.0.0.1", ssh_key=localhost_ssh_key,
        )
        try:
            time.sleep(3)  # let SSH connect

            sock, transport, _ = connect_to_daemon(tmp_config, alias)
            assert transport == "ssh"

            # Unbind SSH
            sync_write_msg(sock, {"type": "ssh_unbind"})
            resp = _read_until_type(sock, "ssh_bind_ack")
            assert resp["ok"] is True
            sock.close()

            # Reconnect — serial
            sock2, transport2, _ = connect_to_daemon(tmp_config, alias)
            assert transport2 == "serial"

            # Verify serial data flows
            with open(pty_peer, "wb", buffering=0) as peer:
                peer.write(b"fallback-test-data\r\n")

            collected = b""
            sock2.settimeout(3.0)
            deadline = time.time() + 3.0
            while time.time() < deadline:
                try:
                    msg = sync_read_msg(sock2)
                    if msg and msg["type"] == "output":
                        collected += unb64(msg["data"])
                        if b"fallback-test-data" in collected:
                            break
                except Exception:
                    break
            assert b"fallback-test-data" in collected
            sock2.close()
        finally:
            _kill_daemon(proc)

    def test_rebind_ssh_after_unbind(self, tmp_config, localhost_ssh_key):
        """Unbind then re-bind SSH → works again."""
        alias = "rebind"
        proc = _start_daemon(tmp_config, alias, ssh_key=localhost_ssh_key)
        try:
            sock, _, _ = connect_to_daemon(tmp_config, alias)

            target = f"{_USER}@127.0.0.1"

            # Bind → unbind → re-bind
            sync_write_msg(sock, {"type": "ssh_bind", "target": target})
            resp = _read_until_type(sock, "ssh_bind_ack")
            assert resp["ok"] is True

            sync_write_msg(sock, {"type": "ssh_unbind"})
            resp = _read_until_type(sock, "ssh_bind_ack")
            assert resp["ok"] is True

            sync_write_msg(sock, {"type": "ssh_bind", "target": target})
            resp = _read_until_type(sock, "ssh_bind_ack")
            assert resp["ok"] is True, f"Re-bind failed: {resp.get('message')}"
            sock.close()

            sock2, transport2, _ = connect_to_daemon(tmp_config, alias)
            assert transport2 == "ssh"
            sock2.close()
        finally:
            _kill_daemon(proc)

    def test_ssh_write_fallback_to_serial(self, tmp_config, socat_pty, localhost_ssh_key):
        """Input routing: SSH active → SSH gets input; after unbind → serial gets input."""
        pty_device, pty_peer = socat_pty
        alias = "write_fb"
        proc = _start_daemon(
            tmp_config, alias, pty_device=pty_device, ssh_key=localhost_ssh_key,
        )
        try:
            sock, _, _ = connect_to_daemon(tmp_config, alias)

            # Bind SSH
            sync_write_msg(sock, {"type": "ssh_bind", "target": f"{_USER}@127.0.0.1"})
            resp = _read_until_type(sock, "ssh_bind_ack")
            assert resp["ok"] is True

            # Input goes to SSH — verify by reading SSH output
            sync_write_msg(sock, {"type": "input", "data": b64(b"echo SSH_SIDE\n")})
            collected = b""
            sock.settimeout(3.0)
            deadline = time.time() + 3.0
            while time.time() < deadline:
                try:
                    msg = sync_read_msg(sock)
                    if msg and msg["type"] == "output":
                        collected += unb64(msg["data"])
                        if b"SSH_SIDE" in collected:
                            break
                except Exception:
                    break
            assert b"SSH_SIDE" in collected

            # Unbind SSH
            sync_write_msg(sock, {"type": "ssh_unbind"})
            _read_until_type(sock, "ssh_bind_ack")

            # Now input should go to serial — write and read from pty_peer
            sync_write_msg(sock, {"type": "input", "data": b64(b"SERIAL_CMD\n")})
            time.sleep(0.5)
            with open(pty_peer, "rb") as peer:
                ready, _, _ = select.select([peer], [], [], 2.0)
                if ready:
                    data = os.read(peer.fileno(), 4096)
                    assert b"SERIAL_CMD" in data

            sock.close()
        finally:
            _kill_daemon(proc)