"""OBS_ALLOWED_HOSTS wiring for the streamable-HTTP DNS-rebinding allowlist."""

import importlib

import pytest


@pytest.fixture()
def server_mod(monkeypatch):
    def _load():
        import openbrainstore.server as s
        return importlib.reload(s)
    return _load


def test_no_allowed_hosts_defaults_to_none(server_mod, monkeypatch):
    monkeypatch.delenv("OBS_ALLOWED_HOSTS", raising=False)
    assert server_mod()._transport_security() is None


def test_allowed_hosts_configures_settings(server_mod, monkeypatch):
    monkeypatch.setenv("OBS_ALLOWED_HOSTS", "example.sslip.io, mem.example.com")
    sec = server_mod()._transport_security()
    assert sec is not None
    assert sec.enable_dns_rebinding_protection is True
    assert sec.allowed_hosts == ["example.sslip.io", "mem.example.com"]
    assert "https://example.sslip.io" in sec.allowed_origins
    assert "http://mem.example.com" in sec.allowed_origins
