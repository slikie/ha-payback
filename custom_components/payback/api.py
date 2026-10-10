"""PAYBACK Deutschland API client.

Provenance of every decision in this file — this was rewritten after the 1.0.0
client was found to be non-functional, so the evidence matters more than usual:

* The 1.0.0 client called ``https://www.payback.de/pb/v1/{sessions,account}``.
  Both return **HTTP 404** — verified from two independent networks (a Home
  Assistant host in Europe and a cloud VM). ``/pb/v1/`` even 301-redirects, so
  that namespace is a web route, not an API.
* The real host is ``services-ext.payback.de``. Probing
  ``https://services-ext.payback.de/<principal>/v1/json/getcoupons`` returns
  **HTTP 401 Authorization Required** — i.e. the host and path exist and want
  credentials, which is what the previous client never supplied.
* Endpoint names, payload shapes and the coupon response schema below are taken
  from the reverse-engineered reference implementation
  ``theodm/python-coupons`` (``src/payback/PaybackAPI.py``), which is the only
  public source that documents them.

Two things are therefore NOT guesses but copied from that reference: the request
bodies, and the fact that the host needs **HTTP Basic auth with credentials
extracted from the official mobile app** (its ``secrets.properties`` template
documents this explicitly). Those app credentials are required — without them
this returns 401, and no amount of retrying will change that.

Known-unverified piece: the *response* schema of ``getaccountbalance``. The
reference returns that payload raw without reading any field, so the balance
parser below is deliberately permissive and must be checked against a live
response before anyone trusts those three numbers.
"""

from __future__ import annotations

import logging
from typing import Any, Literal

from curl_cffi import requests
from pydantic import BaseModel, ConfigDict, Field

_LOGGER = logging.getLogger(__name__)

# ── Endpoints ────────────────────────────────────────────────────────────────
# Base URL is host + principal + version; every action is POST .../json/<action>.
PAYBACK_API_HOST = "https://services-ext.payback.de"
PAYBACK_API_VERSION = "v1"
# The "principal" identifies the client application to PAYBACK (138/139 in the
# reference implementation). It travels both in the URL and in the body.
DEFAULT_PRINCIPAL = "139"

API_PATH_SECURE_AUTHENTICATE = "secureauthenticate"
API_PATH_GET_ACCOUNT_BALANCE = "getaccountbalance"
API_PATH_GET_COUPONS = "getcoupons"
API_PATH_ACTIVATE_COUPON = "activatecoupon"

# The reference implementation impersonates the app; keep that, and its UA.
DEFAULT_USER_AGENT = "DSA/24.02.0101(1707819571) iOS/16.1.1"
DEFAULT_TIMEOUT = 20.0

# aliasType for the login "alias": 1 = Kundennummer (KdNr), 5 = e-mail address.
ALIAS_TYPE_CARD_NUMBER = 1
ALIAS_TYPE_EMAIL = 5
SECRET_TYPE_PASSWORD = 3

# Refused before any request is made, so the failure is obvious instead of
# looking like an empty account.
_PLACEHOLDER_CREDENTIALS = {
    "",
    "LKAJSDkaskdjskds",  # example value shipped in the reference's template
    "123456789ABCDEFGHIJK+",
}

# couponStatus: 1 = available, 2 = activated, 3 = used, 4 = used (?)
COUPON_STATUS_AVAILABLE = 1
COUPON_STATUS_ACTIVATED = 2


class PaybackApiError(RuntimeError):
    """Any failed PAYBACK API interaction."""


class PaybackAuthError(PaybackApiError):
    """Credentials rejected, or app credentials missing. Not retryable."""


class PaybackPoints(BaseModel):
    """Points breakdown.

    Field names are unverified — see the module docstring. The parser tolerates
    several spellings rather than asserting one.
    """

    model_config = ConfigDict(extra="allow")

    total_points: int = 0
    available_points: int = 0
    pending_points: int = 0


class PaybackCoupon(BaseModel):
    """A coupon, mapped from the verified ``couponListItem[].coupon`` schema."""

    model_config = ConfigDict(extra="allow")

    coupon_id: str = ""
    status: int = 0
    activated: bool = False
    valid_from: str = ""
    valid_to: str = ""
    partner_name: str = ""
    title: str = ""
    description: str = ""


class PaybackAccount(BaseModel):
    """Account summary: points plus the coupon list."""

    model_config = ConfigDict(extra="allow")

    card_number: str = ""
    customer_name: str = ""
    points: PaybackPoints = Field(default_factory=PaybackPoints)
    coupons: list[PaybackCoupon] = Field(default_factory=list)


def _first_non_empty(values: list[Any]) -> str:
    """First non-empty string from a list, else ''."""
    for value in values:
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


class PaybackAPIClient:
    """Client for the PAYBACK mobile API, via curl_cffi TLS impersonation."""

    def __init__(
        self,
        username: str,
        password: str,
        *,
        session_cookie: str | None = None,
        username_is_card_number: bool | None = None,
        basic_auth_username: str | None = None,
        basic_auth_credential: str | None = None,
        principal: str | None = None,
        user_agent: str | None = None,
        access_token: str | None = None,
    ) -> None:
        """Build a client.

        ``basic_auth_username``/``basic_auth_credential`` are the app-level HTTP
        Basic credentials the host demands (the 401). They come from the official
        app, so they must be supplied by the user; a placeholder is refused up
        front with a clear message.

        ``access_token`` skips ``secureauthenticate`` entirely and reuses a token
        captured from an already-logged-in session. That path needs no app
        credentials and no interactive login, which sidesteps both the app
        reverse-engineering and PAYBACK's reCAPTCHA-gated login.
        """
        self.username = (username or "").strip()
        self.password = password or ""
        self.session_cookie = session_cookie
        self.principal = (principal or DEFAULT_PRINCIPAL).strip() or DEFAULT_PRINCIPAL
        self.user_agent = user_agent or DEFAULT_USER_AGENT
        self.basic_auth_username = (basic_auth_username or "").strip()
        self.basic_auth_credential = (basic_auth_credential or "").strip()

        # Digits => Kundennummer, otherwise treat as e-mail. Same rule the
        # reference implementation uses.
        self.username_is_card_number = (
            self.username.isdigit()
            if username_is_card_number is None
            else username_is_card_number
        )

        self.session = requests.Session()
        self._authentication: dict[str, Any] | None = (
            {"token": access_token} if access_token else None
        )

        if session_cookie:
            for item in session_cookie.split(";"):
                if "=" in item:
                    key, value = item.strip().split("=", 1)
                    self.session.cookies.set(key, value, domain=".payback.de")

    # ── plumbing ────────────────────────────────────────────────────────────
    @property
    def base_url(self) -> str:
        """``https://services-ext.payback.de/<principal>/v1``."""
        return f"{PAYBACK_API_HOST}/{self.principal}/{PAYBACK_API_VERSION}"

    def _consumer_identification(self) -> dict[str, Any]:
        return {
            "consumerIdentification": {
                "consumerAuthentication": {
                    "credential": self.basic_auth_credential,
                    "principal": self.principal,
                }
            }
        }

    def _require_credentials(self) -> None:
        """Fail loudly *before* a request when the app credentials are missing."""
        missing = []
        if not self.basic_auth_username:
            missing.append("basic_auth_username")
        if self.basic_auth_credential in _PLACEHOLDER_CREDENTIALS:
            missing.append("basic_auth_credential")
        if missing:
            raise PaybackAuthError(
                "PAYBACK app credentials are not configured ("
                + ", ".join(missing)
                + "). The API host requires HTTP Basic auth with credentials taken "
                "from the official PAYBACK app; see docs/AUTH.md. Alternatively "
                "supply a session access token to skip login entirely."
            )

    def _request(
        self,
        action: str,
        json_data: dict[str, Any] | None = None,
        *,
        auth: tuple[str, str] | None = None,
    ) -> Any:
        """POST ``<base>/json/<action>`` and return the JSON body.

        Raises ``PaybackAuthError`` on 401/403 and ``PaybackApiError`` otherwise —
        the previous version logged at debug level and returned an empty result,
        which is what let a completely broken client look healthy in Home
        Assistant for days.
        """
        url = f"{self.base_url}/json/{action}"
        headers = {
            "Accept": "application/json; charset=utf-8",
            "Content-Type": "application/json; charset=utf8",
            "Accept-Encoding": "gzip",
            "User-Agent": self.user_agent,
            "Accept-Language": "de-DE,de;q=0.9",
        }
        try:
            response = self.session.post(
                url,
                json=json_data or {},
                headers=headers,
                auth=auth,
                impersonate="chrome",
                timeout=DEFAULT_TIMEOUT,
            )
        except Exception as exc:  # network / TLS / timeout
            raise PaybackApiError(f"PAYBACK request to {action} failed: {exc}") from exc

        if response.status_code in (401, 403):
            raise PaybackAuthError(
                f"PAYBACK rejected the credentials for {action} "
                f"(HTTP {response.status_code}). Check the app Basic-auth "
                "credentials and the principal."
            )
        if response.status_code >= 400:
            raise PaybackApiError(
                f"PAYBACK {action} returned HTTP {response.status_code}: "
                f"{response.text[:200]!r}"
            )
        if not response.content:
            raise PaybackApiError(f"PAYBACK {action} returned an empty body")

        try:
            return response.json()
        except Exception as exc:
            raise PaybackApiError(
                f"PAYBACK {action} returned non-JSON content "
                f"({response.headers.get('Content-Type')}): {response.text[:200]!r}"
            ) from exc

    # ── authentication ──────────────────────────────────────────────────────
    def login(self) -> dict[str, Any]:
        """Authenticate and return the ``authentication`` object (contains token).

        Raises on failure. A pre-supplied ``access_token`` short-circuits this.
        """
        if self._authentication:
            return self._authentication
        if not self.username or not self.password:
            raise PaybackAuthError("A username and password are required to log in.")

        self._require_credentials()

        alias_type = (
            ALIAS_TYPE_CARD_NUMBER if self.username_is_card_number else ALIAS_TYPE_EMAIL
        )
        payload = {
            **self._consumer_identification(),
            "authorizationRequestValidityDuration": 120,
            "authentication": {
                "identification": {"alias": self.username, "aliasType": alias_type},
                "security": {"secret": self.password, "secretType": SECRET_TYPE_PASSWORD},
            },
        }

        data = self._request(
            API_PATH_SECURE_AUTHENTICATE,
            payload,
            auth=(self.basic_auth_username, self.basic_auth_credential),
        )
        authentication = data.get("authentication") if isinstance(data, dict) else None
        if not isinstance(authentication, dict) or not authentication.get("token"):
            raise PaybackAuthError(
                "PAYBACK login succeeded but no token was returned; the response "
                f"shape may have changed: {list(data)[:10] if isinstance(data, dict) else type(data)}"
            )

        self._authentication = authentication
        _LOGGER.debug("PAYBACK login OK, token acquired")
        return authentication

    def _token_body(self) -> dict[str, Any]:
        if not self._authentication:
            self.login()
        return {"authentication": self._authentication, **self._consumer_identification()}

    # ── data ────────────────────────────────────────────────────────────────
    def get_account(self) -> PaybackAccount:
        """Fetch the points balance and the coupon list. Raises on any failure."""
        coupons = self.get_coupons()
        balance_ok = True
        balance: dict[str, Any] = {}
        try:
            balance = self._request(API_PATH_GET_ACCOUNT_BALANCE, self._token_body())
        except PaybackAuthError:
            raise
        except PaybackApiError as exc:
            # A missing balance must not hide the coupons we did get, but it also
            # must not be silently reported as zero points.
            balance_ok = False
            _LOGGER.warning("PAYBACK balance request failed: %s", exc)

        points = self._parse_points(balance if isinstance(balance, dict) else {})
        if not balance_ok:
            _LOGGER.warning(
                "PAYBACK points are unavailable this cycle; they are reported as 0 "
                "only because no other value exists."
            )

        return PaybackAccount(
            card_number=_first_non_empty(
                [
                    balance.get("cardNumber") if isinstance(balance, dict) else "",
                    balance.get("cardnumber") if isinstance(balance, dict) else "",
                    self.username if self.username_is_card_number else "",
                ]
            ),
            customer_name=_first_non_empty(
                [
                    balance.get("customerName") if isinstance(balance, dict) else "",
                    balance.get("firstName") if isinstance(balance, dict) else "",
                    balance.get("name") if isinstance(balance, dict) else "",
                ]
            ),
            points=points,
            coupons=coupons,
        )

    @staticmethod
    def _parse_points(balance: dict[str, Any]) -> PaybackPoints:
        """Map a balance payload onto ``PaybackPoints``.

        Unverified: the reference implementation never reads this response, so
        several plausible spellings are accepted and anything else is ignored
        rather than invented.
        """

        def pick(*names: str) -> int:
            for name in names:
                value = balance.get(name)
                if isinstance(value, dict):
                    value = value.get("points") or value.get("value")
                if isinstance(value, (int, float)):
                    return int(value)
                if isinstance(value, str) and value.strip().lstrip("-").isdigit():
                    return int(value.strip())
            return 0

        found = {
            k: v
            for k, v in balance.items()
            if isinstance(v, (int, float, str, dict))
            and any(t in k.lower() for t in ("point", "balance", "punkt"))
        }
        if found:
            _LOGGER.debug("PAYBACK balance keys present: %s", list(found)[:12])
        else:
            _LOGGER.debug(
                "PAYBACK balance response had no recognisable points keys: %s",
                list(balance)[:12],
            )

        return PaybackPoints(
            total_points=pick("pointsBalance", "totalPoints", "total", "balance"),
            available_points=pick("availablePoints", "redeemablePoints", "available"),
            pending_points=pick("pendingPoints", "pending"),
        )

    def get_coupons(self) -> list[PaybackCoupon]:
        """Fetch and map the coupon list. Raises on failure.

        Response schema (verified against the reference implementation):
        ``{"couponListItem": [{"coupon": {...}}, ...]}`` where each coupon has
        ``couponID``, ``couponStatus``, ``partner[].partnerDisplayName``,
        ``validity.validFrom/validTo`` and ``couponContentSet.textItem[].textValue``.
        """
        from datetime import datetime

        payload = {
            **self._token_body(),
            "couponFilter": {"couponDistributionChannel": [5]},
            "couponPeriodFilter": [
                {
                    "periodQuery": 9,
                    "referenceDate": datetime.now().strftime("%Y-%m-%dT%H:%M:%S+0200"),
                }
            ],
            # Required by the API; a fixed point in Germany is what the reference
            # sends, and coupon availability is location-filtered server-side.
            "locationFilter": {
                "position": {"latitude": 51.165691, "longitude": 10.451526}
            },
        }

        data = self._request(API_PATH_GET_COUPONS, payload)
        items = data.get("couponListItem") if isinstance(data, dict) else None
        if items is None:
            # An empty object is how "no coupons this period" looks, so it must not
            # be reported as a failure -- but a body that carries keys while
            # missing the list means the schema moved, which must not be mistaken
            # for an empty account either.
            if isinstance(data, dict) and not data:
                _LOGGER.info("PAYBACK returned no coupons for this period")
                return []
            raise PaybackApiError(
                "PAYBACK getcoupons response had no 'couponListItem' key; the schema "
                f"may have changed: {list(data)[:10] if isinstance(data, dict) else type(data)}"
            )

        coupons: list[PaybackCoupon] = []
        for entry in items or []:
            coupon = entry.get("coupon") if isinstance(entry, dict) else None
            if not isinstance(coupon, dict):
                continue

            partners = coupon.get("partner") or []
            partner_name = ""
            if partners and isinstance(partners[0], dict):
                partner_name = str(partners[0].get("partnerDisplayName") or "")

            texts: list[Any] = []
            content_set = coupon.get("couponContentSet")
            if isinstance(content_set, dict):
                for item in content_set.get("textItem") or []:
                    if isinstance(item, dict):
                        texts.append(item.get("textValue"))

            validity = coupon.get("validity") or {}
            status = coupon.get("couponStatus") or 0
            try:
                status = int(status)
            except (TypeError, ValueError):
                status = 0

            # textItem indices vary per coupon type (the reference reads [2]/[3] for
            # its log line), so keep every non-empty string in order instead of
            # betting on fixed positions and silently dropping the rest.
            values = [t.strip() for t in texts if isinstance(t, str) and t.strip()]

            coupons.append(
                PaybackCoupon(
                    coupon_id=str(coupon.get("couponID") or ""),
                    status=status,
                    activated=status != COUPON_STATUS_AVAILABLE,
                    valid_from=str(validity.get("validFrom") or ""),
                    valid_to=str(validity.get("validTo") or ""),
                    partner_name=partner_name,
                    title=values[0] if values else "",
                    description=" · ".join(values[1:])[:300],
                )
            )
        return coupons

    def activate_coupon(self, coupon_id: str) -> bool:
        """Activate one coupon. Raises on failure; True only on a real response."""
        from datetime import datetime

        payload = {
            **self._token_body(),
            "couponID": coupon_id,
            "force": False,
            "activatedAt": datetime.now().strftime("%Y-%m-%dT%H:%M:%S+0200"),
        }
        data = self._request(API_PATH_ACTIVATE_COUPON, payload)
        if data and not isinstance(data, dict):
            raise PaybackApiError(
                f"PAYBACK activatecoupon returned an unexpected body for {coupon_id}"
            )
        # An empty object is a normal success for this endpoint; a dict carrying
        # an explicit error is not.
        if isinstance(data, dict) and any(
            str(data.get(k) or "").strip()
            for k in ("error", "errorMessage", "message", "fault")
        ):
            raise PaybackApiError(
                f"PAYBACK refused activation of {coupon_id}: {str(data)[:200]}"
            )
        _LOGGER.debug("PAYBACK activated coupon %s", coupon_id)
        return True
