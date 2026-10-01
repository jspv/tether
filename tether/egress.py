"""Egress policy: the single place that decides whether a URL may be fetched.

The model chooses the URLs this project fetches, and fetched content is itself untrusted,
so egress is a model-controlled capability. This module denies the internal address space
by default and re-validates every redirect hop, so a 302 cannot walk a request inward.

Known residual risk: validation resolves the hostname and then hands the URL to httpx,
which resolves it again -- a DNS entry that changes in between (rebinding) is not caught.
Closing that needs connect-to-validated-IP with a Host override; see the spec's out-of-scope
section.
"""

from __future__ import annotations

import ipaddress
import socket
from collections.abc import Callable
from urllib.parse import urlparse

from .config import FetchConfig

# RFC 6598 carrier-grade NAT. Verified: no ipaddress flag reports this range private,
# reserved, or otherwise special, yet it is routinely routable inside cloud and carrier
# networks. Every other special-use range is already covered by the flag checks below.
_EXTRA_DENIED_NETS = (ipaddress.ip_network("100.64.0.0/10"),)
_METADATA_IPS = frozenset({"169.254.169.254", "fd00:ec2::254"})


class BlockedAddressError(ValueError):
    """Raised when a URL's host resolves to an address egress policy forbids."""


def _resolve(host: str) -> list[str]:
    return [info[4][0] for info in socket.getaddrinfo(host, None)]


def _is_denied(addr: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    if str(addr) in _METADATA_IPS:
        return True
    if any(addr in net for net in _EXTRA_DENIED_NETS):
        return True
    return bool(addr.is_loopback or addr.is_private or addr.is_link_local
                or addr.is_reserved or addr.is_multicast or addr.is_unspecified)


def _allowed_by_config(host: str, addr: ipaddress.IPv4Address | ipaddress.IPv6Address,
                       cfg: FetchConfig) -> bool:
    for entry in cfg.allow_private_hosts:
        if entry.lower() == host.lower():
            return True
        try:
            if addr in ipaddress.ip_network(entry, strict=False):
                return True
        except ValueError:
            continue       # a hostname entry, already compared above
    return False


def validate_url(url: str, cfg: FetchConfig, *,
                 resolve: Callable[[str], list[str]] | None = None) -> None:
    """Raise BlockedAddressError unless every address ``url``'s host resolves to is allowed.

    *Every* address must pass: a host answering with both a public and an internal address
    is denied, because the attacker -- not us -- effectively picks which one is connected to.
    ``resolve`` is injectable so tests never perform DNS.
    """
    host = urlparse(url).hostname
    if not host:
        raise BlockedAddressError(f"no host in url: {url!r}")

    try:                                  # a literal IP needs no resolution
        addrs = [str(ipaddress.ip_address(host))]
    except ValueError:
        resolver = resolve or _resolve
        try:
            addrs = resolver(host)
        except OSError as e:
            raise BlockedAddressError(f"could not resolve host {host!r}: {e}") from e
        if not addrs:
            raise BlockedAddressError(f"could not resolve host {host!r}: no addresses")

    for raw in addrs:
        addr = ipaddress.ip_address(raw)
        if _is_denied(addr) and not _allowed_by_config(host, addr, cfg):
            raise BlockedAddressError(
                f"blocked internal address {raw} for host {host!r}; add the host or its "
                f"CIDR to FetchConfig.allow_private_hosts to permit it")
