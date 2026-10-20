"""In-process sliding-window rate limiter for sensitive endpoints.

Designed for the login route, where a global per-IP throttle is more
useful than the per-account lockout already in ``services.account``.

Why not slowapi / Redis-backed limiter?  We don't want a hard
dependency on Redis just for this — the auth login flow is
low-volume and a single uvicorn worker is enough for most
deployments.  The limit is per-worker, so horizontally scaled
workers multiply the effective rate (acceptable for a defense-in-depth
throttle, not as the *only* defense).

Algorithm: per-IP sliding window of last N seconds.  Each request
pushes a timestamp; we count how many fall inside the window and
block when over the threshold.  Old entries are pruned lazily on
each check so memory stays bounded.

The state lives in a module-level dict guarded by ``_lock`` — a single
process-wide mutex is fine here because the lock is only held during
the small list mutation, and login is not on the hot path.
"""
from __future__ import annotations

import threading
import time
from collections import defaultdict, deque
from typing import Deque, Dict


class IPRateLimiter:
    """Sliding-window IP limiter.  Thread-safe; per-process."""

    def __init__(self, max_requests: int, window_seconds: float) -> None:
        self.max_requests = max_requests
        self.window = float(window_seconds)
        self._lock = threading.Lock()
        self._hits: Dict[str, Deque[float]] = defaultdict(deque)

    def check(self, key: str) -> tuple[bool, int]:
        """Return ``(allowed, retry_after_seconds)``.

        ``retry_after_seconds`` is the time until the oldest in-window
        hit expires (0 when the request is allowed).
        """
        now = time.monotonic()
        cutoff = now - self.window
        with self._lock:
            bucket = self._hits[key]
            # Prune expired entries from the left.
            while bucket and bucket[0] <= cutoff:
                bucket.popleft()
            if len(bucket) >= self.max_requests:
                retry = max(1, int(self.window - (now - bucket[0])))
                return False, retry
            bucket.append(now)
            return True, 0

    def reset(self, key: str | None = None) -> None:
        """For tests: clear one key (or all)."""
        with self._lock:
            if key is None:
                self._hits.clear()
            else:
                self._hits.pop(key, None)


# Default: 10 failed attempts per IP per minute.  This is generous
# enough to not bother humans behind a shared NAT, but tight enough
# to slow down a single-source dictionary attack.  Per-account
# lockout (services.account.is_locked_out) is the primary defense;
# this is the safety net.
login_ip_limiter = IPRateLimiter(max_requests=10, window_seconds=60.0)

# Per-user throttle for batch job creation. Prevents a single
# authenticated user from queuing dozens of full-fleet (500-device)
# jobs in rapid succession — each of those burns a worker thread for
# ~30 minutes, so 3 batch jobs per user per 10 minutes is a sane
# ceiling that still lets ops legitimately re-run a failed job.
#
# NOTE: process-local (same limitation as login_ip_limiter) — fine
# for a defense-in-depth cap; absolute enforcement belongs in Celery
# admission control / queue depth alerts at the infra layer.
batch_job_user_limiter = IPRateLimiter(max_requests=3, window_seconds=600.0)
