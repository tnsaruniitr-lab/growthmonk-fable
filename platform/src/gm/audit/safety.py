"""SSRF guards and request-safety helpers for the audit pipeline.

Faithful port of aeo-seo-auditor-fable ``service/safety.py``. The auditor
fetches whatever URL a caller submits. Without a guard, a caller can point it
at cloud-metadata endpoints (169.254.169.254), localhost, or RFC1918 hosts and
read internal responses back through the audit output. This module rejects
such targets BEFORE any fetch is dispatched, and the fetch layer re-checks
every redirect hop.

Public error type: ``UnsafeURL`` (the source returned (ok, reason) tuples via
``check_url_safe``, which is kept; ``validate_url`` is the raising wrapper the
Phase B contract specifies).

Stdlib only. DNS resolution goes through the module-level ``_getaddrinfo``
function so tests can monkeypatch it and never hit live DNS.
"""

from __future__ import annotations

import ipaddress
import socket
from urllib.parse import urlparse


class UnsafeURL(ValueError):
    """Raised when a URL must not be fetched server-side (SSRF guard)."""


# Hostnames that must never be fetched regardless of DNS resolution.
_BLOCKED_HOSTNAMES = frozenset({
    "localhost",
    "metadata.google.internal",  # GCP metadata
    "metadata",
})

# Cloud metadata IPs (link-local already blocks 169.254/16, but be explicit).
_BLOCKED_IPS = frozenset({
    "169.254.169.254",  # AWS/GCP/Azure/DO/etc. IMDS
    "100.100.100.200",  # Alibaba metadata
    "fd00:ec2::254",  # AWS IMDSv6
})


def _getaddrinfo(host: str, port: int) -> list[tuple]:
    """Resolver indirection — monkeypatch this in tests to avoid live DNS."""
    return socket.getaddrinfo(host, port, proto=socket.IPPROTO_TCP)


def _ip_is_disallowed(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    """True for any address that must not be the target of a server-side fetch."""
    return (
        ip.is_private
        or ip.is_loopback
        or ip.is_link_local
        or ip.is_multicast
        or ip.is_reserved
        or ip.is_unspecified
        or str(ip) in _BLOCKED_IPS
        # IPv4-mapped IPv6 (::ffff:169.254.169.254) must be unwrapped & checked
        or (
            getattr(ip, "ipv4_mapped", None) is not None
            and _ip_is_disallowed(ip.ipv4_mapped)  # type: ignore[union-attr]
        )
    )


def check_url_safe(url: str, *, resolve: bool = True) -> tuple[bool, str | None]:
    """Validate that `url` is safe to fetch server-side.

    Returns (ok, reason). When ok is False, reason explains why (for logging /
    a 400 response). Blocks non-http(s) schemes, credentialed URLs, blocked
    hostnames, and — when resolve=True — any hostname that resolves to a
    private/loopback/link-local/reserved/metadata address.
    """
    if not url or not isinstance(url, str):
        return False, "empty url"

    parsed = urlparse(url)
    if parsed.scheme.lower() not in ("http", "https"):
        return False, f"scheme '{parsed.scheme}' not allowed (http/https only)"

    host = (parsed.hostname or "").strip().lower()
    if not host:
        return False, "no host in url"

    # user:pass@host can smuggle the real host past naive checks
    if parsed.username or parsed.password:
        return False, "credentials in url are not allowed"

    if host in _BLOCKED_HOSTNAMES:
        return False, f"host '{host}' is blocked"

    # Literal IP in the URL — check directly without DNS.
    try:
        literal = ipaddress.ip_address(host)
    except ValueError:
        pass  # not a literal IP — fall through to DNS resolution
    else:
        if _ip_is_disallowed(literal):
            return False, f"host resolves to disallowed address {literal}"
        return True, None

    if not resolve:
        return True, None

    # Resolve ALL addresses; reject if ANY is internal (DNS-rebinding-resistant
    # to the extent a pre-flight check can be — the fetch layer should ideally
    # pin to a vetted IP, but blocking here stops the trivial attacks).
    try:
        infos = _getaddrinfo(host, parsed.port or (443 if parsed.scheme == "https" else 80))
    except socket.gaierror as e:
        return False, f"dns resolution failed for '{host}': {e}"
    except Exception as e:  # noqa: BLE001 — never let a resolver quirk crash submission
        return False, f"dns error for '{host}': {type(e).__name__}"

    for info in infos:
        addr = info[4][0]
        try:
            ip = ipaddress.ip_address(addr.split("%")[0])  # strip zone id
        except ValueError:
            continue
        if _ip_is_disallowed(ip):
            return False, f"host '{host}' resolves to disallowed address {ip}"

    return True, None


def validate_url(url: str, *, resolve: bool = True) -> str:
    """Raise UnsafeURL unless `url` is safe to fetch server-side; return it unchanged."""
    ok, reason = check_url_safe(url, resolve=resolve)
    if not ok:
        raise UnsafeURL(f"unsafe url {url!r}: {reason}")
    return url
