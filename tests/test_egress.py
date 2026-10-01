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
    with pytest.raises(BlockedAddressError):
        validate_url("http://127.0.0.1:8080/admin", FetchConfig())
    validate_url("http://93.184.216.34/", FetchConfig())


def test_unresolvable_host_is_blocked():
    def resolve(host):
        raise OSError("name resolution failed")
    with pytest.raises(BlockedAddressError, match="could not resolve"):
        validate_url("http://nope.invalid/x", FetchConfig(), resolve=resolve)


def test_url_without_host_is_blocked():
    with pytest.raises(BlockedAddressError, match="no host"):
        validate_url("http:///x", FetchConfig())
