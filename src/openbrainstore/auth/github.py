"""GitHub as the upstream identity provider. We are the OAuth authorization
server; GitHub only answers 'which human is this?'. No passwords stored,
identity is portable, and the allowlist is the whole authorization policy."""

from urllib.parse import urlencode

import httpx

from .. import config

AUTHORIZE_URL = "https://github.com/login/oauth/authorize"
TOKEN_URL = "https://github.com/login/oauth/access_token"
USER_URL = "https://api.github.com/user"


def build_authorize_url(state: str, redirect_uri: str) -> str:
    params = {
        "client_id": config.gh_client_id(),
        "redirect_uri": redirect_uri,
        "state": state,
        "scope": "read:user",
        "allow_signup": "false",
    }
    return f"{AUTHORIZE_URL}?{urlencode(params)}"


async def exchange_code(code: str, redirect_uri: str) -> dict:
    """Trade the GitHub code for the user's identity: {'id': int, 'login': str}."""
    async with httpx.AsyncClient(timeout=15) as client:
        token_res = await client.post(
            TOKEN_URL,
            headers={"Accept": "application/json"},
            data={
                "client_id": config.gh_client_id(),
                "client_secret": config.gh_client_secret(),
                "code": code,
                "redirect_uri": redirect_uri,
            },
        )
        token_res.raise_for_status()
        gh_token = token_res.json().get("access_token")
        if not gh_token:
            raise ValueError(f"github token exchange failed: {token_res.json()}")

        user_res = await client.get(
            USER_URL,
            headers={"Authorization": f"Bearer {gh_token}", "Accept": "application/vnd.github+json"},
        )
        user_res.raise_for_status()
        user = user_res.json()
        return {"id": user["id"], "login": user["login"]}
