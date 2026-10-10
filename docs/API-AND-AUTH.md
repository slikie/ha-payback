# PAYBACK API and authentication

Notes for anyone working on this integration. Everything under "Verified" was
checked against live hosts or a reverse-engineered reference implementation;
everything under "Unverified" is a hypothesis that still needs a real response.

## Why version 1.0.0 never worked

Three independent defects, the last two of which hid the first:

1. **Wrong API.** It called `https://www.payback.de/pb/v1/sessions` and
   `.../pb/v1/account`. Both return **HTTP 404**, confirmed from two unrelated
   networks. `https://www.payback.de/pb/v1/` answers **301** — it is a web route,
   not an API namespace. `api.payback.de` does not resolve at all.
2. **`login()` could not fail.** Both the success path and the `except` path
   returned `True`, so a rejected login was indistinguishable from a good one.
3. **`get_account()` invented data.** On any error it returned an account with
   `customerName="Payback Customer (<card number>)"`, zero points and no coupons.
   That placeholder is exactly what the sensors published.

The coordinator's own error handling (back-off, repair issue, `UpdateFailed`) was
correct all along — it simply never received an exception, so HA reported
`success: True` while every sensor stayed at zero.

## The real API

All calls are `POST` to `https://services-ext.payback.de/<principal>/v1/json/<action>`:

| Action | Purpose |
| --- | --- |
| `secureauthenticate` | Log in, returns an `authentication` object with a `token` |
| `getcoupons` | Coupon list for a period |
| `getaccountbalance` | Points balance |
| `activatecoupon` | Activate one coupon |

Facts established by probing:

* `.../json/getcoupons` returns **401 Authorization Required**. The host and path
  exist and want credentials — that 401 (not a 404) is the evidence for this shape.
* `principal` (138/139 in the reference) appears **both** in the URL and inside
  every request body under `consumerIdentification`.

Payload shapes and endpoint names come from the reverse-engineered reference
`theodm/python-coupons` (`src/payback/PaybackAPI.py`). Coupon responses look like:

```
{"couponListItem": [{"coupon": {
    "couponID": "497462",
    "couponStatus": 1,                     # 1 available, 2 activated, 3/4 used
    "partner": [{"partnerDisplayName": "REWE"}],
    "validity": {"validFrom": "...", "validTo": "..."},
    "couponContentSet": {"textItem": [{"textValue": "10x Punkte"}, ...]}
}}]}
```

The 1.0.0 models did not match this at all — `couponId` vs `couponID`, and
`title`/`description`/`partnerName`/`validUntil` are not fields of the real
schema (they only ever existed because the models were fed fabricated data).

## Authentication: three routes

The host demands **HTTP Basic auth with credentials extracted from the official
mobile app**. The reference's own `secrets.properties` says so explicitly:

> Die APIs benötigen beide ein spezielles API-Token, welches nur der Client kennt.
> Diese können mittels Reverse-Engineering der Android-Apps oder der iOS-Apps
> herausgefunden und extrahiert werden.

That is a hard dependency for route 1, and it is why every tool in this space
breaks whenever PAYBACK rotates the credential.

### Route 1 — app credentials + login

Supply `basic_auth_username`, `basic_auth_credential` and `principal`, plus the
account's card number/e-mail and password. The client calls
`secureauthenticate` and keeps the returned token.

* Needs reverse-engineered app credentials.
* PAYBACK gates login behind **reCAPTCHA**, so an automated login can be blocked
  even with correct credentials.
* `aliasType` is `1` for a card number and `5` for an e-mail address; the client
  picks automatically.

### Route 2 — reuse an access token (recommended)

Pass `access_token` and the client skips `secureauthenticate` entirely. No app
credentials, no captcha, no password stored. Capture the token from an
already-authenticated session and refresh it when the API starts returning 401.

### Route 3 — reuse the browser session cookie

Pass `session_cookie` (e.g. `incap_ses_...=...; visid_incap_1=...`) and the client
loads it into the jar. Useful when the WAF challenges the API host and a real
browser session is already established.

Routes 2 and 3 avoid the app reverse-engineering problem and the captcha problem,
which is why they are the preferred direction.

## Verified vs unverified

**Verified:** endpoint host and paths (401 vs 404 probes); endpoint and payload
names (reference implementation); the coupon response schema and status codes;
the app-credential requirement; that `www.payback.de/pb/v1/*` is not an API.

**Unverified:** the **response schema of `getaccountbalance`** — the reference
returns it raw without reading a single field. `_parse_points()` therefore accepts
several plausible spellings and returns 0 when nothing matches, logging what keys
it did see. Before anyone trusts the points sensors, capture one live response and
pin the names down.

## Testing

`tests/test_api_offline.py` runs with no network and no credentials — the session
is stubbed — and covers the three 1.0.0 regressions, the endpoint, the coupon
mapping and the token/cookie routes:

```bash
python tests/test_api_offline.py     # or: pytest tests/
```

Anything that talks to the real API must be checked manually, because it needs a
valid credential.

## Caveats

* Automating coupon activation is against the spirit of PAYBACK's terms and may
  get an account flagged. Keep polling gentle — the coordinator's jitter and
  back-off exist for that reason.
* Never commit credentials, tokens or session cookies. They belong in the Home
  Assistant config entry, not in the repository.
