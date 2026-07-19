"""Beta-signup validation (pure, no DB). The storage path is a thin INSERT
covered by manual verification on the deployed Postgres."""

import pytest

from openbrainstore import web_signup


def _base(**over):
    d = {
        "github_username": "octocat",
        "email": "octo@example.com",
        "role": "indie",
        "use_case": "cross-client memory",
        "acknowledged": True,
        "company_website": "",
    }
    d.update(over)
    return d


def test_valid_signup_is_cleaned():
    out = web_signup.validate(_base(github_username="@Octo-Cat"))
    assert out == {
        "github_username": "Octo-Cat",  # leading @ stripped, case preserved
        "email": "octo@example.com",
        "role": "indie",
        "use_case": "cross-client memory",
    }


def test_honeypot_returns_none_not_error():
    # a bot filling the hidden field: treated as success, stored as nothing
    assert web_signup.validate(_base(company_website="http://spam.example")) is None


@pytest.mark.parametrize("bad", ["", "has spaces", "-startshyphen", "a" * 40, "bad_underscore"])
def test_rejects_bad_github_username(bad):
    with pytest.raises(web_signup.SignupError, match="GitHub username"):
        web_signup.validate(_base(github_username=bad))


@pytest.mark.parametrize("bad", ["", "no-at-sign", "missing@domain", "@example.com"])
def test_rejects_bad_email(bad):
    with pytest.raises(web_signup.SignupError, match="email"):
        web_signup.validate(_base(email=bad))


def test_rejects_unknown_role():
    with pytest.raises(web_signup.SignupError, match="describes you"):
        web_signup.validate(_base(role="astronaut"))


def test_requires_acknowledgement():
    with pytest.raises(web_signup.SignupError, match="beta"):
        web_signup.validate(_base(acknowledged=False))


def test_acknowledgement_accepts_form_truthy_strings():
    for v in ("true", "on", "1", "yes"):
        assert web_signup.validate(_base(acknowledged=v)) is not None


def test_use_case_is_optional_and_capped():
    assert web_signup.validate(_base(use_case=""))["use_case"] == ""
    long = "x" * 5000
    assert len(web_signup.validate(_base(use_case=long))["use_case"]) == 1000
