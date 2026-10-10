"""Config flow for PAYBACK Deutschland integration."""

from __future__ import annotations

from typing import Any

import voluptuous as vol
from homeassistant.config_entries import (
    ConfigEntry,
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlow,
)
from homeassistant.core import callback

from .api import DEFAULT_PRINCIPAL, PaybackAPIClient, PaybackApiError, PaybackAuthError
from .const import (
    CONF_ACCESS_TOKEN,
    CONF_AUTO_ACTIVATE_COUPONS,
    CONF_BASIC_AUTH_CREDENTIAL,
    CONF_BASIC_AUTH_USERNAME,
    CONF_PASSWORD,
    CONF_PRINCIPAL,
    CONF_SESSION_COOKIE,
    CONF_UPDATE_INTERVAL,
    CONF_USERNAME,
    DEFAULT_UPDATE_INTERVAL,
    DOMAIN,
    MIN_UPDATE_INTERVAL,
)


class PaybackConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle a config flow for PAYBACK Deutschland."""

    VERSION = 1

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Handle the initial user step."""
        errors: dict[str, str] = {}

        if user_input is not None:
            username = user_input[CONF_USERNAME].strip()
            password = user_input[CONF_PASSWORD].strip()

            await self.async_set_unique_id(username.lower())
            self._abort_if_unique_id_configured()

            # Validate the credentials. login() raises on failure now instead of
            # returning a flag that was True no matter what happened.
            auth: dict[str, Any] = {
                CONF_USERNAME: username,
                CONF_PASSWORD: password,
                CONF_SESSION_COOKIE: user_input.get(CONF_SESSION_COOKIE, "").strip(),
                CONF_PRINCIPAL: user_input.get(CONF_PRINCIPAL, "").strip(),
                CONF_BASIC_AUTH_USERNAME: user_input.get(
                    CONF_BASIC_AUTH_USERNAME, ""
                ).strip(),
                CONF_BASIC_AUTH_CREDENTIAL: user_input.get(
                    CONF_BASIC_AUTH_CREDENTIAL, ""
                ).strip(),
                CONF_ACCESS_TOKEN: user_input.get(CONF_ACCESS_TOKEN, "").strip(),
            }
            client = PaybackAPIClient(
                username=username,
                password=password,
                session_cookie=auth[CONF_SESSION_COOKIE] or None,
                principal=auth[CONF_PRINCIPAL] or None,
                basic_auth_username=auth[CONF_BASIC_AUTH_USERNAME] or None,
                basic_auth_credential=auth[CONF_BASIC_AUTH_CREDENTIAL] or None,
                access_token=auth[CONF_ACCESS_TOKEN] or None,
            )
            try:
                await self.hass.async_add_executor_job(client.login)
            except PaybackAuthError:
                errors["base"] = "auth_failed"
            except PaybackApiError:
                errors["base"] = "cannot_connect"
            else:
                return self.async_create_entry(title=f"PAYBACK ({username})", data=auth)

        schema = vol.Schema(
            {
                vol.Required(CONF_USERNAME): str,
                vol.Required(CONF_PASSWORD): str,
                vol.Optional(CONF_SESSION_COOKIE, default=""): str,
                vol.Optional(CONF_PRINCIPAL, default=DEFAULT_PRINCIPAL): str,
                vol.Optional(CONF_BASIC_AUTH_USERNAME, default=""): str,
                vol.Optional(CONF_BASIC_AUTH_CREDENTIAL, default=""): str,
                vol.Optional(CONF_ACCESS_TOKEN, default=""): str,
            }
        )

        return self.async_show_form(
            step_id="user",
            data_schema=schema,
            errors=errors,
        )

    @staticmethod
    @callback
    def async_get_options_flow(
        config_entry: ConfigEntry,
    ) -> OptionsFlow:
        """Get options flow handler."""
        return PaybackOptionsFlowHandler()


class PaybackOptionsFlowHandler(OptionsFlow):
    """Handle options flow for PAYBACK Deutschland."""

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Manage options."""
        if user_input is not None:
            return self.async_create_entry(title="", data=user_input)

        config = {**self.config_entry.data, **self.config_entry.options}

        schema = vol.Schema(
            {
                vol.Optional(
                    CONF_UPDATE_INTERVAL,
                    default=config.get(CONF_UPDATE_INTERVAL, DEFAULT_UPDATE_INTERVAL),
                ): vol.All(
                    vol.Coerce(int), vol.Range(min=MIN_UPDATE_INTERVAL, max=168)
                ),
                vol.Optional(
                    CONF_AUTO_ACTIVATE_COUPONS,
                    default=config.get(CONF_AUTO_ACTIVATE_COUPONS, False),
                ): bool,
                vol.Optional(
                    CONF_SESSION_COOKIE,
                    default=config.get(CONF_SESSION_COOKIE, ""),
                ): str,
                # Auth options live here too, so a rejected login can be fixed
                # without deleting and re-adding the integration.
                vol.Optional(
                    CONF_PRINCIPAL,
                    default=config.get(CONF_PRINCIPAL, DEFAULT_PRINCIPAL),
                ): str,
                vol.Optional(
                    CONF_BASIC_AUTH_USERNAME,
                    default=config.get(CONF_BASIC_AUTH_USERNAME, ""),
                ): str,
                vol.Optional(
                    CONF_BASIC_AUTH_CREDENTIAL,
                    default=config.get(CONF_BASIC_AUTH_CREDENTIAL, ""),
                ): str,
                vol.Optional(
                    CONF_ACCESS_TOKEN,
                    default=config.get(CONF_ACCESS_TOKEN, ""),
                ): str,
            }
        )

        return self.async_show_form(step_id="init", data_schema=schema)
