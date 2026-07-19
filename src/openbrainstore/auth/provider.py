"""OAuth 2.1 authorization server provider (the spec's 'minimal OAuth
implementation, acceptable at 50 users'). The MCP SDK mounts /authorize,
/token, /register and the metadata endpoints and handles PKCE verification;
this class supplies storage-backed behavior and the GitHub identity hop.

Flow: client hits /authorize -> we stash the request and bounce to GitHub ->
/github/callback verifies the human is allowlisted -> we mint our own code ->
client swaps it at /token for our access/refresh tokens -> SDK middleware
validates those tokens on every /mcp request. Memory user_id = token subject
(gh_<github-id>), finally implementing identity-scoped tenancy for real."""

import secrets
import time

from mcp.server.auth.provider import (
    AccessToken,
    AuthorizationCode,
    AuthorizationParams,
    OAuthAuthorizationServerProvider,
    RefreshToken,
    TokenError,
    construct_redirect_uri,
)
from mcp.shared.auth import OAuthClientInformationFull, OAuthToken

from . import github
from .storage import AuthStorage

ACCESS_TTL = 3600            # 1 hour
REFRESH_TTL = 30 * 86400     # 30 days
CODE_TTL = 300               # 5 minutes
STATE_TTL = 600              # 10 minutes for the GitHub round-trip
DEFAULT_SCOPES = ["memory"]


class DeniedUserError(Exception):
    """GitHub authenticated the user, but they're not on the allowlist."""


class GitHubOAuthProvider(OAuthAuthorizationServerProvider[AuthorizationCode, RefreshToken, AccessToken]):
    def __init__(self, storage: AuthStorage, issuer_url: str, allowed_users: set[str]) -> None:
        self.storage = storage
        self.issuer_url = issuer_url.rstrip("/")
        self.allowed_users = allowed_users

    @property
    def callback_uri(self) -> str:
        return f"{self.issuer_url}/github/callback"

    # ---- clients ----
    async def get_client(self, client_id: str) -> OAuthClientInformationFull | None:
        data = self.storage.get_client(client_id)
        return OAuthClientInformationFull.model_validate(data) if data else None

    async def register_client(self, client_info: OAuthClientInformationFull) -> None:
        self.storage.put_client(client_info.client_id, client_info.model_dump(mode="json"))

    # ---- authorize: stash request, hop to GitHub ----
    async def authorize(self, client: OAuthClientInformationFull, params: AuthorizationParams) -> str:
        state = secrets.token_urlsafe(32)
        self.storage.put_state(
            state,
            {"client_id": client.client_id, "params": params.model_dump(mode="json")},
            STATE_TTL,
        )
        return github.build_authorize_url(state, self.callback_uri)

    async def handle_github_callback(self, code: str, state: str) -> str:
        """Called by the /github/callback route. Returns the redirect URL back
        to the OAuth client, or raises (bad state / user not allowlisted)."""
        pending = self.storage.pop_state(state)
        if pending is None:
            raise ValueError("unknown or expired state")
        identity = await github.exchange_code(code, self.callback_uri)
        if identity["login"] not in self.allowed_users:
            raise DeniedUserError(f"github user {identity['login']!r} is not allowlisted")

        params = AuthorizationParams.model_validate(pending["params"])
        subject = f"gh_{identity['id']}"
        auth_code = AuthorizationCode(
            code=f"obs_ac_{secrets.token_urlsafe(32)}",
            scopes=params.scopes or DEFAULT_SCOPES,
            expires_at=time.time() + CODE_TTL,
            client_id=pending["client_id"],
            code_challenge=params.code_challenge,
            redirect_uri=params.redirect_uri,
            redirect_uri_provided_explicitly=params.redirect_uri_provided_explicitly,
            resource=params.resource,
            subject=subject,
        )
        self.storage.put_code(
            auth_code.code, auth_code.model_dump(mode="json"), auth_code.expires_at
        )
        return construct_redirect_uri(str(params.redirect_uri), code=auth_code.code, state=params.state)

    # ---- codes ----
    async def load_authorization_code(
        self, client: OAuthClientInformationFull, authorization_code: str
    ) -> AuthorizationCode | None:
        data = self.storage.get_code(authorization_code)
        if data is None or data.get("client_id") != client.client_id:
            return None
        return AuthorizationCode.model_validate(data)

    async def exchange_authorization_code(
        self, client: OAuthClientInformationFull, authorization_code: AuthorizationCode
    ) -> OAuthToken:
        self.storage.delete_code(authorization_code.code)
        return self._mint(client.client_id, authorization_code.scopes,
                          authorization_code.subject, authorization_code.resource)

    # ---- refresh ----
    async def load_refresh_token(
        self, client: OAuthClientInformationFull, refresh_token: str
    ) -> RefreshToken | None:
        data = self.storage.get_token(refresh_token, "refresh")
        if data is None or data.get("client_id") != client.client_id:
            return None
        return RefreshToken.model_validate({**data, "token": refresh_token})

    async def exchange_refresh_token(
        self, client: OAuthClientInformationFull, refresh_token: RefreshToken, scopes: list[str]
    ) -> OAuthToken:
        stored = self.storage.get_token(refresh_token.token, "refresh")
        if stored is None:
            raise TokenError("invalid_grant", "refresh token no longer valid")
        self.storage.delete_token(refresh_token.token)  # rotation
        return self._mint(client.client_id, scopes or refresh_token.scopes,
                          stored.get("subject"), stored.get("resource"))

    # ---- access ----
    async def load_access_token(self, token: str) -> AccessToken | None:
        data = self.storage.get_token(token, "access")
        if data is None:
            return None
        return AccessToken.model_validate({**data, "token": token})

    async def revoke_token(self, token: AccessToken | RefreshToken) -> None:
        self.storage.delete_token(token.token)

    # ---- minting ----
    def _mint(self, client_id: str, scopes: list[str], subject: str | None,
              resource: str | None) -> OAuthToken:
        access = f"obs_at_{secrets.token_urlsafe(32)}"
        refresh = f"obs_rt_{secrets.token_urlsafe(32)}"
        now = time.time()
        common = {"client_id": client_id, "scopes": scopes, "subject": subject, "resource": resource}
        self.storage.put_token(access, "access",
                               {**common, "expires_at": int(now + ACCESS_TTL)}, now + ACCESS_TTL)
        self.storage.put_token(refresh, "refresh",
                               {**common, "expires_at": int(now + REFRESH_TTL)}, now + REFRESH_TTL)
        return OAuthToken(
            access_token=access,
            expires_in=ACCESS_TTL,
            scope=" ".join(scopes),
            refresh_token=refresh,
        )
