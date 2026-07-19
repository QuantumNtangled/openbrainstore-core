"""Contact / commercial-inquiry validation (pure, no DB)."""

import pytest

from openbrainstore import web_contact


def _base(**over):
    d = {
        "name": "Ada Lovelace",
        "email": "ada@example.com",
        "company": "Analytical Engines",
        "purpose": "commercial",
        "message": "We're over the threshold and want a commercial license.",
        "company_website": "",
    }
    d.update(over)
    return d


def test_valid_contact_is_cleaned():
    out = web_contact.validate(_base())
    assert out["name"] == "Ada Lovelace"
    assert out["purpose"] == "commercial"
    assert out["message"].startswith("We're over")


def test_honeypot_returns_none():
    assert web_contact.validate(_base(company_website="http://spam.example")) is None


def test_requires_name_email_purpose_message():
    with pytest.raises(web_contact.ContactError, match="name"):
        web_contact.validate(_base(name=""))
    with pytest.raises(web_contact.ContactError, match="email"):
        web_contact.validate(_base(email="nope"))
    with pytest.raises(web_contact.ContactError, match="about"):
        web_contact.validate(_base(purpose="astrology"))
    with pytest.raises(web_contact.ContactError, match="message"):
        web_contact.validate(_base(message="   "))


def test_company_optional_and_fields_capped():
    assert web_contact.validate(_base(company=""))["company"] == ""
    assert len(web_contact.validate(_base(message="x" * 9000))["message"]) == 5000


def test_all_purposes_accepted():
    for p in web_contact.PURPOSES:
        assert web_contact.validate(_base(purpose=p)) is not None
