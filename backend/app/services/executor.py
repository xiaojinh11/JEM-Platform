"""Executor.

This module is the bridge between Adapter (declarative) and the actual SSH
transport. All Paramiko / Netmiko calls live here, with adapter-specific
command sets.
"""
from __future__ import annotations

import io
import socket
import threading
import time
from dataclasses import dataclass
from typing import Iterable, Optional

from app.core.config import settings
from app.core.enums import Vendor
from app.core.logging import logger
from app.models.credential import Credential
from app.core.security import decrypt_secret

try:  # optional dependency to stay import-clean in CI
    import paramiko as _paramiko  # type: ignore
    paramiko = _paramiko
    _HAVE_PARAMIKO = True
except Exception:  # noqa: BLE001
    _HAVE_PARAMIKO = False
    paramiko = None  # type: ignore


# Pin legacy SHA-1 KEX algorithms to the top of paramiko's preference list
# so that older Huawei VRP / Cisco IOS XR firmware (which only advertises
# ``diffie-hellman-group1-sha1`` / ``group14-sha1`` / ``group-exchange-sha1``)
# can complete the SSH handshake.  paramiko 5.x dropped these algorithms
# entirely, so this module requires paramiko < 5; the patch below is a
# no-op on installs where these KEX entries aren't recognised.
if _HAVE_PARAMIKO:
    _legacy_kex = (
        "diffie-hellman-group1-sha1",
        "diffie-hellman-group14-sha1",
        "diffie-hellman-group-exchange-sha1",
    )
    try:
        _existing = list(paramiko.Transport._preferred_kex)
        # put legacy entries first, but only ones that exist in _kex_info
        # (otherwise paramiko will refuse to negotiate them)
        _supported = set(paramiko.Transport._kex_info.keys())
        _legacy_first = tuple(k for k in _legacy_kex if k in _supported)
        _rest = tuple(k for k in _existing if k not in _legacy_first)
        paramiko.Transport._preferred_kex = _legacy_first + _rest
        logger.info(
            "executor: legacy KEX pinned to top: {}",
            _legacy_first or "(none — paramiko lacks SHA-1 KEX support)",
        )
    except Exception as exc:  # noqa: BLE001
        # Paramiko doesn't expose this on the active version; silently
        # fall back to defaults rather than crash on import.
        logger.debug("executor: legacy KEX patch failed: {}", exc)


class _AlertOnChangePolicy((_paramiko.AutoAddPolicy if _HAVE_PARAMIKO else object)):
    """Trust-on-first-use host key policy with MITM-change alerts.

    - New host: silently accept (auto-add), log INFO so operators know
      the trust was just bootstrapped.
    - Already-known host whose key CHANGED: reject the connection and
      log ERROR — that's the canary for a man-in-the-middle attempt.
    - Already-known host with the SAME key: accept (normal path).
    """

    def missing_host_key(self, client: "paramiko.SSHClient", hostname: str, key) -> None:  # type: ignore[override]
        # ``client`` already loaded system_host_keys above; consult them.
        existing = None
        try:
            # paramiko 3.x exposes ``get_host_key(host, port)``
            existing = client.get_host_key(hostname)
        except Exception:  # noqa: BLE001
            existing = None

        if existing is not None and existing.get_fingerprint() != key.get_fingerprint():
            fingerprint_b64 = key.get_base64()
            logger.error(
                "SSH host key CHANGED for {} (suspected MITM) — new fingerprint={}",
                hostname,
                fingerprint_b64,
            )
            # Raising here aborts the connection — the SSHClient will
            # never see the new key.  This matches paramiko's
            # RejectPolicy behaviour for the change case while keeping
            # the auto-add path for fresh hosts.
            raise paramiko.SSHException(
                f"host key for {hostname} does not match the trusted fingerprint; "
                "investigate possible MITM"
            )

        # First-time host OR same-key repeat: silently accept.
        super().missing_host_key(client, hostname, key)
        logger.info("SSH: trusting host key for {} ({})", hostname, key.get_name())


class ExecutorError(RuntimeError):
    pass


@dataclass
class SSHOptions:
    username: str
    password: Optional[str] = None
    ssh_key: Optional[str] = None
    enable_password: Optional[str] = None
    timeout: int = 10


def _make_ssh_client(opt: SSHOptions) -> "paramiko.SSHClient":
    if not _HAVE_PARAMIKO:
        raise ExecutorError("paramiko is not installed in this environment")
    client = paramiko.SSHClient()
    client.load_system_host_keys()
    # P0 Security: trust-on-first-use (AutoAddPolicy) instead of hard
    # RejectPolicy — operational reality is that new devices arrive
    # regularly and bootstrapping known_hosts by hand doesn't scale.
    # We compensate with a custom policy that ALERTS on any change to
    # an already-trusted host (TOFU → MIDM detection).
    client.set_missing_host_key_policy(_AlertOnChangePolicy())
    pkey = None
    if opt.ssh_key:
        for loader in (paramiko.Ed25519Key, paramiko.RSAKey):
            try:
                pkey = loader.from_private_key(io.StringIO(opt.ssh_key))
                break
            except Exception:  # noqa: BLE001
                continue
        if pkey is None:
            raise ExecutorError("unsupported SSH private key format")
    logger.info(
        "ssh.connect start: host={}:{} user={} timeout={}s",
        opt.address, opt.port, opt.username, opt.timeout,
    )

    # Huawei eNSP legacy devices require SHA-1 KEX (Paramiko 3+ disabled by default)
    # Explicitly allow deprecated algorithms for network automation compatibility
    disabled_algos = {
        'kex': [],  # Allow all KEX including diffie-hellman-group1-sha1
        'pubkeys': [],
        'ciphers': [],
        'macs': [],
    }

    try:
        client.connect(
            hostname=opt.address,
            port=opt.port,
            username=opt.username,
            password=opt.password,
            pkey=pkey,
            timeout=opt.timeout,
            allow_agent=False,
            look_for_keys=False,
            banner_timeout=opt.timeout,
            auth_timeout=opt.timeout,
            disabled_algorithms=disabled_algos,  # Enable legacy KEX for old devices
        )
    except (paramiko.SSHException, socket.error, OSError) as e:
        # 详细诊断:TimeoutError/Banner/NoValidConnections/Auth/协商失败
        # 都能在这里被区分出来,而不是只看到一个笼统的 "timed out"。
        # 分类:
        #   - socket.timeout / TimeoutError → 拿不到 banner 或 auth 阶段卡住
        #   - NoValidConnectionsError       → 目标端口拒绝/路由不可达
        #   - AuthenticationException      → 用户名/密码/密钥错
        #   - BadHostKeyException           → host key 变更(MITM 嫌疑)
        #   - SSHException (无子类)         → 协议层(算法协商/版本等)
        logger.error(
            "ssh.connect failed: host={}:{} user={} err_type={} msg={!r}",
            opt.address, opt.port, opt.username,
            type(e).__name__, str(e),
        )
        # 顺手把 paramiko 的 transport 协商状态也打一下(若有)
        try:
            trans = client.get_transport()
            if trans is not None:
                logger.error(
                    "ssh.connect transport: active={} negotiated={}",
                    trans.is_active(),
                    trans.get_security_options().__dict__ if hasattr(trans, 'get_security_options') else 'n/a',
                )
        except Exception:  # noqa: BLE001
            pass
        raise
    logger.info("ssh.connect ok: host={}:{} user={}", opt.address, opt.port, opt.username)
    # Long-running sessions (e.g. WebSSH) can be silently dropped by NAT /
    # stateful firewalls. Enable paramiko's transport keepalive so dead
    # connections are detected within a minute rather than blocking on the
    # next ``recv()``.  ``disabled`` is honoured when ``opt.timeout == 0``.
    transport = client.get_transport()
    if transport is not None and opt.timeout > 0:
        try:
            # Floor of 15 s was set when the only concern was stateful
            # firewall idle timeouts on the public internet (which are
            # typically 60-120 s).  We also serve eNSP virtual devices,
            # which close the SSH channel after ~28 s of operator idle
            # or after the first error response — well within the
            # 28 s window would the floor of 15 s actually be aggressive
            # enough.  5 s gives paramiko time to send at least two
            # probes inside eNSP's close window so the read loop
            # notices the death and the banner / re-connect button
            # appears within seconds rather than the operator having
            # to wait for the next operator keystroke to fail.
            transport.set_keepalive(int(max(5, opt.timeout // 3)))
        except Exception as exc:  # noqa: BLE001
            logger.debug("ssh keepalive not enabled: {}", exc)
    return client


@dataclass
class _ChannelShell:
    client: "paramiko.SSHClient"
    channel: "paramiko.Channel"
    recv_buf: io.BytesIO
    _closed: bool = False

    def read(self, size: int = 4096, timeout: float = 0.1) -> bytes:
        if self._closed:
            return b""
        self.channel.settimeout(timeout)
        try:
            data = self.channel.recv(size)
        except (socket.timeout, BlockingIOError):
            return b""
        return data or b""

    def write(self, data: bytes) -> int:
        if self._closed:
            return 0
        try:
            return self.channel.send(data)
        except EOFError:
            # Remote closed cleanly (e.g. eNSP 28s timeout, VTY limit, explicit quit)
            self._closed = True
            return 0
        except OSError as e:
            # Only treat EPIPE/ECONNRESET as closed, re-raise others
            import errno
            if e.errno in (errno.EPIPE, errno.ECONNRESET, errno.ESHUTDOWN):
                self._closed = True
                return 0
            raise  # Transient errors (EAGAIN, etc.) propagate

    def resize(self, cols: int, rows: int) -> None:
        try:
            self.channel.resize_pty(width=cols, height=rows)
        except Exception as exc:  # noqa: BLE001
            logger.warning("pty resize failed: {}", exc)

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            self.channel.close()
        except Exception:  # noqa: BLE001
            pass
        try:
            self.client.close()
        except Exception:  # noqa: BLE001
            pass


def _build_collector(vendor: Vendor):
    """Return (collect_command, apply_pattern) for the vendor."""
    if vendor == Vendor.HUAWEI_VRP:
        return ("display current-configuration", ("system-view\n", "return\n"))
    if vendor == Vendor.CISCO_IOS_XE:
        return ("show running-config", ("configure terminal\n", "end\n"))
    if vendor == Vendor.RUIJIE_RGOS:
        return ("show running-config", ("configure terminal\n", "end\n"))
    raise ExecutorError(f"unsupported vendor {vendor}")


def collect_config(
    *,
    vendor: Vendor,
    address: str,
    port: int,
    credential: Credential,
    timeout_override: Optional[int] = None,
) -> str:
    """Open a shell, run 'display current-configuration' (or vendor equivalent), return text.

    No stub: simulated vendor is rejected because we have no in-process SSH
    server and any "fake config" returned here would be persisted as if it
    were real, masking transport failures from the operator.
    """
    if vendor == Vendor.SIMULATED:
        raise ExecutorError(
            f"collect_config: vendor=SIMULATED is not supported — "
            f"address={address}:{port} has no real SSH transport. "
            f"Configure a real device (huawei_vrp / cisco_ios_xe / ruijie_rgos) "
            f"or fix the device's endpoint/credential before retrying."
        )
    opt = SSHOptions(
        username=credential.username,
        password=decrypt_secret(credential.secret_enc) if credential.secret_kind == "password" else None,
        ssh_key=decrypt_secret(credential.secret_enc) if credential.secret_kind == "ssh_key" else None,
        enable_password=decrypt_secret(credential.enable_password_enc) if credential.enable_password_enc else None,
        timeout=timeout_override if timeout_override is not None else settings.ssh_timeout_seconds,
    )
    opt.address = address  # type: ignore[attr-defined]
    opt.port = port  # type: ignore[attr-defined]
    client = _make_ssh_client(opt)
    try:
        collect_cmd, _ = _build_collector(vendor)
        out = _exec_collect(client, vendor, collect_cmd)
        return out
    finally:
        client.close()


def _exec_collect(client, vendor: Vendor, cmd: str) -> str:
    """Run a single read-only command and capture the buffered output.

    Handles vendor paging prompts by detecting ``---- More ----`` (Huawei VRP)
    or ``--More--`` (Cisco IOS XE / Ruijie RGOS) in the accumulated output
    and sending a space character to advance to the next page.  Without
    this, any device whose ``display current-configuration`` exceeds the
    terminal page size (~24 rows on eNSP AR routers) would only return
    the first screen of output.
    """
    chan = client.invoke_shell(term="vt100", width=200, height=50)
    chan.settimeout(2.0)
    captured = io.BytesIO()
    chan.send(cmd.encode() + b"\n")
    end = time.time() + settings.ssh_read_timeout_seconds  # Use configurable read timeout
    last = time.time()
    more_markers = _paging_markers(vendor)
    while time.time() < end:
        try:
            data = chan.recv(8192)
            if not data:
                time.sleep(0.1)
            else:
                captured.write(data)
                last = time.time()
                # If a paging prompt is visible, advance the page.  The
                # captured buffer is decoded lazily below so the marker
                # check is byte-level on the latest chunk (markers are
                # ASCII so the slice is unambiguous even mid-multibyte
                # sequence).
                if any(m in data for m in more_markers):
                    chan.send(b" ")
                    # Reset the idle counter — the device is now busy
                    # generating the next page.
                    last = time.time()
        except socket.timeout:
            pass
        if time.time() - last > 1.2:
            break
    chan.close()
    return _strip_paging(captured.getvalue().decode("utf-8", errors="replace"), vendor)


def _paging_markers(vendor: Vendor) -> tuple[bytes, ...]:
    """Return byte-strings that, when seen in a chunk from the channel,
    indicate a vendor-specific pagination prompt that needs to be
    advanced by sending a space (HUAWEI / IOS / RGOS) or a CR
    (future-proofing: e.g. ``press any key`` style prompts).

    Returning a tuple of ``bytes`` (not str) lets the caller match
    against raw ``chan.recv()`` output without an intermediate decode.
    """
    if vendor == Vendor.HUAWEI_VRP:
        return (b"---- More ----",)
    if vendor in (Vendor.CISCO_IOS_XE, Vendor.RUIJIE_RGOS):
        return (b"--More--", b"--- More ---")
    return ()


def _strip_paging(text: str, vendor: Vendor) -> str:
    """Strip terminal control sequences and pagination prompts."""
    import re
    # ANSI escape sequences
    text = re.sub(r"\x1b\[[0-9;?]*[A-Za-z]", "", text)
    text = re.sub(r"\x1b\([AB]", "", text)
    if vendor in (Vendor.HUAWEI_VRP,):
        text = re.sub(r"---- More ----.*?\r", "", text)
    elif vendor in (Vendor.CISCO_IOS_XE, Vendor.RUIJIE_RGOS):
        text = re.sub(r"--More--.*?\r", "", text)
    return text.strip("\r\n")


def apply_config(
    *,
    vendor: Vendor,
    address: str,
    port: int,
    credential: Credential,
    commands: Iterable[str],
) -> str:
    """Open a shell, enter config mode, run commands, return session log.

    No stub: simulated vendor is rejected (see ``collect_config`` for the
    same reasoning).
    """
    commands = list(commands)
    if vendor == Vendor.SIMULATED:
        raise ExecutorError(
            f"apply_config: vendor=SIMULATED is not supported — "
            f"address={address}:{port} has no real SSH transport. "
            f"Configure a real device before retrying."
        )
    opt = SSHOptions(
        username=credential.username,
        password=decrypt_secret(credential.secret_enc) if credential.secret_kind == "password" else None,
        ssh_key=decrypt_secret(credential.secret_enc) if credential.secret_kind == "ssh_key" else None,
        enable_password=decrypt_secret(credential.enable_password_enc) if credential.enable_password_enc else None,
        timeout=settings.ssh_timeout_seconds,
    )
    opt.address = address  # type: ignore[attr-defined]
    opt.port = port  # type: ignore[attr-defined]
    client = _make_ssh_client(opt)
    try:
        _, (enter, exit_cmd) = _build_collector(vendor)
        chan = client.invoke_shell(term="vt100", width=200, height=50)
        chan.settimeout(1.0)
        time.sleep(0.3)
        chan.recv(8192)  # clear banner
        log = io.BytesIO()
        log.write(f"{enter}".encode())
        chan.send(enter.encode())
        time.sleep(0.2)
        for cmd in commands:
            line = f"{cmd}\n"
            log.write(line.encode())
            chan.send(line.encode())
            time.sleep(0.1)
        log.write(f"{exit_cmd}\n".encode())
        chan.send(f"{exit_cmd}\n".encode())
        time.sleep(0.5)
        # Drain with paging support so that any ``display``-style command
        # in the apply payload (or the echoed config) is fully captured
        # even when it spans multiple terminal pages.
        more_markers = _paging_markers(vendor)
        end = time.time() + settings.ssh_read_timeout_seconds
        last = time.time()
        while time.time() < end:
            try:
                data = chan.recv(8192)
                if not data:
                    time.sleep(0.1)
                else:
                    log.write(data)
                    last = time.time()
                    if any(m in data for m in more_markers):
                        chan.send(b" ")
                        last = time.time()
            except socket.timeout:
                pass
            if time.time() - last > 1.2:
                break
        chan.close()
        return _strip_paging(log.getvalue().decode("utf-8", errors="replace"), vendor)
    finally:
        client.close()


def interactive_session(
    *,
    vendor: Vendor,
    address: str,
    port: int,
    credential: Credential,
    cols: int = 120,
    rows: int = 30,
) -> _ChannelShell | object:
    """Open an interactive shell and return a _ChannelShell.

    No stub: simulated vendor is rejected (see ``collect_config`` for the
    same reasoning — the previous _SimulatedShell path silently returned
    fake shell output, hiding transport failures from WebSSH users).
    """
    if vendor == Vendor.SIMULATED:
        raise ExecutorError(
            f"interactive_session: vendor=SIMULATED is not supported — "
            f"address={address}:{port} has no real SSH transport. "
            f"Configure a real device before opening a WebSSH session."
        )
    opt = SSHOptions(
        username=credential.username,
        password=decrypt_secret(credential.secret_enc) if credential.secret_kind == "password" else None,
        ssh_key=decrypt_secret(credential.secret_enc) if credential.secret_kind == "ssh_key" else None,
        timeout=settings.ssh_timeout_seconds,
    )
    opt.address = address  # type: ignore[attr-defined]
    opt.port = port  # type: ignore[attr-defined]
    client = _make_ssh_client(opt)
    chan = client.invoke_shell(term="xterm-256color", width=cols, height=rows)
    chan.settimeout(0.1)
    return _ChannelShell(client=client, channel=chan, recv_buf=io.BytesIO())


# ── v1.8: Custom command collection (migrated from PythonProject5) ──────────
#
# The original ``executor`` module only knew about two hard-coded
# commands (``display current-configuration`` and the matching
# ``system-view`` / ``configure terminal`` pair).  PythonProject5
# demonstrated that operators need a "send whatever the user types"
# path so they can run ``display cpu-usage``, ``display fan``,
# ``display health``, etc. without code changes per vendor.
#
# Design constraints honoured here:
#   * No shared mutable state — every call opens its own SSH session
#     so the existing ``batch_collect`` concurrency model is reused
#     without change.
#   * Reuse the existing paging logic (``_exec_collect`` /
#     ``_paging_markers`` / ``_strip_paging``) so a user-typed
#     ``display health`` honours the same ``---- More ----`` and
#     ``--More--`` handling the platform already implements for the
#     hard-coded collect path.
#   * Reject ``Vendor.SIMULATED`` for the same reason as
#     ``collect_config`` — we will not synthesise fake output.
#   * Command safety: a caller-supplied list is trusted (this
#     endpoint is gated by ``require_operator`` at the API layer);
#     however dangerous keywords (``reboot`` / ``reset`` /
#     ``format`` / ``delete saved-configuration`` etc.) are detected
#     here and surface as a per-command error in the result dict
#     rather than as a fatal exception, so a single bad command
#     doesn't poison the rest of the batch.

import re as _re_custom_cmd
_DANGEROUS_CUSTOM_PATTERNS: tuple[str, ...] = (
    r"\breboot\b",
    r"\breset\s+",  # 'reset saved-configuration' etc.
    r"\bformat\b",
    r"\bdelete\b",
    r"\bsave\b\s*(\n|$)",  # bare 'save' at end-of-line
    r"\bsystem-view\b",
    r"\bundo\b\s+",
    r"\berase\b",
    r"\bwrite\s+erase\b",
)
_DANGEROUS_RE = _re_custom_cmd.compile(
    "|".join(_DANGEROUS_CUSTOM_PATTERNS),
    _re_custom_cmd.IGNORECASE,
)


def _is_dangerous_command(cmd: str) -> bool:
    """Return True if ``cmd`` matches a known mutating pattern.

    This is a *defence in depth* check: the API layer (commands.py)
    already validates and the audit log records every run, but a
    straggler ``reboot`` would still take a device down. Refuse here
    so the operator sees a per-command error rather than a device
    that suddenly drops.
    """
    return bool(_DANGEROUS_RE.search(cmd))


def collect_custom_commands(
    *,
    vendor: Vendor,
    address: str,
    port: int,
    credential: Credential,
    commands: list[str],
    timeout_override: Optional[int] = None,
    per_command_timeout_sec: Optional[int] = None,
) -> dict[str, str]:
    """Execute a user-supplied list of read-style commands on a single device.

    Returns a dict ``{command: output}`` — empty string for skipped
    / dangerous commands, an ``__error_<idx>__`` key on SSH failure
    so the caller can tell the row apart from a real empty result.

    Differences from ``collect_config``:

    * ``commands`` is a list, not a single string. Each command is
      run on a single channel — no channel re-use for now because
      output ordering on the same channel is fragile across
      vendors when the user picks an arbitrary list.
    * Pagination is handled per command by ``_exec_collect`` so a
      ``display health`` that spans 3 terminal pages still
      returns the full body.
    * Dangerous commands are detected and reported, not executed.

    The function opens and closes its own SSH session so it is
    safe to call from a thread pool, mirroring the contract
    ``collect_health_via_executor`` honours for the health path.
    """
    if vendor == Vendor.SIMULATED:
        raise ExecutorError(
            f"collect_custom_commands: vendor=SIMULATED is not supported — "
            f"address={address}:{port} has no real SSH transport. "
            f"Configure a real device (huawei_vrp / cisco_ios_xe / ruijie_rgos) "
            f"or fix the device's endpoint/credential before retrying."
        )
    if not commands:
        return {}

    per_cmd_to = per_command_timeout_sec or settings.ssh_read_timeout_seconds
    opt = SSHOptions(
        username=credential.username,
        password=decrypt_secret(credential.secret_enc) if credential.secret_kind == "password" else None,
        ssh_key=decrypt_secret(credential.secret_enc) if credential.secret_kind == "ssh_key" else None,
        enable_password=decrypt_secret(credential.enable_password_enc) if credential.enable_password_enc else None,
        timeout=timeout_override if timeout_override is not None else settings.ssh_timeout_seconds,
    )
    opt.address = address  # type: ignore[attr-defined]
    opt.port = port  # type: ignore[attr-defined]
    client = _make_ssh_client(opt)
    results: dict[str, str] = {}
    try:
        for idx, cmd in enumerate(commands):
            cmd = (cmd or "").strip()
            if not cmd:
                continue
            if _is_dangerous_command(cmd):
                results[cmd] = f"__refused__: dangerous command rejected by server-side policy ({cmd})"
                logger.warning(
                    "collect_custom_commands: refused dangerous command '{}' on {}:{}",
                    cmd, address, port,
                )
                continue
            try:
                # Per-command timeout: shrink the per-call deadline so
                # a slow command can't take the whole budget when the
                # user pasted a list of 20 commands.  ``_exec_collect``
                # is bounded by ``settings.ssh_read_timeout_seconds``,
                # so we override by passing a wrapper if the caller
                # asked for a tighter budget.  We re-implement the
                # minimal loop here rather than threading yet another
                # parameter into the internal helper.
                if per_cmd_to and per_cmd_to < settings.ssh_read_timeout_seconds:
                    results[cmd] = _exec_collect_bounded(client, vendor, cmd, per_cmd_to)
                else:
                    results[cmd] = _exec_collect(client, vendor, cmd)
            except Exception as exc:  # noqa: BLE001
                results[cmd] = f"__error__: {exc!s}"[:2000]
                logger.warning(
                    "collect_custom_commands: command '{}' on {}:{} failed: {}",
                    cmd, address, port, exc,
                )
        return results
    finally:
        try:
            client.close()
        except Exception:  # noqa: BLE001
            pass


def _exec_collect_bounded(client, vendor: Vendor, cmd: str, timeout_sec: float) -> str:
    """Like ``_exec_collect`` but with a caller-supplied deadline.

    Used by ``collect_custom_commands`` when the API asks for a
    per-command budget tighter than the global
    ``settings.ssh_read_timeout_seconds``.  Behaviour is otherwise
    identical (same paging, same strip).
    """
    chan = client.invoke_shell(term="vt100", width=200, height=50)
    chan.settimeout(2.0)
    captured = io.BytesIO()
    chan.send(cmd.encode() + b"\n")
    end = time.time() + timeout_sec
    last = time.time()
    more_markers = _paging_markers(vendor)
    while time.time() < end:
        try:
            data = chan.recv(8192)
            if not data:
                time.sleep(0.1)
            else:
                captured.write(data)
                last = time.time()
                if any(m in data for m in more_markers):
                    chan.send(b" ")
                    last = time.time()
        except socket.timeout:
            pass
        if time.time() - last > 1.2:
            break
    chan.close()
    return _strip_paging(captured.getvalue().decode("utf-8", errors="replace"), vendor)
