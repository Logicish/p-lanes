# providers/homeassistant/provider.py
#
# Author:  Logicish
# Company: Logic-Ish Designs
# Date:    4/2/2026
#
# ==================================================
# Home Assistant REST API provider.
# Fetches entity states and calls services via the
# HA long-lived token REST API.
#
# Self-contained: reads providers/homeassistant/config.yaml.
# Does not touch core config.
#
# Knows about: providers.base only.
# ==================================================

# ==================================================
# Imports
# ==================================================
import aiohttp
import structlog

from providers.base import Provider

log = structlog.get_logger()


# ==================================================
# HomeAssistantProvider
# ==================================================

class HomeAssistantProvider(Provider):

    def __init__(self, cfg: dict):
        self._url:                str       = cfg["url"].rstrip("/")
        self._token:              str       = cfg["token"]
        self._timeout:            int       = cfg.get("timeout", 10)
        self._domains:            list      = cfg.get("domains", ["light", "switch", "climate", "lock"])
        self._exclude_entity_ids: set[str]  = set(cfg.get("exclude_entity_ids", []))
        self._weather_entity:     str       = cfg.get("weather_entity", "weather.forecast_home")
        self._session: aiohttp.ClientSession | None = None
        self._ready:   bool = False

    # --------------------------------------------------
    # Provider identity / state
    # --------------------------------------------------

    @property
    def name(self) -> str:
        return "homeassistant"

    @property
    def is_ready(self) -> bool:
        return self._ready

    @property
    def exclude_entity_ids(self) -> set[str]:
        return self._exclude_entity_ids

    @property
    def weather_entity(self) -> str:
        return self._weather_entity

    def _headers(self) -> dict:
        return {
            "Authorization": f"Bearer {self._token}",
            "Content-Type":  "application/json",
        }

    # --------------------------------------------------
    # Lifecycle
    # --------------------------------------------------

    async def start(self) -> bool:
        self._session = aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=self._timeout)
        )
        try:
            async with self._session.get(
                f"{self._url}/api/",
                headers=self._headers(),
            ) as resp:
                self._ready = resp.status == 200
        except Exception as e:
            log.warning("ha_health_failed", error=str(e))
            self._ready = False

        if self._ready:
            log.info("ha_ready", url=self._url)
        else:
            log.warning("ha_not_ready", url=self._url)
        return self._ready

    async def stop(self) -> None:
        self._ready = False
        if self._session:
            await self._session.close()
            self._session = None

    # --------------------------------------------------
    # States
    # --------------------------------------------------

    async def get_states(self, domains: list[str] | None = None) -> list[dict]:
        """Fetch all entity states, filtered to the configured domains.
        Pass domains to override the default domain list for this call."""
        if not self._session:
            return []
        domains = domains or self._domains
        try:
            async with self._session.get(
                f"{self._url}/api/states",
                headers=self._headers(),
            ) as resp:
                if resp.status != 200:
                    log.warning("ha_get_states_failed", status=resp.status)
                    return []
                all_states = await resp.json()
                return [
                    s for s in all_states
                    if s["entity_id"].split(".")[0] in domains
                ]
        except Exception as e:
            log.error("ha_get_states_error", error=str(e))
            return []

    # --------------------------------------------------
    # Entity Registry
    # --------------------------------------------------

    async def get_entity_registry(self) -> dict[str, dict]:
        """Fetch entity registry entries.
        Returns {entity_id: {aliases: list[str], area_id: str}}.
        Returns {} if the endpoint is unavailable (older HA versions)."""
        if not self._session:
            return {}
        try:
            async with self._session.get(
                f"{self._url}/api/config/entity_registry",
                headers=self._headers(),
            ) as resp:
                if resp.status != 200:
                    log.debug("ha_entity_registry_unavailable", status=resp.status)
                    return {}
                entries = await resp.json()
                return {
                    e["entity_id"]: {
                        "aliases": e.get("aliases") or [],
                        "area_id": e.get("area_id") or "",
                    }
                    for e in entries
                    if "entity_id" in e
                }
        except Exception as e:
            log.warning("ha_entity_registry_error", error=str(e))
            return {}

    # --------------------------------------------------
    # Area lookup via template API
    # --------------------------------------------------

    async def get_areas_for_entities(self, entity_ids: list[str]) -> dict[str, str]:
        """Return {entity_id: area_name} for all entities that have an area assigned.
        Uses the /api/template endpoint — works even when the entity registry
        REST endpoint is unavailable (older HA versions)."""
        if not self._session or not entity_ids:
            return {}

        # build a template that returns a pipe-delimited list of entity_id|area_name
        lines = "\n".join(
            f"{{% set a = area_name('{eid}') | default('') %}}"
            f"{{% if a %}}{{{{ '{eid}' }}}}|{{{{ a }}}},{{% endif %}}"
            for eid in entity_ids
        )
        template = lines

        try:
            async with self._session.post(
                f"{self._url}/api/template",
                headers=self._headers(),
                json={"template": template},
            ) as resp:
                if resp.status != 200:
                    log.debug("ha_template_areas_failed", status=resp.status)
                    return {}
                text = (await resp.text()).strip()
                result = {}
                for part in text.split(","):
                    part = part.strip()
                    if "|" in part:
                        eid, area = part.split("|", 1)
                        result[eid.strip()] = area.strip()
                return result
        except Exception as e:
            log.warning("ha_template_areas_error", error=str(e))
            return {}

    # --------------------------------------------------
    # Services
    # --------------------------------------------------

    async def call_service(
        self,
        domain:       str,
        service:      str,
        entity_id:    str = "",
        area_id:      str = "",
        service_data: dict | None = None,
    ) -> bool:
        """Call a HA service. Returns True on success.

        Args:
            domain:       HA domain (e.g. 'light', 'switch')
            service:      HA service (e.g. 'turn_on', 'turn_off')
            entity_id:    Full entity ID (e.g. 'light.kitchen') — or empty if using area_id
            area_id:      HA area ID (e.g. 'master_bedroom') — targets all entities in area
            service_data: Optional extra params (brightness, temp, etc.)
        """
        if not self._session:
            return False

        payload: dict = {}
        if entity_id:
            payload["entity_id"] = entity_id
        if area_id:
            payload["area_id"] = area_id
        if service_data:
            payload.update(service_data)

        try:
            async with self._session.post(
                f"{self._url}/api/services/{domain}/{service}",
                headers=self._headers(),
                json=payload,
            ) as resp:
                ok = resp.status in (200, 201)
                if not ok:
                    body = await resp.text()
                    log.warning("ha_service_call_failed",
                                domain=domain, service=service,
                                entity_id=entity_id,
                                status=resp.status, body=body[:200])
                return ok
        except Exception as e:
            log.error("ha_service_call_error",
                      domain=domain, service=service,
                      entity_id=entity_id, error=str(e))
            return False
