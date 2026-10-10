"""Offline tests for the PAYBACK API client.

No network and no real credentials: the session is replaced with a stub, so these
pin down the behaviours that were broken in 1.0.0 and the response mapping taken
from the reference implementation.

Run: python tests/test_api_offline.py     (also works under pytest)
"""

from __future__ import annotations

import importlib.util
import json
import pathlib
import sys
from typing import Any

API_PATH = (
    pathlib.Path(__file__).resolve().parent.parent
    / "custom_components"
    / "payback"
    / "api.py"
)
spec = importlib.util.spec_from_file_location("payback_api", API_PATH)
assert spec is not None and spec.loader is not None
api = importlib.util.module_from_spec(spec)
spec.loader.exec_module(api)


class StubResponse:
    """Minimal stand-in for a curl_cffi response."""

    def __init__(self, status_code: int, body: str = "", content_type: str = "application/json"):
        self.status_code = status_code
        self._body = body
        self.text = body
        self.content = body.encode()
        self.headers = {"Content-Type": content_type}

    def json(self) -> Any:
        return json.loads(self._body)


class StubSession:
    """Records requests, always answers with the same response."""

    def __init__(self, response: StubResponse | list[StubResponse]):
        self.responses = response if isinstance(response, list) else [response]
        self.calls: list[dict[str, Any]] = []

    def post(self, url: str, **kwargs: Any) -> StubResponse:
        self.calls.append({"url": url, **kwargs})
        if len(self.responses) > 1:
            return self.responses.pop(0)
        return self.responses[0]


def make_client(response: StubResponse | list[StubResponse], **kwargs: Any) -> Any:
    """A client that talks to a stub instead of the network."""
    kwargs.setdefault("basic_auth_username", "iosapp_de_23a")
    kwargs.setdefault("basic_auth_credential", "a-real-looking-secret")
    client = api.PaybackAPIClient("1234567890", "hunter2", **kwargs)
    client.session = StubSession(response)
    return client


# ── regressions from 1.0.0 ──────────────────────────────────────────────────
def test_missing_app_credentials_fails_loudly() -> None:
    """1.0.0 returned True from login() unconditionally."""
    client = api.PaybackAPIClient("1234567890", "hunter2")
    try:
        client.login()
    except api.PaybackAuthError as exc:
        assert "app credentials" in str(exc).lower()
    else:
        raise AssertionError("login() must not succeed without app credentials")


def test_placeholder_credential_is_refused() -> None:
    """The example value shipped in the reference template must not be used."""
    client = api.PaybackAPIClient(
        "1234567890",
        "hunter2",
        basic_auth_username="iosapp_de_23a",
        basic_auth_credential="LKAJSDkaskdjskds",
    )
    try:
        client.login()
    except api.PaybackAuthError:
        return
    raise AssertionError("placeholder credential should be rejected before any request")


def test_unauthorized_maps_to_auth_error() -> None:
    client = make_client(StubResponse(401, "401 Authorization Required"))
    try:
        client.login()
    except api.PaybackAuthError:
        return
    raise AssertionError("HTTP 401 must raise PaybackAuthError")


def test_http_error_does_not_produce_an_empty_account() -> None:
    """1.0.0 fabricated a 0-point account, which is what hid the failure."""
    client = make_client(StubResponse(404, "404 not found"))
    try:
        client.get_account()
    except api.PaybackApiError as exc:
        assert "404" in str(exc)
    else:
        raise AssertionError("get_account() must raise instead of inventing data")


# ── endpoints and mapping ───────────────────────────────────────────────────
def test_uses_the_real_api_endpoint() -> None:
    client = make_client(StubResponse(200, "{}"))
    client._authentication = {"token": "tok"}
    client.get_coupons()
    assert (
        client.session.calls[0]["url"]
        == "https://services-ext.payback.de/139/v1/json/getcoupons"
    ), client.session.calls[0]["url"]


def test_no_coupons_is_not_an_error() -> None:
    client = make_client(StubResponse(200, "{}"))
    client._authentication = {"token": "tok"}
    assert client.get_coupons() == []


def test_schema_drift_raises_instead_of_returning_empty() -> None:
    client = make_client(StubResponse(200, json.dumps({"data": {"moved": True}})))
    client._authentication = {"token": "tok"}
    try:
        client.get_coupons()
    except api.PaybackApiError as exc:
        assert "couponListItem" in str(exc)
    else:
        raise AssertionError("unexpected schema must raise")


def test_maps_the_verified_coupon_schema() -> None:
    payload = {
        "couponListItem": [
            {
                "coupon": {
                    "couponID": "497462",
                    "couponStatus": 1,
                    "partner": [{"partnerDisplayName": "REWE"}],
                    "validity": {
                        "validFrom": "2026-10-01T00:00:00+0200",
                        "validTo": "2026-10-31T00:00:00+0200",
                    },
                    "couponContentSet": {
                        "textItem": [
                            {"textValue": "10x Punkte"},
                            {"textValue": "auf Obst & Gemüse"},
                        ]
                    },
                }
            },
            {
                "coupon": {
                    "couponID": "111",
                    "couponStatus": 2,
                    "partner": [],
                    "validity": {},
                    "couponContentSet": {"textItem": [{"textValue": "Already on"}]},
                }
            },
        ]
    }
    client = make_client(StubResponse(200, json.dumps(payload)))
    client._authentication = {"token": "tok"}
    coupons = client.get_coupons()

    assert len(coupons) == 2
    first = coupons[0]
    assert first.coupon_id == "497462"
    assert first.partner_name == "REWE"
    assert first.title == "10x Punkte"
    assert first.description == "auf Obst & Gemüse"
    assert first.valid_to == "2026-10-31T00:00:00+0200"
    assert first.status == 1 and first.activated is False
    assert coupons[1].activated is True and coupons[1].status == 2


def test_activate_coupon_posts_the_coupon_id() -> None:
    client = make_client(StubResponse(200, "{}"))
    client._authentication = {"token": "tok"}
    assert client.activate_coupon("497462") is True
    call = client.session.calls[0]
    assert call["url"].endswith("/json/activatecoupon")
    assert call["json"]["couponID"] == "497462"
    assert call["json"]["authentication"] == {"token": "tok"}


def test_activate_coupon_surfaces_an_api_error() -> None:
    client = make_client(StubResponse(200, json.dumps({"errorMessage": "coupon gone"})))
    client._authentication = {"token": "tok"}
    try:
        client.activate_coupon("497462")
    except api.PaybackApiError as exc:
        assert "497462" in str(exc)
    else:
        raise AssertionError("an error body must not be reported as success")


# ── the cookie / token route ────────────────────────────────────────────────
def test_access_token_skips_login() -> None:
    """The route that needs no app credentials and no reCAPTCHA login."""
    client = api.PaybackAPIClient("", "", access_token="tok-from-browser")
    client.session = StubSession(StubResponse(200, "{}"))
    client._token_body()
    assert client.session.calls == []


def test_browser_session_cookie_is_loaded() -> None:
    client = api.PaybackAPIClient(
        "1234567890",
        "hunter2",
        basic_auth_username="u",
        basic_auth_credential="c",
        session_cookie="incap_ses_123=abc; visid_incap_1=xyz",
    )
    names = {cookie.name for cookie in client.session.cookies.jar}
    assert {"incap_ses_123", "visid_incap_1"} <= names


def _main() -> int:
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    failed = 0
    for test in tests:
        try:
            test()
        except Exception as exc:  # noqa: BLE001 - test runner
            failed += 1
            print(f"  FAIL  {test.__name__}: {type(exc).__name__}: {exc}")
        else:
            print(f"  pass  {test.__name__}")
    print(f"\n  {len(tests) - failed} passed, {failed} failed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(_main())
