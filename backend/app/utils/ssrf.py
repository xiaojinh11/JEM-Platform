"""SSRF protection utilities.

This module provides URL validation to prevent Server-Side Request Forgery attacks.
Configured allowlists and denylists restrict which hosts/IPs can be accessed.
"""
from __future__ import annotations

import ipaddress
import socket
import urllib.parse
from typing import Optional
from urllib.parse import urlparse

from app.core.config import settings
from app.core.logging import logger

# Blocked schemes - only HTTP/HTTPS allowed
ALLOWED_SCHEMES = frozenset({"http", "https"})

# Blocked private/reserved IP ranges
_BLOCKED_IP_RANGES: list[ipaddress.IPv4Network | ipaddress.IPv6Network] = [
    ipaddress.ip_network("10.0.0.0/8"),
    ipaddress.ip_network("172.16.0.0/12"),
    ipaddress.ip_network("192.168.0.0/16"),
    ipaddress.ip_network("127.0.0.0/8"),
    ipaddress.ip_network("169.254.0.0/16"),  # Link-local
    ipaddress.ip_network("0.0.0.0/8"),
    ipaddress.ip_network("100.64.0.0/10"),  # Carrier-grade NAT
    ipaddress.ip_network("192.0.0.0/24"),
    ipaddress.ip_network("192.0.2.0/24"),  # Documentation
    ipaddress.ip_network("198.51.100.0/24"),  # Documentation
    ipaddress.ip_network("203.0.113.0/24"),  # Documentation
    ipaddress.ip_network("255.255.255.255/32"),
    ipaddress.ip_network("::1/128"),  # Loopback
    ipaddress.ip_network("fc00::/7"),  # Unique local
    ipaddress.ip_network("fe80::/10"),  # Link-local
    ipaddress.ip_network("ff00::/8"),  # Multicast
]


class SSRFProtectionError(Exception):
    """Raised when a URL fails SSRF validation."""

    pass


def _parse_url(url: str) -> urllib.parse.ParseResult:
    """Safely parse a URL."""
    try:
        return urlparse(url)
    except Exception as exc:
        raise SSRFProtectionError(f"Invalid URL: {exc}") from exc


def _is_ip_blocked(ip_str: str) -> bool:
    """Check if an IP address is in a blocked range."""
    try:
        ip = ipaddress.ip_address(ip_str)
        for blocked in _BLOCKED_IP_RANGES:
            if ip in blocked:
                return True
        return False
    except ValueError:
        return True  # Invalid IPs are blocked


def _resolve_and_check(hostname: str, timeout: float = 2.0) -> bool:
    """Resolve hostname and check all resulting IPs.

    Returns True if ALL resolved IPs are safe.
    """
    try:
        # Check if hostname itself is an IP (shouldn't be allowed)
        if _is_ip_blocked(hostname):
            return False

        # Try to resolve
        try:
            addr_info = socket.getaddrinfo(hostname, None, socket.AF_UNSPEC, socket.SOCK_STREAM)
        except socket.gaierror:
            logger.warning("SSRF: could not resolve hostname %r", hostname)
            # Fail closed - if we can't resolve, block it
            return False

        resolved_ips = set()
        for family, _, _, _, sockaddr in addr_info:
            if sockaddr[0]:
                resolved_ips.add(sockaddr[0])

        for ip_str in resolved_ips:
            if _is_ip_blocked(ip_str):
                logger.warning("SSRF: blocked %r resolved to blocked IP %r", hostname, ip_str)
                return False

        return True

    except Exception as exc:
        logger.warning("SSRF: error resolving %r: %s", hostname, exc)
        return False


def validate_url(url: str, timeout: float = 2.0) -> str:
    """Validate a URL for SSRF vulnerabilities.

    Args:
        url: The URL to validate
        timeout: DNS resolution timeout in seconds

    Returns:
        The validated URL (may be modified, e.g., trailing slash normalized)

    Raises:
        SSRFProtectionError: If the URL is blocked

    Allowed:
        - HTTP and HTTPS URLs
        - Publicly routable IPs only (no private, loopback, link-local)
        - Resolvable hostnames that resolve to safe IPs
        - Port 80 (HTTP) and 443 (HTTPS) only, unless ALLOWED_EXTRA_PORTS is set

    Blocked:
        - File://, ftp://, gopher:// and other dangerous schemes
        - Localhost, 127.0.0.1
        - Private IP ranges (10.x, 172.16-31.x, 192.168.x)
        - Link-local addresses
        - IPv6 loopback/local addresses
        - Hostnames that resolve to blocked IPs
    """
    if not url:
        raise SSRFProtectionError("URL cannot be empty")

    parsed = _parse_url(url)

    # Check scheme
    scheme = parsed.scheme.lower()
    if scheme not in ALLOWED_SCHEMES:
        raise SSRFProtectionError(
            f"URL scheme '{scheme}' is not allowed. Only HTTP and HTTPS are permitted."
        )

    # Get hostname
    hostname = parsed.hostname
    if not hostname:
        raise SSRFProtectionError("URL must have a valid hostname")

    # Check port
    port = parsed.port
    allowed_ports = getattr(settings, "allowed_extra_ports", set())
    if port is not None and port not in {80, 443} and port not in allowed_ports:
        raise SSRFProtectionError(
            f"Port {port} is not allowed. Only ports 80, 443, and {sorted(allowed_ports)} are permitted."
        )

    # Check if hostname itself is a blocked IP
    if _is_ip_blocked(hostname):
        raise SSRFProtectionError(f"Hostname '{hostname}' resolves to a blocked IP address")

    # Check for credentials in URL (credential leakage)
    if parsed.username or parsed.password:
        raise SSRFProtectionError("URLs with embedded credentials are not permitted")

    # Resolve and verify all IPs are safe
    if not _resolve_and_check(hostname, timeout):
        raise SSRFProtectionError(
            f"Hostname '{hostname}' resolves to a blocked or private IP address"
        )

    # Normalize URL (remove default ports, trailing slash)
    netloc = hostname
    if port:
        netloc = f"{hostname}:{port}"
    if parsed.username:
        netloc = f"{parsed.username}@{netloc}"

    normalized = urllib.parse.urlunparse((
        scheme,
        netloc,
        parsed.path or "/",
        parsed.params,
        parsed.query,
        "",  # No fragment
    ))

    return normalized


def safe_http_get(url: str, timeout: float = 10.0, max_response_size: int = 1024 * 1024) -> dict:
    """Perform an HTTP GET with SSRF protection.

    Args:
        url: The URL to fetch
        timeout: Request timeout in seconds
        max_response_size: Maximum response size in bytes

    Returns:
        dict with keys: status_code, headers, body, truncated

    Raises:
        SSRFProtectionError: If URL fails SSRF validation
        Exception: Other HTTP errors
    """
    import httpx

    validated_url = validate_url(url)

    with httpx.Client(timeout=timeout, follow_redirects=False) as client:
        resp = client.get(validated_url, timeout=timeout)

    body = resp.text
    truncated = len(body) > max_response_size
    if truncated:
        body = body[:max_response_size]

    return {
        "url": validated_url,
        "status_code": resp.status_code,
        "headers": dict(resp.headers),
        "body": body,
        "truncated": truncated,
        "content_type": resp.headers.get("content-type", ""),
    }
