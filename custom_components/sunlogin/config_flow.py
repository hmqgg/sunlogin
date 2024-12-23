"""Configuration flows."""

import errno
import logging
import asyncio
import aiohttp
import time
import pyqrcode
import io
import base64
from importlib import import_module
from datetime import timedelta

import homeassistant.helpers.config_validation as cv
import homeassistant.helpers.entity_registry as er
import voluptuous as vol
from homeassistant import config_entries, core, exceptions
from homeassistant.const import (
    CONF_DEVICE_ID,
    CONF_DEVICES,
    CONF_ENTITIES,
    CONF_FRIENDLY_NAME,
    CONF_PLATFORM,
    CONF_SCAN_INTERVAL,
    CONF_USERNAME,
    CONF_PASSWORD,
    CONF_IP_ADDRESS,
    CONF_CODE,
)
from homeassistant.core import callback
from .sunlogin import SunLogin, async_guess_model, make_qrcode_base64_v2, device_filter, config_options
from .sunlogin_api import CloudAPI, CloudAPI_V2, change_cliend_id_by_seed, PlugAPI_V2_FAST
from .dns_api import change_dns_server
from .updater import (
    DEFAULT_UPDATE_INTERVAL,
    DEFAULT_POWER_CONSUMES_UPDATE_INTERVAL,
    DEFAULT_DNS_UPDATE_INTERVAL,
    DEFAULT_CONFIG_UPDATE_INTERVAL,
    DEFAULT_TOKEN_UPDATE_INTERVAL,
    DEFAULT_DEVICES_UPDATE_INTERVAL,
)
from .const import (
    CONF_SMARTPLUG,
    CONF_USER_INPUT,
    CONF_SMSCODE,
    CONF_LOGIN_METHOD,
    CONF_REMOTE_UPDATE_INTERVAL,
    CONF_LOCAL_UPDATE_INTERVAL,
    CONF_POWER_CONSUMES_UPDATE_INTERVAL,
    CONF_CONFIG_UPDATE_INTERVAL,
    CONF_TOKEN_UPDATE_INTERVAL,
    CONF_DEVICES_UPDATE_INTERVAL,
    CONF_ENABLE_DNS_INJECTOR,
    CONF_DNS_SERVER,
    CONF_DNS_UPDATE_INTERVAL,
    CONF_ENABLE_PROXY,
    CONF_PROXY_SERVER,
    CONF_ENABLE_DEVICES_UPDATE,
    DEFAULT_ENABLE_DEVICES_UPDATE,
    DEFAULT_ENABLE_DNS_INJECTOR,
    DEFAULT_DNS_SERVER,
    DEFAULT_ENABLE_PROXY,
    DEFAULT_PROXY_SERVER,

    CONF_DEVICE_IP_ADDRESS,
    CONF_DEVICE_SN,
    CONF_DEVICE_MODEL,
    CONF_DEVICE_NAME,
    SL_COORDINATOR,
    SL_DEVICES,
    DOMAIN,
)
#from .discovery import discover

_LOGGER = logging.getLogger(__name__)

ENTRIES_VERSION = 1

CONFIGURE_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_REMOTE_UPDATE_INTERVAL, default=DEFAULT_UPDATE_INTERVAL.remote.seconds): int,
        vol.Required(CONF_LOCAL_UPDATE_INTERVAL, default=DEFAULT_UPDATE_INTERVAL.local.seconds): int,
        vol.Required(CONF_POWER_CONSUMES_UPDATE_INTERVAL, default=DEFAULT_POWER_CONSUMES_UPDATE_INTERVAL.interval.seconds): int,
        vol.Required(CONF_CONFIG_UPDATE_INTERVAL, default=DEFAULT_CONFIG_UPDATE_INTERVAL.interval.seconds): int,
        vol.Required(CONF_TOKEN_UPDATE_INTERVAL, default=DEFAULT_TOKEN_UPDATE_INTERVAL.interval.seconds): int,
        vol.Required(CONF_ENABLE_DEVICES_UPDATE, default=DEFAULT_ENABLE_DEVICES_UPDATE): bool,
        vol.Required(CONF_DEVICES_UPDATE_INTERVAL, default=DEFAULT_DEVICES_UPDATE_INTERVAL.interval.seconds): int,
        vol.Required(CONF_ENABLE_DNS_INJECTOR, default=DEFAULT_ENABLE_DNS_INJECTOR): bool,
        vol.Required(CONF_DNS_SERVER, default=DEFAULT_DNS_SERVER): cv.string,
        vol.Required(CONF_DNS_UPDATE_INTERVAL, default=DEFAULT_DNS_UPDATE_INTERVAL.interval.seconds): int,
        vol.Required(CONF_ENABLE_PROXY, default=DEFAULT_ENABLE_PROXY): bool,
        vol.Required(CONF_PROXY_SERVER, default=DEFAULT_PROXY_SERVER): cv.string,
    }
)

LOCAL_SETUP_SCHEMA = vol.Schema(
    {    
        vol.Optional(CONF_IP_ADDRESS): cv.string,
    }
)

SMS_SETUP_SCHEMA = vol.Schema(
    {    
        vol.Optional(CONF_USERNAME): cv.string,
        vol.Optional(CONF_CODE): cv.string,
    }
)

PASSWORD_SETUP_SCHEMA = vol.Schema(
    {    
        vol.Optional(CONF_USERNAME): cv.string,
        vol.Optional(CONF_PASSWORD): cv.string,
    }
)

CLOUD_SETUP_SCHEMA = vol.Schema(
    {    
        vol.Optional(CONF_USERNAME): cv.string,
        vol.Optional(CONF_PASSWORD): cv.string,
        vol.Optional(CONF_SCAN_INTERVAL, default=60): int,
        vol.Required(CONF_LOGIN_METHOD, default="local"): vol.In(["local", "password", "sms"])
    }
)

def schema_defaults(schema, dps_list=None, **defaults):
    """Create a new schema with default values filled in."""
    copy = schema.extend({})
    for field, field_type in copy.schema.items():
        if isinstance(field_type, vol.In):
            value = None
            for dps in dps_list or []:
                if dps.startswith(f"{defaults.get(field)} "):
                    value = dps
                    break

            if value in field_type.container:
                field.default = vol.default_factory(value)
                continue

        if field.schema in defaults:
            field.default = vol.default_factory(defaults[field])
    return copy

# def make_qrcode_img(qrdata):
#     if qrdata is not None:
#         buffer = io.BytesIO()
#         url = pyqrcode.create(qrdata)
#         url.png(buffer, scale=5, module_color="#000", background="#FFF")
#         image_base64 = str(base64.b64encode(buffer.getvalue()), encoding='utf-8')
#         image = f'![image](data:image/png;base64,{image_base64})'
#         return image
#     return

async def attempt_connection(sunlogin, method = 1, *args):
    """Create device."""
    if method == 1:
        res = await sunlogin.async_get_access_token_by_password(*args)
    elif method == 2:
        res = await sunlogin.async_get_access_token_by_sms(*args)
    elif method == 3:
        res = await sunlogin.async_get_access_token_by_qrcode(*args)
    if res != "ok":
        _LOGGER.error("Cloud API connection failed: %s", res)
        return {"reason": "authentication_failed", "msg": res}

    res = await sunlogin.async_get_devices_list()
    if res != "ok":
        _LOGGER.error("Cloud API get_devices_list failed: %s", res)
        return {"reason": "device_list_failed", "msg": res}

    _LOGGER.info("Cloud API connection succeeded.")

    return {}


class SunLoginConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle a config flow for SunLogin integration."""

    VERSION = ENTRIES_VERSION
    QRImage = ''

    @staticmethod
    @callback
    def async_get_options_flow(config_entry):
        """Get options flow for this handler."""
        return SunLoginOptionsFlowHandler(config_entry)

    def __init__(self):
        """Initialize a new SunLoginConfigFlow."""
        self.qrdata = {}
        self.qrstep = 0
        self.qrtask = None
        self.api_v2 = None
        self.sunlogin = None
        self.reauth_entry_id = None

    async def async_step_reauth(self, data):
        """Handle configuration by re-auth."""

        self.reauth_entry_id = self.context["entry_id"]
        self.api_v2 = CloudAPI_V2(self.hass)
        self.sunlogin = SunLogin(self.hass)

        return self.async_show_menu(
            step_id="reauth",
            menu_options=[
                "password",
                "sms",
                "qrcode"
            ]
        )

    async def async_step_user(self, user_input=None):
        """Handle the initial step."""
        errors = {}
        placeholders = {}
        self.api_v2 = CloudAPI_V2(self.hass)
        self.sunlogin = SunLogin(self.hass)

        if user_input is not None:
            pass
        
        return self.async_show_menu(
            step_id="user",
            menu_options=[
                "local",
                "password",
                "sms",
                "qrcode"
            ]
        )

    async def async_step_local(self, user_input=None):
        errors = {}

        if user_input is not None:
            return await self._create_entry_by_ip(user_input)
        return self.async_show_form(
            step_id="local",
            data_schema=LOCAL_SETUP_SCHEMA,
            errors=errors,
        )

    async def async_step_password(self, user_input=None):
        errors = {}
        placeholders = {"msg": ''}

        if user_input is not None:
            if user_input.get(CONF_USERNAME) == 'scry':
                return await self._create_entry(user_input)

            res = await attempt_connection(self.sunlogin, 1, user_input.get(CONF_USERNAME), user_input.get(CONF_PASSWORD))

            if not res:
                return await self._create_entry(user_input)
            errors["base"] = res["reason"]
            placeholders = {"msg": res["msg"]}

        return self.async_show_form(
            step_id="password",
            data_schema=PASSWORD_SETUP_SCHEMA,
            errors=errors,
            description_placeholders=placeholders,
        )

    async def async_step_sms(self, user_input=None):
        errors = {}
        placeholders = {"msg": ''}

        if user_input is not None:
            res = await attempt_connection(self.sunlogin, 2, user_input.get(CONF_USERNAME), user_input.get(CONF_CODE))

            if not res:
                return await self._create_entry(user_input)
            errors["base"] = res["reason"]
            placeholders = {"msg": res["msg"]}

        return self.async_show_form(
            step_id="sms",
            data_schema=SMS_SETUP_SCHEMA,
            errors=errors,
            description_placeholders=placeholders,
        )

    async def async_step_qrcode(self, user_input=None):
        if not self.qrtask:
            if self.qrstep == 0:
                _LOGGER.debug("Generating QR code...")
                self.qrtask = self.hass.async_create_task(self.make_qrcode_img())
                return self.async_show_progress(
                    step_id="change_ui",
                    progress_action="qrcode1",
                )
            elif self.qrstep == 1:
                _LOGGER.debug("Scanning QR code...")
                self.qrtask = self.hass.async_create_task(self.check_qrcode_status())
                return self.async_show_progress(
                    step_id="change_ui",
                    progress_action="qrcode2",
                    description_placeholders={"msg": self.qrdata.get('image')}
                )
            elif self.qrstep == 2:
                _LOGGER.debug("Finishing QR code...")
                self.sunlogin.hass = self.hass
                self.qrtask = self.hass.async_create_task(self.qrcode_finish())
                return self.async_show_progress(
                    step_id="change_ui",
                    progress_action="qrcode3",
                    description_placeholders={"msg": ''}
                )
            elif self.qrstep == 3:
                _LOGGER.debug("Creating entry...")
                self.qrstep = 0
                return await self._create_entry({})
            
        self.qrtask = None
        return self.async_show_progress_done(next_step_id="qrcode")
    
    async def async_step_change_ui(self, user_input=None):
        if self.qrtask is None:
            return self.async_show_progress_done(next_step_id="qrcode")
        try:
            await self.qrtask
        except asyncio.TimeoutError:
            self.qrtask = None
            # return self.async_show_progress_done(next_step_id="pairing_timeout")

        self.qrtask = None
        return self.async_show_progress_done(next_step_id="qrcode")

    async def make_qrcode_img(self):
        resp =  await self.api_v2.async_get_qrdata()
        if not resp.ok:
            # raise error
            pass
        r_json = resp.json()
        qrdata = make_qrcode_base64_v2(r_json)
        if qrdata is not None:
            self.qrdata = qrdata
            self.qrstep += 1
        self.hass.async_create_task(
            self.hass.config_entries.flow.async_configure(flow_id=self.flow_id)
        )

    async def check_qrcode_status(self):
        failed_count = 0
        key = self.qrdata.get('key')
        # await asyncio.sleep(2)
        tick_count = 0
        while failed_count < 3:
            await asyncio.sleep(3)
            tick_count += 1
            if tick_count * 3 > 170:
                failed_count = 3
                break
            try:
                resp = await self.api_v2.async_get_qrstatus(key)
            except:
                failed_count += 1
                continue
            if not resp.ok:
                failed_count += 1

            r_json = resp.json()
            try:
                status = r_json['status']
            except:
                failed_count += 2
                continue
            
            if status == 2:
                self.qrstep += 1
                self.qrdata['secret'] = r_json.get('secret')
                break
            

        if failed_count >= 3:
            #raise error
            self.qrstep -= 1
        
        self.hass.async_create_task(
            self.hass.config_entries.flow.async_configure(flow_id=self.flow_id)
        )

    async def qrcode_finish(self):
        res = await attempt_connection(self.sunlogin, 3, self.qrdata.get('secret'))

        self.qrstep += 1
        self.hass.async_create_task(
            self.hass.config_entries.flow.async_configure(flow_id=self.flow_id)
        )

    async def _create_entry(self, user_input):
        """Register new entry."""
        # if self._async_current_entries():
        #     return self.async_abort(reason="already_configured")
        unique_id = self.sunlogin.userid if self.sunlogin.userid is not None else user_input.get(CONF_USERNAME)
        await self.async_set_unique_id(str(unique_id))

        devices = device_filter(self.sunlogin.device_list)
        
        entry = {
            CONF_USER_INPUT: user_input, 
            CONF_DEVICES: devices, 
            **self.sunlogin.token.config
        }

        if self.reauth_entry_id is not None:
            reauth_entry = self.hass.config_entries.async_get_entry(self.reauth_entry_id)
            data = {**reauth_entry.data}
            data.update(self.sunlogin.token.config)
            self.hass.config_entries.async_update_entry(reauth_entry, data=data)
            self.hass.async_create_task(
                self.hass.config_entries.async_reload(self.reauth_entry_id)
            )
            return self.async_abort(reason="reauth_successful")
        
        return self.async_create_entry(
            title=unique_id,
            data=entry,
        )
    
    async def _create_entry_by_ip(self, user_input):
        """Register new entry."""
        # if self._async_current_entries():
        #     return self.async_abort(reason="already_configured")

        await self.async_set_unique_id(user_input.get(CONF_IP_ADDRESS))
        sn, model = await async_guess_model(self.hass, user_input.get(CONF_IP_ADDRESS))
        device_name = "{model}({sn})".format(model=model, sn=sn[:4])
        device_conf = {CONF_DEVICE_IP_ADDRESS: user_input.get(CONF_IP_ADDRESS).strip(), CONF_DEVICE_MODEL: model, CONF_DEVICE_NAME: device_name}
        if sn is not None:
            device_conf.update({CONF_DEVICE_SN: sn})
            devices = {sn: device_conf}
        else:
            devices = {"__sn__": device_conf}
        
        entry = {CONF_USER_INPUT: user_input, CONF_DEVICES: devices}
        return self.async_create_entry(
            title=user_input.get(CONF_IP_ADDRESS),
            data=entry,
        )
        
    async def async_step_import(self, user_input):
        """Handle import from YAML."""
        _LOGGER.error(
            "Configuration via YAML file is no longer supported by this integration."
        )


class SunLoginOptionsFlowHandler(config_entries.OptionsFlow):
    """Handle options flow for SunLogin integration."""

    def __init__(self, config_entry):
        """Initialize sunlogin options flow."""
        self.config_entry = config_entry
        # _LOGGER.debug(config_entry.entry_id)

    async def async_step_init(self, user_input=None):
        """Manage basic options."""
        # device_id = self.config_entry.data[CONF_DEVICE_ID]
        defaults = dict()
        defaults.update(self.config_entry.options)

        if user_input is not None:
            data = defaults.copy()
            diff = dict(set(user_input.items()) - set(data.items()))
            options = config_options(self.hass, self.config_entry, diff)
            data.update(options)
            
            return self.async_create_entry(title="", data=data)

        return self.async_show_form(
            step_id="init",
            data_schema=schema_defaults(CONFIGURE_SCHEMA, **defaults),
        )


    async def async_step_add_device(self, user_input=None):
        scan_interval = user_input.get(CONF_SCAN_INTERVAL)
        _LOGGER.debug("scan_interval: ", scan_interval)


    async def async_step_yaml_import(self, user_input=None):
        """Manage YAML imports."""
        _LOGGER.error(
            "Configuration via YAML file is no longer supported by this integration."
        )
        # if user_input is not None:
        #     return self.async_create_entry(title="", data={})
        # return self.async_show_form(step_id="yaml_import")

    @property
    def current_entity(self):
        """Existing configuration for entity currently being edited."""
        return self.entities[len(self.device_data[CONF_ENTITIES])]


class CannotConnect(exceptions.HomeAssistantError):
    """Error to indicate we cannot connect."""


class InvalidAuth(exceptions.HomeAssistantError):
    """Error to indicate there is invalid auth."""


class EmptyDpsList(exceptions.HomeAssistantError):
    """Error to indicate no datapoints found."""
