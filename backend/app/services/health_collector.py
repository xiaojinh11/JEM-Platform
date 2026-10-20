"""Lightweight per-device health snapshot collector.

Background
----------
This module captures the *health* of a device — CPU, memory, temperature,
fan, power — by issuing a small set of read-only display/show commands
on the same SSH channel. It deliberately does **not** pull the full
running-configuration; that path is owned by ``app.services.executor``
and the ``batch_collect`` task.

The implementation is inspired by the H3C automation training module
``V08S03C306 — 自动化采集`` (in ``docs/references/python-ops-collection/``),
which demonstrates how to scrape CPU / memory / temperature / fan /
power / disk with ``re.findall``. Differences from the reference:

* We send each command on an **existing** SSH session rather than
  forking a new connection per device — saves ~1.5s handshake per
  device on a 200-device fleet.
* Vendor dispatch is data-driven (``HEALTH_COMMANDS``) so adding a new
  vendor is a one-line change instead of a new ``if``.
* Each metric is parsed with a single, focused regex; failures are
  returned as ``None`` rather than raising, so a partial snapshot
  (e.g. "fan command not supported on this firmware") still gets
  persisted.

Public surface
--------------
* :func:`collect_health` — synchronously fetch health for a single
  device. Caller decides the SSH session.
* :func:`collect_health_via_executor` — convenience that opens a fresh
  SSH channel via the existing ``executor`` module (used by
  ``batch_health`` Celery task).
* :class:`HealthSnapshot` — typed result.

The module is import-clean: it only depends on ``app.core``,
``app.models.credential``, ``app.core.enums`` and ``paramiko`` (which is
already a project dependency).
"""
from __future__ import annotations

import io
import re
import socket
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Optional

from app.core.config import settings
from app.core.enums import Vendor
from app.core.logging import logger
from app.core.security import decrypt_secret
from app.models.credential import Credential

try:  # optional import for CI environments without paramiko
    import paramiko as _paramiko  # type: ignore
    _HAVE_PARAMIKO = True
except Exception:  # noqa: BLE001
    _HAVE_PARAMIKO = False
    _paramiko = None  # type: ignore


# ── Per-vendor command sets ────────────────────────────────────────────────
#
# Each entry: (cpu_cmd, mem_cmd, temp_cmd, fan_cmd, power_cmd).
# A command that the vendor doesn't support on a given firmware should
# return an empty string here — the parser will return ``None`` for that
# metric instead of failing the whole snapshot.
#
# These names are written against Huawei VRP V8 (``display ...``),
# Cisco IOS-XE 16+ (``show ...``) and Ruijie RGOS 11+ (``show ...``).
# If you target an older firmware, change the command in the dict
# rather than hard-coding branches later.

_HEALTH_COMMANDS: dict[Vendor, dict[str, str]] = {
    Vendor.HUAWEI_VRP: {
        "cpu": "display cpu-usage",
        "mem": "display memory-usage",
        "temp": "display temperature all",
        "fan": "display fan",
        "power": "display power",
        # Optical: RX/TX power per port.  ``verbose`` ensures we get
        # a column for both values plus the alarm threshold so we
        # can tell "low RX power" from "port just lost the DOM".
        "optical": "display transceiver diagnosis interface",
        # Config fingerprint: capture first N lines of the running
        # config.  We don't store the full body (it could be tens of
        # thousands of lines) — just a hash + line count + a
        # short excerpt.  See HealthSnapshot.config_fingerprint /
        # config_excerpt for the contract.
        "config": "display current-configuration | head 100",
    },
    Vendor.CISCO_IOS_XE: {
        "cpu": "show processes cpu",
        "mem": "show memory statistics",
        "temp": "show env temperature",
        "fan": "show env fan",
        "power": "show env power",
        "optical": "show interfaces transceiver detail",
        "config": "show running-config | head 100",
    },
    Vendor.RUIJIE_RGOS: {
        "cpu": "show cpu",
        "mem": "show memory",
        "temp": "show environment",
        "fan": "show fan",
        "power": "show power",
        "optical": "show transceiver detail",
        "config": "show running-config | head 100",
    },
}

# Per-run metric filter: which metrics to actually issue commands for.
# We default to ALL so the legacy contract (collect everything) holds,
# but scheduled / targeted runs can pass a subset. The keys are the
# same names used in ``_HEALTH_COMMANDS``; the value is the
# command to send. An empty dict means "collect all known metrics
# for this vendor".
DEFAULT_METRICS = frozenset({"cpu", "mem", "temp", "fan", "power", "optical", "config"})


# ── Parsers ────────────────────────────────────────────────────────────────
#
# Each parser takes the raw command output and returns either a value
# (``float`` for numeric, ``str`` for status) or ``None`` if the metric
# isn't in the output. We intentionally tolerate varying whitespace,
# Unicode dashes and the occasional Chinese annotation.

def _to_float(s: Optional[str]) -> Optional[float]:
    if s is None:
        return None
    try:
        return float(s.replace(",", ""))
    except (TypeError, ValueError):
        return None


def _parse_cpu(vendor: Vendor, text: str) -> Optional[float]:
    """Return CPU usage as a percent (0–100)."""
    if not text:
        return None
    # Huawei VRP: "CPU utilization for five seconds: 5%; one minute: 7%; five minutes: 6%"
    m = re.search(
        r"(?:five\s+seconds|5\s*seconds?)\s*[:：]\s*(\d+(?:\.\d+)?)\s*%",
        text, flags=re.IGNORECASE,
    )
    if m:
        return _to_float(m.group(1))
    # Cisco IOS-XE: "CPU utilization for 5 seconds = 3%; 1 minute = 4%; 5 minutes = 5%"
    m = re.search(
        r"CPU\s+utilization\s+for\s+5\s+seconds?\s*=\s*(\d+(?:\.\d+)?)\s*%",
        text, flags=re.IGNORECASE,
    )
    if m:
        return _to_float(m.group(1))
    # Ruijie: "CPU utilization for five seconds: 5%; ..."
    m = re.search(r"CPU.*?(\d+(?:\.\d+)?)\s*%", text, flags=re.IGNORECASE)
    if m:
        return _to_float(m.group(1))
    return None


def _parse_mem(vendor: Vendor, text: str) -> Optional[float]:
    """Return memory utilization as a percent (0–100)."""
    if not text:
        return None
    # Huawei VRP V8: "Memory Using Rate Is: 35%"  (newer firmware)
    m = re.search(r"[Mm]emory\s+[Uu]sing\s+[Rr]ate\s+[Ii]s\s*[:：]?\s*(\d+(?:\.\d+)?)\s*%", text)
    if m:
        return _to_float(m.group(1))
    # Huawei VRP V8: "Used Rate: 35%" — older / alt rendering.
    m = re.search(r"[Uu]sed\s*[Rr]ate\s*[:：]?\s*(\d+(?:\.\d+)?)\s*%", text)
    if m:
        return _to_float(m.group(1))
    # Cisco: "Processor Pool Total: ... Used: ...  Free: ..."
    # Fallback: any "... X% used" or "Memory used ... X%"
    m = re.search(r"[Mm]emory.*?(\d+(?:\.\d+)?)\s*%\s*used", text, flags=re.IGNORECASE)
    if m:
        return _to_float(m.group(1))
    # Ruijie: "Used Memory: ...  Used Rate: 32%"
    m = re.search(r"Used\s+Rate\s*[:：]?\s*(\d+(?:\.\d+)?)\s*%", text)
    if m:
        return _to_float(m.group(1))
    return None


def _parse_temp(vendor: Vendor, text: str) -> Optional[float]:
    """Return current temperature in Celsius (single value, hottest slot)."""
    if not text:
        return None
    # Huawei: "Slot 0  Sensor 0 :  32 (C)  [26 / 60]" — pick the max so we
    # can alert if any sensor breaches the upper limit.
    nums = [float(n) for n in re.findall(r"(\d+)\s*\(C\)", text)]
    if nums:
        return max(nums)
    # Cisco: "TEMPERATURE VALUE: 28 Celsius  STATE: GREEN"
    m = re.search(r"(\d+)\s*Celsius", text, flags=re.IGNORECASE)
    if m:
        return _to_float(m.group(1))
    # Ruijie: similar to Huawei "Temperature: 35 C"
    m = re.search(r"[Tt]emperature\s*[:：]?\s*(\d+(?:\.\d+)?)", text)
    if m:
        return _to_float(m.group(1))
    return None


def _parse_fan(vendor: Vendor, text: str) -> Optional[str]:
    """Return a coarse fan status string.

    We don't try to model N fan slots — we return a single
    "normal" / "abnormal" / "absent" verdict so the UI can flag
    anomalies without us inventing a schema.
    """
    if not text:
        return None
    low = text.lower()
    if "abnormal" in low or "fail" in low or "fault" in low:
        return "abnormal"
    if "absent" in low or "not installed" in low:
        return "absent"
    if "normal" in low or "ok" in low or "green" in low or "auto" in low:
        return "normal"
    return "unknown"


def _parse_power(vendor: Vendor, text: str) -> Optional[float]:
    """Return current power draw in Watts. ``None`` if not parseable."""
    if not text:
        return None
    # Huawei: "Current Power: 32.5W   Rated Power: 450.0W"
    m = re.search(r"[Cc]urrent\s+[Pp]ower\s*[:：]?\s*(\d+(?:\.\d+)?)\s*W", text)
    if m:
        return _to_float(m.group(1))
    # Cisco: "PS1  is  AC  130W" or "Power ...:  130 W"
    m = re.search(r"(\d+(?:\.\d+)?)\s*W(?!\w)", text)
    if m:
        return _to_float(m.group(1))
    return None


# ── Optical (RX/TX dBm per port) ─────────────────────────────────────
#
# Vendor output formats vary widely; we normalise to a list of
# ``{"port": "GigabitEthernet0/0/1", "rx_dbm": -7.23, "tx_dbm": -5.10}``
# dicts.  Anything we can't parse is silently dropped — a "no DDM
# support" module is not an error, just a None row.
#
# Why not return a flat dict {port: (rx, tx)}? Because some modules
# report only RX, some only TX, and some include alarm thresholds
# inline.  Per-port dicts let the caller see which values are real.

_OPTICAL_LINE_RE = re.compile(
    # Huawei: "GigabitEthernet0/0/1  -7.23  -5.10  -7.00  -1.00  normal"
    # Cisco:  "Gi0/1  rx_power (-7.23 dBm)  tx_power (-5.10 dBm)"
    # Ruijie: "Te0/1  RX:-7.23  TX:-5.10  ..."
    r"""
    (?P<port>
        (?:Gigabit|TenGig|Fast)?Ethernet[\d/:\.]+
        | \w+/\d+/\d+                # Cisco short form (Gi0/1)
    )
    .*?
    (?P<rx>-?\d+\.\d+|-?\d+)
    \s*(?:dBm)?
    \s+
    (?P<tx>-?\d+\.\d+|-?\d+)
    """,
    re.VERBOSE,
)


def _parse_optical(vendor: Vendor, text: str) -> list[dict[str, Optional[float]]]:
    """Return per-port RX/TX dBm readings.

    Returns an empty list if the device doesn't support DDM, the
    command is missing, or the format is too far from any known
    shape.  We do NOT raise — see module docstring's "partial
    snapshot" guarantee.
    """
    if not text:
        return []
    rows: list[dict[str, Optional[float]]] = []
    for m in _OPTICAL_LINE_RE.finditer(text):
        rows.append({
            "port": m.group("port").strip(),
            "rx_dbm": _to_float(m.group("rx")),
            "tx_dbm": _to_float(m.group("tx")),
        })
    return rows


# ── Config fingerprint ─────────────────────────────────────────────
#
# We don't store the full config (10k+ lines × N snapshots = GB of
# dead weight).  We DO capture:
#   - sha256 of the captured excerpt (so a "did the config change?"
#     question has a cheap answer)
#   - line count of the excerpt
#   - the first 100 lines verbatim (so an operator can sanity-check
#     which hostname / banner / vrf is in effect)
#
# Callers that need the full body can fall back to ``collect_config`` /
# the configs/revisions table — that's the canonical store.

def _parse_config_excerpt(vendor: Vendor, text: str) -> dict[str, object]:
    """Return ``{"fingerprint": "sha256…", "lines": 87, "excerpt": "..."}``."""
    import hashlib
    excerpt = (text or "").rstrip("\r\n")
    lines = excerpt.count("\n") + 1 if excerpt else 0
    fingerprint = hashlib.sha256(excerpt.encode("utf-8")).hexdigest() if excerpt else ""
    return {
        "fingerprint": fingerprint,
        "lines": lines,
        "excerpt": excerpt[:2000],  # cap to 2KB to bound DB growth
    }


# ── Optical (RX/TX dBm per port) ─────────────────────────────────────
#
# Vendor output formats vary widely; we normalise to a list of
# ``{"port": "GigabitEthernet0/0/1", "rx_dbm": -7.23, "tx_dbm": -5.10}``
# dicts.  Anything we can't parse is silently dropped — a "no DDM
# support" module is not an error, just a None row.
#
# Why not return a flat dict {port: (rx, tx)}? Because some modules
# report only RX, some only TX, and some include alarm thresholds
# inline.  Per-port dicts let the caller see which values are real.

_OPTICAL_LINE_RE = re.compile(
    # Huawei: "GigabitEthernet0/0/1  -7.23  -5.10  -7.00  -1.00  normal"
    # Cisco:  "Gi0/1  rx_power (-7.23 dBm)  tx_power (-5.10 dBm)"
    # Ruijie: "Te0/1  RX:-7.23  TX:-5.10  ..."
    r"""
    (?P<port>
        (?:Gigabit|TenGig|Fast)?Ethernet[\d/:\.]+
        | \w+/\d+/\d+                # Cisco short form (Gi0/1)
    )
    .*?
    (?P<rx>-?\d+\.\d+|-?\d+)
    \s*(?:dBm)?
    \s+
    (?P<tx>-?\d+\.\d+|-?\d+)
    """,
    re.VERBOSE,
)


def _parse_optical(vendor: Vendor, text: str) -> list[dict[str, Optional[float]]]:
    """Return per-port RX/TX dBm readings.

    Returns an empty list if the device doesn't support DDM, the
    command is missing, or the format is too far from any known
    shape.  We do NOT raise — see module docstring's "partial
    snapshot" guarantee.
    """
    if not text:
        return []
    rows: list[dict[str, Optional[float]]] = []
    for m in _OPTICAL_LINE_RE.finditer(text):
        rows.append({
            "port": m.group("port").strip(),
            "rx_dbm": _to_float(m.group("rx")),
            "tx_dbm": _to_float(m.group("tx")),
        })
    return rows


# ── Config fingerprint ─────────────────────────────────────────────
#
# We don't store the full config (10k+ lines × N snapshots = GB of
# dead weight).  We DO capture:
#   - sha256 of the captured excerpt (so a "did the config change?"
#     question has a cheap answer)
#   - line count of the excerpt
#   - the first 100 lines verbatim (so an operator can sanity-check
#     which hostname / banner / vrf is in effect)
#
# Callers that need the full body can fall back to ``collect_config`` /
# the configs/revisions table — that's the canonical store.

def _parse_config_excerpt(vendor: Vendor, text: str) -> dict[str, object]:
    """Return ``{"fingerprint": "sha256…", "lines": 87, "excerpt": "..."}``."""
    import hashlib
    excerpt = (text or "").rstrip("\r\n")
    lines = excerpt.count("\n") + 1 if excerpt else 0
    fingerprint = hashlib.sha256(excerpt.encode("utf-8")).hexdigest() if excerpt else ""
    return {
        "fingerprint": fingerprint,
        "lines": lines,
        "excerpt": excerpt[:2000],  # cap to 2KB to bound DB growth
    }


@dataclass
class HealthSnapshot:
    """Structured health reading for one device at one point in time.

    New fields (v1.7, scheduled-collection feature):

    * ``optical`` — list of per-port RX/TX dBm readings.  Empty list
      if the device doesn't support DDM or the metric wasn't in
      this run's filter.  Each entry is a small dict (see
      :func:`_parse_optical` for the exact shape).
    * ``config_fingerprint`` — sha256 of the captured config
      excerpt, "" if not collected this run.  Pair with
      ``config_excerpt`` and ``config_lines`` for a lightweight
      "did the config change since last run?" comparison.
    * ``config_lines`` — line count of the excerpt (capped at the
      collector's head-100 limit; the value is informational, not
      the full running-config length).
    * ``config_excerpt`` — verbatim first ~100 lines of running
      config, capped at 2KB.  Intentionally lossy: an operator
      who needs the full config uses the configs/revisions table.
    * ``metrics_run`` — list of metric names actually executed in
      this run.  Useful for the UI to know "this row doesn't
      include optical because the schedule didn't ask for it".
    """

    device_id: int
    collected_at: str  # ISO-8601 UTC
    vendor: str
    cpu_pct: Optional[float] = None
    mem_pct: Optional[float] = None
    temp_c: Optional[float] = None
    fan_state: Optional[str] = None
    power_w: Optional[float] = None
    raw_excerpt: str = ""  # first 1KB for audit
    errors: list[str] = field(default_factory=list)
    optical: list[dict[str, Optional[float]]] = field(default_factory=list)
    config_fingerprint: str = ""
    config_lines: int = 0
    config_excerpt: str = ""
    metrics_run: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)


# ── Helpers ────────────────────────────────────────────────────────────────

_ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]|\x1b\([AB]")


def _strip_paging(text: str) -> str:
    """Strip terminal control sequences and 'More' pagination prompts.

    This is intentionally less aggressive than ``executor._strip_paging``;
    health commands output less data, but we still want clean text.
    """
    text = _ANSI_RE.sub("", text)
    text = re.sub(r"----\s*More\s*----.*?\r", "", text)
    text = re.sub(r"--More--.*?\r", "", text)
    return text.strip("\r\n")


def _run_one_command(
    channel,
    command: str,
    timeout_sec: float,
) -> str:
    """Send ``command`` and capture the buffered response.

    ``channel`` is a paramiko ``Channel`` (already in shell mode). We
    send the command, then poll the channel for up to ``timeout_sec``
    seconds, returning everything the device printed.

    Stops early if the channel goes quiet for >1.2s — that indicates
    the command has finished and we're now seeing either a prompt or
    silence.
    """
    channel.send(command.encode("utf-8") + b"\n")
    captured = io.BytesIO()
    end = time.time() + timeout_sec
    last = time.time()
    while time.time() < end:
        try:
            data = channel.recv(8192)
            if not data:
                time.sleep(0.05)
            else:
                captured.write(data)
                last = time.time()
        except socket.timeout:
            pass
        if time.time() - last > 1.2:
            break
    return _strip_paging(captured.getvalue().decode("utf-8", errors="replace"))


# ── Public API ─────────────────────────────────────────────────────────────


def collect_health(
    *,
    vendor: Vendor,
    channel,
    device_id: int,
    command_timeout_sec: Optional[int] = None,
    metrics: Optional[set[str]] = None,
) -> HealthSnapshot:
    """Run the health command set on an open paramiko ``channel``.

    The caller owns the channel lifecycle (connect/close); this
    function never opens a new SSH session. The same channel can be
    reused for other read-only commands afterwards.

    ``metrics`` is an optional set that limits which commands get
    sent.  Recognised values mirror the keys in
    :data:`_HEALTH_COMMANDS`: ``cpu``, ``mem``, ``temp``, ``fan``,
    ``power``, ``optical``, ``config``.  A metric that's requested
    but not available on the vendor's command set is silently
    skipped (it just won't be parsed and the column will stay
    ``None``).  ``None`` or an empty set means "all metrics".

    Commands are sent sequentially — they are all read-only and a single
    vendor won't refuse to interleave, but doing them serially makes
    log correlation trivial.
    """
    timeout = command_timeout_sec or settings.collect_command_timeout_sec
    snap = HealthSnapshot(
        device_id=device_id,
        collected_at=datetime.now(timezone.utc).isoformat(),
        vendor=vendor.value,
    )

    # Resolve the effective command set: intersection of (request
    # filter) and (vendor has a command for that metric).  We don't
    # raise on a bad metric name — caller's filter is trusted and
    # unrecognised values are a no-op.
    cmds_all = _HEALTH_COMMANDS.get(vendor, {})
    if not cmds_all:
        snap.errors.append(f"no health command set for vendor {vendor}")
        return snap
    if metrics:
        cmds = {k: v for k, v in cmds_all.items() if k in metrics}
    else:
        cmds = cmds_all
    snap.metrics_run = list(cmds.keys())

    raw_chunks: list[str] = []
    for metric, cmd in cmds.items():
        try:
            text = _run_one_command(channel, cmd, timeout)
        except Exception as exc:  # noqa: BLE001
            snap.errors.append(f"{metric}: {exc!s}"[:200])
            continue
        raw_chunks.append(f"### {metric}: {cmd}\n{text}\n")
        if metric == "cpu":
            snap.cpu_pct = _parse_cpu(vendor, text)
        elif metric == "mem":
            snap.mem_pct = _parse_mem(vendor, text)
        elif metric == "temp":
            snap.temp_c = _parse_temp(vendor, text)
        elif metric == "fan":
            snap.fan_state = _parse_fan(vendor, text)
        elif metric == "power":
            snap.power_w = _parse_power(vendor, text)
        elif metric == "optical":
            snap.optical = _parse_optical(vendor, text)
        elif metric == "config":
            cfg = _parse_config_excerpt(vendor, text)
            snap.config_fingerprint = str(cfg["fingerprint"])
            snap.config_lines = int(cfg["lines"])  # type: ignore[arg-type]
            snap.config_excerpt = str(cfg["excerpt"])

    snap.raw_excerpt = "\n".join(raw_chunks)[:1024]
    return snap


def collect_health_via_executor(
    *,
    vendor: Vendor,
    address: str,
    port: int,
    credential: Credential,
    device_id: int,
) -> HealthSnapshot:
    """Open a fresh SSH session, snapshot, then close.

    Mirrors the contract of :func:`app.services.executor.collect_config`
    so the Celery worker code stays consistent. For SIMULATED vendor we
    synthesize a plausible snapshot so the rest of the pipeline (DB row,
    UI chart) can be exercised without real devices.
    """
    if vendor == Vendor.SIMULATED:
        return HealthSnapshot(
            device_id=device_id,
            collected_at=datetime.now(timezone.utc).isoformat(),
            vendor=vendor.value,
            cpu_pct=12.0,
            mem_pct=34.0,
            temp_c=38.0,
            fan_state="normal",
            power_w=45.0,
            raw_excerpt="(simulated device — no SSH)",
        )

    if not _HAVE_PARAMIKO:
        return HealthSnapshot(
            device_id=device_id,
            collected_at=datetime.now(timezone.utc).isoformat(),
            vendor=vendor.value,
            errors=["paramiko not installed"],
        )

    # We deliberately re-implement the minimal SSH setup here instead of
    # calling ``executor.collect_config``: that function returns the
    # output of one command and closes the channel, which forces a
    # reconnect per metric (5× the handshakes).
    from app.services.executor import _make_ssh_client, SSHOptions  # local import to avoid cycle

    secret = decrypt_secret(credential.secret_enc) if credential.secret_enc else None
    enable = decrypt_secret(credential.enable_password_enc) if credential.enable_password_enc else None
    opt = SSHOptions(
        username=credential.username,
        password=secret if credential.secret_kind == "password" else None,
        ssh_key=secret if credential.secret_kind == "ssh_key" else None,
        enable_password=enable,
        timeout=settings.collect_per_device_timeout_sec,
    )
    opt.address = address  # type: ignore[attr-defined]
    opt.port = port  # type: ignore[attr-defined]
    client = _make_ssh_client(opt)
    try:
        chan = client.invoke_shell(term="vt100", width=200, height=50)
        chan.settimeout(2.0)
        # Drain banner so the first ``recv`` after our command isn't
        # polluted with login motd.
        try:
            time.sleep(0.3)
            chan.recv(8192)
        except socket.timeout:
            pass
        return collect_health(
            vendor=vendor,
            channel=chan,
            device_id=device_id,
        )
    finally:
        try:
            client.close()
        except Exception:  # noqa: BLE001
            pass
