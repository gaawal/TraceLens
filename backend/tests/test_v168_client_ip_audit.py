from types import SimpleNamespace

from apps.audits.services import request_client_ip


def _request(**meta):
    return SimpleNamespace(META=meta)


def test_client_ip_prefers_forwarded_for_chain():
    request = _request(
        HTTP_X_FORWARDED_FOR="192.168.10.25, 127.0.0.1",
        HTTP_X_REAL_IP="127.0.0.1",
        REMOTE_ADDR="127.0.0.1",
    )
    assert request_client_ip(request) == "192.168.10.25"


def test_client_ip_supports_x_real_ip():
    request = _request(HTTP_X_REAL_IP="10.20.30.40", REMOTE_ADDR="127.0.0.1")
    assert request_client_ip(request) == "10.20.30.40"


def test_client_ip_supports_rfc_forwarded_ipv6():
    request = _request(
        HTTP_FORWARDED='for="[2001:db8::1234]:4711";proto=http;by=127.0.0.1',
        HTTP_X_FORWARDED_FOR="127.0.0.1",
        REMOTE_ADDR="127.0.0.1",
    )
    assert request_client_ip(request) == "2001:db8::1234"


def test_client_ip_falls_back_to_direct_peer():
    request = _request(REMOTE_ADDR="172.18.0.5")
    assert request_client_ip(request) == "172.18.0.5"
