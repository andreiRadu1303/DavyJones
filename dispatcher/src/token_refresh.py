"""Auto-refresh Anthropic OAuth tokens before they expire."""

import json
import logging
import os
import time
import urllib.request
import urllib.error
from dataclasses import dataclass
from enum import Enum

logger = logging.getLogger(__name__)

OAUTH_ENDPOINT = "https://console.anthropic.com/v1/oauth/token"
CLIENT_ID = "9d1c250a-e61b-44d9-88ed-5944d1962f5e"
REFRESH_MARGIN_S = 300          # refresh 5 min before expiry
EXPIRING_WARN_MARGIN_S = 1800   # warn 30 min before expiry
MAX_RETRIES = 3
RETRY_BASE_DELAY_S = 2


class CredStatus(str, Enum):
    OK = "ok"
    EXPIRING = "expiring"
    REFRESH_FAILED = "refresh_failed"
    AUTH_EXPIRED = "auth_expired"
    NO_CREDENTIALS = "no_credentials"


@dataclass
class CredHealth:
    status: CredStatus = CredStatus.OK
    expires_at: int = 0           # ms epoch
    last_refresh: float = 0.0     # unix timestamp of last successful refresh
    error: str | None = None

    def to_dict(self) -> dict:
        return {
            "status": self.status.value,
            "expires_at": self.expires_at,
            "last_refresh": self.last_refresh,
            "error": self.error,
        }


# Module-level state so main.py can read it for heartbeat
# Default to NO_CREDENTIALS until the first check proves otherwise
_health = CredHealth(status=CredStatus.NO_CREDENTIALS)


def get_cred_health() -> CredHealth:
    """Return the current credential health state."""
    return _health


def _read_creds(creds_path: str) -> dict | None:
    """Read and parse the credentials JSON file."""
    try:
        with open(creds_path) as f:
            return json.load(f)
    except Exception:
        return None


def _get_expiry_ms(data: dict) -> int:
    """Extract the expiresAt timestamp (ms) from credentials."""
    return data.get("claudeAiOauth", {}).get("expiresAt", 0)


def needs_refresh(creds_path: str) -> bool:
    """Check if the access token is expired or about to expire."""
    data = _read_creds(creds_path)
    if not data:
        return False
    expires_at = _get_expiry_ms(data)
    now_ms = int(time.time() * 1000)
    return now_ms >= (expires_at - REFRESH_MARGIN_S * 1000)


def refresh_token(creds_path: str) -> bool:
    """Refresh with retry. Returns True on success.

    Updates module-level _health accordingly.
    """
    global _health

    data = _read_creds(creds_path)
    if not data:
        _health = CredHealth(
            status=CredStatus.NO_CREDENTIALS,
            error="Credentials file missing or unreadable",
        )
        return False

    oauth = data.get("claudeAiOauth", {})
    refresh_tok = oauth.get("refreshToken")
    if not refresh_tok:
        _health = CredHealth(
            status=CredStatus.AUTH_EXPIRED,
            error="No refresh token in credentials",
        )
        return False

    payload = json.dumps({
        "grant_type": "refresh_token",
        "refresh_token": refresh_tok,
        "client_id": CLIENT_ID,
    }).encode()

    last_error = None
    for attempt in range(MAX_RETRIES):
        try:
            logger.info(
                "Refreshing OAuth token (attempt %d/%d)...",
                attempt + 1, MAX_RETRIES,
            )

            req = urllib.request.Request(
                OAUTH_ENDPOINT,
                data=payload,
                headers={
                    "Content-Type": "application/json",
                    "User-Agent": "claude-code/2.1.71",
                },
                method="POST",
            )

            with urllib.request.urlopen(req, timeout=15) as resp:
                result = json.loads(resp.read().decode())

            new_access = result.get("access_token")
            new_refresh = result.get("refresh_token")
            expires_in = result.get("expires_in", 28800)

            if not new_access:
                last_error = "Refresh response missing access_token"
                continue

            # Write refreshed credentials back
            oauth["accessToken"] = new_access
            if new_refresh:
                oauth["refreshToken"] = new_refresh
            new_expires_at = int(time.time() * 1000) + (expires_in * 1000)
            oauth["expiresAt"] = new_expires_at
            data["claudeAiOauth"] = oauth

            with open(creds_path, "w") as f:
                json.dump(data, f, indent=2)

            _health = CredHealth(
                status=CredStatus.OK,
                expires_at=new_expires_at,
                last_refresh=time.time(),
            )
            logger.info("Token refreshed, expires in %ds", expires_in)
            return True

        except urllib.error.HTTPError as e:
            body = e.read().decode() if e.fp else ""
            last_error = f"HTTP {e.code}: {body[:200]}"

            # 400/401/403 = auth is dead, no point retrying
            # 400 with invalid_grant means refresh token is revoked/expired
            if e.code in (400, 401, 403):
                logger.error(
                    "Auth expired (HTTP %d). Re-login required.", e.code,
                )
                _health = CredHealth(
                    status=CredStatus.AUTH_EXPIRED,
                    expires_at=_get_expiry_ms(data),
                    error=(
                        f"Refresh token rejected (HTTP {e.code}). "
                        "Run 'claude login' on the host."
                    ),
                )
                return False

            # 429, 5xx = transient, retry
            logger.warning(
                "Transient refresh error (HTTP %d), retrying...", e.code,
            )

        except Exception as exc:
            last_error = str(exc)
            logger.warning("Refresh error: %s, retrying...", exc)

        if attempt < MAX_RETRIES - 1:
            delay = RETRY_BASE_DELAY_S * (2 ** attempt)
            time.sleep(delay)

    # All retries exhausted
    _health = CredHealth(
        status=CredStatus.REFRESH_FAILED,
        expires_at=_get_expiry_ms(data),
        error=last_error,
    )
    logger.error(
        "Token refresh failed after %d attempts: %s", MAX_RETRIES, last_error,
    )
    return False


def ensure_valid_token(creds_path: str) -> None:
    """Refresh the token if needed. Updates health state regardless."""
    global _health

    # Long-lived token from setup-token — no refresh needed
    if os.environ.get("CLAUDE_CODE_OAUTH_TOKEN"):
        _health = CredHealth(status=CredStatus.OK, expires_at=0)
        return

    data = _read_creds(creds_path)
    if not data:
        _health = CredHealth(
            status=CredStatus.NO_CREDENTIALS,
            error="Credentials file missing",
        )
        return

    expires_at = _get_expiry_ms(data)
    now_ms = int(time.time() * 1000)

    if now_ms >= (expires_at - REFRESH_MARGIN_S * 1000):
        # Token expired or about to — attempt refresh
        refresh_token(creds_path)
    elif now_ms >= (expires_at - EXPIRING_WARN_MARGIN_S * 1000):
        # Token still valid but approaching expiry — informational warning
        if _health.status == CredStatus.OK:
            _health = CredHealth(
                status=CredStatus.EXPIRING,
                expires_at=expires_at,
                last_refresh=_health.last_refresh,
            )
    else:
        # Token is healthy — update state (unless auth is already dead)
        if _health.status != CredStatus.AUTH_EXPIRED:
            _health = CredHealth(
                status=CredStatus.OK,
                expires_at=expires_at,
                last_refresh=_health.last_refresh,
            )
