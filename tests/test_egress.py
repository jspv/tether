import pytest

from tether.config import FetchConfig
from tether.egress import BlockedAddressError, validate_url


def _resolver(mapping):
    """Stub DNS: hostname -> list of addresses. No network in tests."""
    def resolve(host: str) -> list[str]:
        return mapping[host]
    return resolve


@pytest.mark.parametrize("addr,label", [
    ("127.0.0.1", "ipv4 loopback"),
    ("::1", "ipv6 loopback"),
    ("::ffff:127.0.0.1", "ipv4-mapped loopback"),
    ("10.0.0.5", "private 10/8"),
    ("172.16.0.1", "private 172.16/12"),
    ("192.168.1.1", "private 192.168/16"),
    ("fd00::1", "ipv6 unique-local"),
    ("169.254.169.254", "cloud metadata"),
    ("169.254.1.1", "link-local"),
    ("0.0.0.0", "unspecified"),
    ("224.0.0.1", "multicast"),
    ("100.64.0.1", "RFC6598 CGNAT — no standard flag catches this one"),
])
def test_validate_url_rejects_internal_addresses(addr, label):
    cfg = FetchConfig()
    with pytest.raises(BlockedAddressError):
        validate_url("http://target.example/x", cfg,
                     resolve=_resolver({"target.example": [addr]}))


@pytest.mark.parametrize("addr", ["93.184.216.34", "2606:2800:220:1:248:1893:25c8:1946"])
def test_validate_url_allows_public_addresses(addr):
    validate_url("https://example.com/x", FetchConfig(),
                 resolve=_resolver({"example.com": [addr]}))


def test_any_denied_address_blocks_the_host():
    """A hostname resolving to both a public and a private address is denied -- the
    attacker picks which one connect() uses, so one bad answer is enough."""
    with pytest.raises(BlockedAddressError):
        validate_url("https://split.example/x", FetchConfig(),
                     resolve=_resolver({"split.example": ["93.184.216.34", "127.0.0.1"]}))


def test_allowlist_admits_a_named_host():
    cfg = FetchConfig(allow_private_hosts=("intranet.corp",))
    validate_url("http://intranet.corp/report", cfg,
                 resolve=_resolver({"intranet.corp": ["10.1.2.3"]}))


def test_allowlist_is_case_insensitive():
    cfg = FetchConfig(allow_private_hosts=("Intranet.Corp",))
    validate_url("http://intranet.corp/x", cfg,
                 resolve=_resolver({"intranet.corp": ["10.1.2.3"]}))


def test_allowlist_admits_a_cidr():
    cfg = FetchConfig(allow_private_hosts=("10.1.0.0/16",))
    validate_url("http://db.internal/x", cfg,
                 resolve=_resolver({"db.internal": ["10.1.2.3"]}))


def test_cidr_allowlist_does_not_admit_other_private_space():
    cfg = FetchConfig(allow_private_hosts=("10.1.0.0/16",))
    with pytest.raises(BlockedAddressError):
        validate_url("http://other.internal/x", cfg,
                     resolve=_resolver({"other.internal": ["10.9.9.9"]}))


def test_empty_allowlist_admits_nothing_internal():
    assert FetchConfig().allow_private_hosts == ()
    with pytest.raises(BlockedAddressError):
        validate_url("http://x.internal/x", FetchConfig(),
                     resolve=_resolver({"x.internal": ["10.0.0.1"]}))


def test_literal_ip_url_is_checked_without_dns():
    """A URL with a bare IP must be validated too, and must not need a resolver."""
    def must_not_resolve(host):
        raise AssertionError(f"resolver called for literal IP {host!r}")

    with pytest.raises(BlockedAddressError):
        validate_url("http://127.0.0.1:8080/admin", FetchConfig(), resolve=must_not_resolve)
    validate_url("http://93.184.216.34/", FetchConfig(), resolve=must_not_resolve)


def test_unresolvable_host_is_blocked():
    def resolve(host):
        raise OSError("name resolution failed")
    with pytest.raises(BlockedAddressError, match="could not resolve"):
        validate_url("http://nope.invalid/x", FetchConfig(), resolve=resolve)


def test_url_without_host_is_blocked():
    with pytest.raises(BlockedAddressError, match="no host"):
        validate_url("http:///x", FetchConfig())


def test_resolver_returning_empty_list_is_blocked():
    """A resolver returning no addresses is a denial, not a pass-through."""
    with pytest.raises(BlockedAddressError, match="no addresses"):
        validate_url("http://nope.example/x", FetchConfig(),
                     resolve=_resolver({"nope.example": []}))


def test_unicode_error_on_resolution_is_blocked():
    """IDNA encoding errors (e.g., over-long labels) raise UnicodeError, which must be caught."""
    def resolve_unicode_error(host):
        raise UnicodeError("label too long")
    with pytest.raises(BlockedAddressError, match="could not resolve"):
        validate_url("http://toolonglabel.example/x", FetchConfig(), resolve=resolve_unicode_error)


@pytest.mark.parametrize("metadata_addr,label", [
    ("169.254.169.254", "AWS/GCP/Azure IMDS"),
    ("169.254.170.2", "AWS ECS task credentials"),
    ("168.63.129.16", "Azure wireserver (public)"),
    ("100.100.100.200", "Alibaba Cloud (in CGNAT)"),
    ("192.0.0.192", "Oracle Cloud"),
    ("fd00:ec2::254", "AWS IMDS IPv6"),
])
def test_metadata_endpoints_are_denied(metadata_addr, label):
    """All known cloud metadata endpoints are denied by default."""
    with pytest.raises(BlockedAddressError, match="blocked cloud metadata endpoint"):
        validate_url("http://target.example/x", FetchConfig(),
                     resolve=_resolver({"target.example": [metadata_addr]}))


@pytest.mark.parametrize("metadata_addr", [
    "169.254.169.254",
    "169.254.170.2",
    "168.63.129.16",
    "100.100.100.200",
    "192.0.0.192",
    "fd00:ec2::254",
])
def test_metadata_endpoints_cannot_be_allowlisted_by_hostname(metadata_addr):
    """Metadata endpoints cannot be overridden by hostname allowlist."""
    # Attempt to allowlist the hostname; metadata check happens before allowlist
    cfg = FetchConfig(allow_private_hosts=("metadata.example",))
    with pytest.raises(BlockedAddressError, match="blocked cloud metadata endpoint"):
        validate_url("http://metadata.example/x", cfg,
                     resolve=_resolver({"metadata.example": [metadata_addr]}))


def test_metadata_endpoints_cannot_be_allowlisted_by_cidr():
    """Metadata endpoints cannot be overridden by CIDR allowlist."""
    # Attempt to allowlist the 169.254.0.0/16 range containing AWS IMDS
    cfg = FetchConfig(allow_private_hosts=("169.254.0.0/16",))
    with pytest.raises(BlockedAddressError, match="blocked cloud metadata endpoint"):
        validate_url("http://target.example/x", cfg,
                     resolve=_resolver({"target.example": ["169.254.169.254"]}))


def test_allowlisted_host_with_mixed_private_addresses():
    """A host resolving to both allowed (in CIDR) and disallowed (outside CIDR) private addresses is denied."""
    cfg = FetchConfig(allow_private_hosts=("10.1.0.0/16",))
    # 10.1.2.3 is inside the allowed CIDR, but 172.16.0.1 is not.
    # The host resolves to both, so it's denied (all addresses must pass).
    with pytest.raises(BlockedAddressError):
        validate_url("http://internal.corp/x", cfg,
                     resolve=_resolver({"internal.corp": ["10.1.2.3", "172.16.0.1"]}))
