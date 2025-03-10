from __future__ import annotations

import asyncio
import logging

import requests
from requests.auth import HTTPBasicAuth

import voluptuous as vol
from haffmpeg.camera import CameraMjpeg
from haffmpeg.tools import IMAGE_JPEG, ImageFrame
from hass_nabucasa.voice import MAP_VOICE, Gender
from homeassistant.components import mqtt
from homeassistant.components.camera import (Camera, CameraEntityFeature)
from homeassistant.components.ffmpeg import CONF_EXTRA_ARGUMENTS, DATA_FFMPEG
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import (CONF_HOST, CONF_MAC, CONF_NAME, CONF_PASSWORD,
                                 CONF_PORT, CONF_USERNAME, STATE_OFF, STATE_ON)
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import entity_platform
from homeassistant.helpers.aiohttp_client import async_aiohttp_proxy_stream
from homeassistant.helpers.device_registry import CONNECTION_NETWORK_MAC

from .const import (ALLWINNER, ALLWINNERV2, CONF_BOOST_SPEAKER, CONF_HACK_NAME,
                    CONF_MQTT_PREFIX, CONF_PTZ,
                    CONF_TOPIC_MOTION_DETECTION_IMAGE, DEFAULT_BRAND, DOMAIN,
                    HTTP_TIMEOUT, LINK_HIGH_RES_STREAM, LINK_LOW_RES_STREAM,
                    MSTAR, SERVICE_MOVE_TO_PRESET, SERVICE_PTZ,
                    SERVICE_REBOOT, SERVICE_SPEAK)

_LOGGER = logging.getLogger(__name__)

DIR_UP = "up"
DIR_DOWN = "down"
DIR_LEFT = "left"
DIR_RIGHT = "right"
ATTR_MOVEMENT = "movement"
ATTR_TRAVELTIME = "travel_time"
DEFAULT_TRAVELTIME = 0.3

LANG_DE = "de-DE"
LANG_GB = "en-GB"
LANG_US = "en-US"
LANG_ES = "es-ES"
LANG_FR = "fr-FR"
LANG_IT = "it-IT"
ATTR_LANGUAGE = "language"
ATTR_GENDER = "gender"
ATTR_SENTENCE = "sentence"
DEFAULT_LANGUAGE = "en-US"
DEFAULT_SENTENCE = ""

ICON = "mdi:camera"

async def async_setup_entry(hass: HomeAssistant, config: ConfigEntry, async_add_entities):
    """Set up a Yi Camera."""

    platform = entity_platform.current_platform.get()
    platform.async_register_entity_service(
        SERVICE_PTZ,
        {
            vol.Required(ATTR_MOVEMENT): vol.In(
                [
                    DIR_UP,
                    DIR_DOWN,
                    DIR_LEFT,
                    DIR_RIGHT,
                ]
            ),
            vol.Optional(ATTR_TRAVELTIME, default=DEFAULT_TRAVELTIME): vol.Coerce(float),
        },
        "async_perform_ptz",
    )

    platform.async_register_entity_service(
        SERVICE_MOVE_TO_PRESET,
        {
            vol.Required("preset_id"): vol.All(int, vol.Range(min=0, max=14)),
        },
        "async_perform_move_to_preset",
    )

    if (config.data[CONF_HACK_NAME] == MSTAR) or (config.data[CONF_HACK_NAME] == ALLWINNER) or (config.data[CONF_HACK_NAME] == ALLWINNERV2):
        platform.async_register_entity_service(
            SERVICE_SPEAK,
            {
                vol.Required(ATTR_LANGUAGE, default=DEFAULT_LANGUAGE): vol.In(
                    {lang for lang, *_rest in MAP_VOICE}
                ),
                vol.Required(ATTR_GENDER, default=Gender.FEMALE): vol.In(
                    [Gender.MALE, Gender.FEMALE]
                ),
                vol.Required(ATTR_SENTENCE, default=DEFAULT_SENTENCE): str,
            },
            "async_perform_speak",
        )

    platform.async_register_entity_service(
        SERVICE_REBOOT,
        {},
        "async_perform_reboot",
    )

    async_add_entities(
        [
            YiHackCamera(hass, config),
            YiHackMqttCamera(hass, config)
        ],
        True
    )

class YiHackCamera(Camera):
    """Define an implementation of a Yi Camera."""

    def __init__(self, hass, config):
        """Initialize."""
        super().__init__()

        self._extra_arguments = config.data[CONF_EXTRA_ARGUMENTS]
        self._manager = hass.data[DATA_FFMPEG]
        self._device_name = config.data[CONF_NAME]
        self._name = self._device_name + " " + "Cam"
        self._unique_id = self._device_name + "_caca"
        self._mac = config.data[CONF_MAC]
        self._host = config.data[CONF_HOST]
        self._port = config.data[CONF_PORT]
        self._user = config.data[CONF_USERNAME]
        self._password = config.data[CONF_PASSWORD]
        self._hack_name = config.data[CONF_HACK_NAME]
        self._ptz = config.data[CONF_PTZ]
        self._mqtt_subscription = None
        self._mqtt_cmnd_topic = config.data[CONF_MQTT_PREFIX] + "/cmnd/camera/switch_on"
        self._mqtt_stat_topic = config.data[CONF_MQTT_PREFIX] + "/stat/camera/switch_on"
        self._state = True

        self._http_base_url = "http://" + self._host
        if self._port != 80:
            self._http_base_url += ":" + str(self._port)
        self._still_image_url = self._http_base_url + "/cgi-bin/snapshot.sh?res=high&watermark=yes"

        try:
            self._boost_speaker = config.data[CONF_BOOST_SPEAKER]
        except KeyError:
            self._boost_speaker = "auto"

    async def async_added_to_hass(self):
        """Subscribe to MQTT events."""

        @callback
        def message_received(msg):
            """Handle new MQTT messages."""
            try:
                payload = msg.payload.decode("utf-8", "ignore")
            except:
                payload = msg.payload

            if payload in ["yes", "on"]:
                self._state = True
            elif payload in ["no", "off"]:
                self._state = False
            else:  # Payload is not correct for this entity
                _LOGGER.info(
                    "No matching payload found for entity %s with topic: %s. Payload: '%s'",
                    self._name,
                    self._mqtt_stat_topic,
                    payload,
                )
                return

            self.async_write_ha_state()

        self._mqtt_subscription = await mqtt.async_subscribe(
            self.hass, self._mqtt_stat_topic, message_received, 1, None
        )

    async def async_will_remove_from_hass(self):
        """Unsubscribe from MQTT events."""
        if self._mqtt_subscription:
            self._mqtt_subscription()

    @property
    def supported_features(self) -> CameraEntityFeature:
        """Return supported features."""
        return CameraEntityFeature.STREAM | CameraEntityFeature.ON_OFF

    async def async_turn_off(self):
        """Turn off camera"""
        self.hass.async_create_task(
            mqtt.async_publish(self.hass, self._mqtt_cmnd_topic, "no", 1, 0)
        )
        self._state = False

    async def async_turn_on(self):
        """Turn on camera"""
        self.hass.async_create_task(
            mqtt.async_publish(self.hass, self._mqtt_cmnd_topic, "yes", 1, 0)
        )
        self._state = True

    async def stream_source(self) -> str:
        """Return the stream source."""
        def fetch_link():
            """Get URL from camera available links."""
            auth = None
            if self._user or self._password:
                auth = HTTPBasicAuth(self._user, self._password)

            try:
                response = requests.get(self._http_base_url + "/cgi-bin/links.sh",
                                        timeout=HTTP_TIMEOUT, auth=auth)
                if response.status_code < 300:
                    links: dict = response.json()
                    stream_source: str = links.get(LINK_HIGH_RES_STREAM) or links.get(LINK_LOW_RES_STREAM)
                    if self._user or self._password:
                        stream_source = stream_source.replace(
                            "rtsp://", f"rtsp://{self._user}:{self._password}@", 1
                        )

                    return stream_source

            except requests.exceptions.RequestException as error:
                _LOGGER.error(
                    "Error getting stream link from %s: %s",
                    self._name,
                    error,
                )

            return None

        return await self.hass.async_add_executor_job(fetch_link)

    async def async_camera_image(
        self, width: int | None = None, height: int | None = None
    ) -> bytes | None:
        """Return a still image response from the camera."""
        """Ignore width and height when the image is fetched from url."""
        """Camera component will resize it."""
        image = None

        if self._still_image_url:
            auth = None
            if self._user or self._password:
                auth = HTTPBasicAuth(self._user, self._password)

            def fetch():
                """Read image from a URL."""
                try:
                    response = requests.get(self._still_image_url, timeout=HTTP_TIMEOUT, auth=auth)
                    if response.status_code < 300:
                        return response.content
                except requests.exceptions.RequestException as error:
                    _LOGGER.error(
                        "Fetch snapshot image failed from %s, falling back to FFmpeg; %s",
                        self._name,
                        error,
                    )

                return None

            image = await self.hass.async_add_executor_job(fetch)
            if image is None:
                await asyncio.sleep(1)
                image = await self.hass.async_add_executor_job(fetch)
            if image is None:
                await asyncio.sleep(1)
                image = await self.hass.async_add_executor_job(fetch)

        if image is None:
            stream_source = await self.stream_source()
            if stream_source:
                ffmpeg = ImageFrame(self.hass.data[DATA_FFMPEG].binary)
                image = await asyncio.shield(
                    ffmpeg.get_image(
                        stream_source,
                        output_format=IMAGE_JPEG,
                        extra_cmd=self._extra_arguments
                    )
                )

        return image

    async def handle_async_mjpeg_stream(self, request):
        """Generate an HTTP MJPEG stream from the camera."""
        _LOGGER.debug("Handling mjpeg stream from camera '%s'", self._name)

        stream_source = await self.stream_source()
        if not stream_source:
            return super().handle_async_mjpeg_stream(request)

        stream = CameraMjpeg(self._manager.binary)
        await stream.open_camera(
            stream_source,
            extra_cmd=self._extra_arguments
        )

        try:
            stream_reader = await stream.get_reader()
            return await async_aiohttp_proxy_stream(
                self.hass,
                request,
                stream_reader,
                self._manager.ffmpeg_stream_content_type,
            )
        finally:
            await stream.close()

    def _perform_ptz(self, movement, travel_time_str):
        auth = None
        if self._user or self._password:
            auth = HTTPBasicAuth(self._user, self._password)

        try:
            response = requests.get("http://" + self._host + ":" + str(self._port) + "/cgi-bin/ptz.sh?dir=" + movement + "&time=" + travel_time_str, timeout=HTTP_TIMEOUT, auth=auth)
            if response.status_code >= 300:
                _LOGGER.error("Failed to send ptz command to device %s", self._host)
        except requests.exceptions.RequestException as error:
            _LOGGER.error("Failed to send ptz command to device %s: error %s", self._host, error)

    async def async_perform_ptz(self, movement, travel_time):
        """Perform a PTZ action on the camera."""
        _LOGGER.debug("PTZ action '%s' on %s", movement, self._name)

        if self._ptz == "no":
            _LOGGER.error("PTZ is not available on %s", self._name)
            return

        try:
            travel_time_str = str(travel_time)
        except ValueError:
            travel_time_str = str(DEFAULT_TRAVELTIME)

        await self.hass.async_add_executor_job(self._perform_ptz, movement, travel_time_str)

    def _perform_move_to_preset(self, preset_id):
        auth = None
        if self._user or self._password:
            auth = HTTPBasicAuth(self._user, self._password)

        try:
            response = requests.get(f"http://{self._host}:{self._port}/cgi-bin/preset.sh?action=go_preset&num={preset_id}", timeout=HTTP_TIMEOUT, auth=auth)
            if response.status_code >= 300:
                _LOGGER.error(f"Failed to send go to preset command to device {self._host}")
        except requests.exceptions.RequestException as error:
            _LOGGER.error(f"Failed to send go to preset command to device {self._host}: error {error}")

    async def async_perform_move_to_preset(self, preset_id):
        """Aim the camera at the given preset."""
        _LOGGER.debug(f"Move to preset {preset_id} on {self._name}")

        await self.hass.async_add_executor_job(self._perform_move_to_preset, preset_id)

    async def async_perform_speak(self, language: str, gender: Gender, sentence: str):
        """Perform a SPEAK action on the camera."""
        _LOGGER.debug("SPEAK action on %s", self._name)

        audio = await _get_tts_audio_from_hass_cloud(self.hass, language, gender, sentence)

        def send_request():
            auth = None
            if self._user or self._password:
                auth = HTTPBasicAuth(self._user, self._password)

            try:
                url = "http://" + self._host + ":" + str(self._port) + "/cgi-bin/speaker.sh"
                response = requests.post(url, data=audio, timeout=5, auth=auth)
                if response.status_code >= 300:
                    _LOGGER.error("Failed to send speaker command to device %s", self._host)
            except requests.exceptions.RequestException as error:
                _LOGGER.error("Failed to send speaker command to device %s: error %s", self._host, error)

        await self.hass.async_add_executor_job(send_request)

    def _perform_reboot(self):
        auth = None
        if self._user or self._password:
            auth = HTTPBasicAuth(self._user, self._password)

        try:
            response = requests.get(f"http://{self._host}:{self._port}/cgi-bin/reboot.sh", timeout=HTTP_TIMEOUT, auth=auth)
            if response.status_code >= 300:
                _LOGGER.error(f"Failed to send reboot command to device {self._host}")
        except requests.exceptions.RequestException as error:
            _LOGGER.error(f"Failed to send reboot command to device {self._host}: error {error}")

    async def async_perform_reboot(self):
        """Reboot the camera."""
        _LOGGER.debug(f"Reboot the camera")

        await self.hass.async_add_executor_job(self._perform_reboot)

    @property
    def brand(self):
        """Camera brand."""
        return DEFAULT_BRAND

    @property
    def name(self):
        """Return the name of the camera."""
        return self._name

    @property
    def is_on(self):
        """Determine whether the camera is on."""
        return self._state

    @property
    def state(self):
        """Return "on" if entity is on."""
        if self._state:
            return STATE_ON
        return STATE_OFF

    @property
    def unique_id(self):
        """Return a unique ID."""
        return self._unique_id

    @property
    def icon(self):
        """Return the icon to use in the frontend."""
        return ICON

    @property
    def device_info(self):
        """Return device specific attributes."""
        return {
            "name": self._device_name,
            "connections": {(CONNECTION_NETWORK_MAC, self._mac)},
            "identifiers": {(DOMAIN, self._mac)},
            "manufacturer": DEFAULT_BRAND,
            "model": DOMAIN,
            "configuration_url": self._http_base_url,
        }

class YiHackMqttCamera(Camera):
    """Representation of a MQTT camera."""

    def __init__(self, hass: HomeAssistant, config):
        """Initialize the MQTT Camera."""
        super().__init__()

        self._device_name = config.data[CONF_NAME]
        self._name = self._device_name + " " + "Motion Detection Cam"
        self._unique_id = self._device_name + "_camd"
        self._mac = config.data[CONF_MAC]
        self._host = config.data[CONF_HOST]
        self._port = config.data[CONF_PORT]
        self._user = config.data[CONF_USERNAME]
        self._password = config.data[CONF_PASSWORD]
        self._image_topic = config.data[CONF_MQTT_PREFIX] + "/" + config.data[CONF_TOPIC_MOTION_DETECTION_IMAGE]
        self._last_image = None
        self._mqtt_subscription = None
        self._mqtt_image_subscription = None
        self._mqtt_cmnd_topic = config.data[CONF_MQTT_PREFIX] + "/cmnd/camera/switch_on"
        self._mqtt_stat_topic = config.data[CONF_MQTT_PREFIX] + "/stat/camera/switch_on"
        self._state = True

    async def async_added_to_hass(self):
        """Subscribe to MQTT events."""

        @callback
        def message_received(msg):
            """Handle new MQTT messages."""
            try:
                payload = msg.payload.decode("utf-8", "ignore")
            except:
                payload = msg.payload

            if payload in ["yes", "on"]:
                self._state = True
            elif payload in ["no", "off"]:
                self._state = False
            else:  # Payload is not correct for this entity
                _LOGGER.info(
                    "No matching payload found for entity %s with topic: %s. Payload: '%s'",
                    self._name,
                    self._mqtt_stat_topic,
                    payload,
                )
                return

            self.async_write_ha_state()

        self._mqtt_subscription = await mqtt.async_subscribe(
            self.hass, self._mqtt_stat_topic, message_received, 1, None
        )

        @callback
        def image_message_received(msg):
            """Handle new MQTT messages."""
            data = msg.payload

            self._last_image = data

        self._mqtt_image_subscription = await mqtt.async_subscribe(
            self.hass, self._image_topic, image_message_received, 1, None
        )

    async def async_will_remove_from_hass(self):
        """Unsubscribe from MQTT events."""
        if self._mqtt_subscription:
            self._mqtt_subscription()
        if self._mqtt_image_subscription:
            self._mqtt_image_subscription()

    @property
    def supported_features(self) -> CameraEntityFeature:
        """Return supported features."""
        return CameraEntityFeature.STREAM | CameraEntityFeature.ON_OFF

    async def async_turn_off(self):
        """Turn off camera"""
        self.hass.async_create_task(
            mqtt.async_publish(self.hass, self._mqtt_cmnd_topic, "no", 1, 0)
        )
        self._state = False

    async def async_turn_on(self):
        """Turn on camera"""
        self.hass.async_create_task(
            mqtt.async_publish(self.hass, self._mqtt_cmnd_topic, "yes", 1, 0)
        )
        self._state = True

    async def async_camera_image(
        self, width: int | None = None, height: int | None = None
    ) -> bytes | None:
        """Return image response."""
        """Ignore width and height: camera component will resize it."""
        return self._last_image

    @property
    def brand(self):
        """Camera brand."""
        return DEFAULT_BRAND

    @property
    def name(self):
        """Return the name of the camera."""
        return self._name

    @property
    def is_on(self):
        """Determine whether the camera is on."""
        return self._state

    @property
    def state(self):
        """Return "on" if entity is on."""
        if self._state:
            return STATE_ON
        return STATE_OFF

    @property
    def unique_id(self):
        """Return a unique ID."""
        return self._unique_id

    @property
    def icon(self):
        """Return the icon to use in the frontend."""
        return ICON

    @property
    def device_info(self):
        """Return device specific attributes."""
        return {
            "name": self._device_name,
            "connections": {(CONNECTION_NETWORK_MAC, self._mac)},
            "identifiers": {(DOMAIN, self._mac)},
            "manufacturer": DEFAULT_BRAND,
            "model": DOMAIN,
        }


async def _get_tts_audio_from_hass_cloud(
        hass: HomeAssistant, language: str, gender: Gender, sentence: str
):
    """Get Speech from text over Azure. see: hass_nabucasa.voice.Voice.process_tts."""
    import xml.etree.ElementTree as ET

    from aiohttp.hdrs import AUTHORIZATION, CONTENT_TYPE
    from hass_nabucasa import Cloud, Voice

    from homeassistant.components.cloud import DOMAIN as CLOUD_DOMAIN

    cloud: Cloud = hass.data[CLOUD_DOMAIN]
    tts: Voice = cloud.voice

    """Get Speech from text over Azure."""
    if not tts._validate_token():
        await tts._update_token()

    # SSML
    xml_body = ET.Element("speak", version="1.0")
    xml_body.set("{http://www.w3.org/XML/1998/namespace}lang", language)
    voice = ET.SubElement(xml_body, "voice")
    voice.set("{http://www.w3.org/XML/1998/namespace}lang", language)
    voice.set(
        "name",
        f"Microsoft Server Speech Text to Speech Voice ({language}, {MAP_VOICE[(language, gender)]})",
    )
    voice.text = sentence[:2048]

    # Send request
    async with cloud.websession.post(
            tts._endpoint_tts,
            headers={
                CONTENT_TYPE: "application/ssml+xml",
                AUTHORIZATION: f"Bearer {tts._token}",
                "X-Microsoft-OutputFormat": "raw-16khz-16bit-mono-pcm",
            },
            data=ET.tostring(xml_body),
    ) as resp:
        if resp.status != 200:
            _LOGGER.error("Failed to call TTS")
            return None
        return await resp.read()

