"""Drive times worked out in Home Assistant (0.4.4): Apple Maps, TomTom, Google, or the longest.

Each asks for the drive arriving by a time, with the traffic expected then, as the app does. With
one chosen in the options, the leave checks re-check the drive here instead of asking the phone
(RUN_LEAVE_CHECK), so they work with the phone asleep.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import logging
import re
import time
from typing import Any
from urllib.parse import quote

from aiohttp import ClientError, ClientSession, ClientTimeout

_LOGGER = logging.getLogger(__name__)

SOURCE_PHONE = "phone"
SOURCE_APPLE = "apple"
SOURCE_TOMTOM = "tomtom"
SOURCE_GOOGLE = "google"
SOURCE_LONGEST = "longest"
SOURCES = [SOURCE_PHONE, SOURCE_APPLE, SOURCE_TOMTOM, SOURCE_GOOGLE, SOURCE_LONGEST]
TIMEOUT = ClientTimeout(total=15)


# key=…, api_key=…, token=… in a request address (v0.4.7 item 3).
_SECRET_PARAM = re.compile(r"((?:[?&]|\b)(?:key|api_key|apikey|token|access_token)=)[^&\s'\")]+", re.IGNORECASE)


def scrub(text: str, *secrets: str | None) -> str:
    """The text with API keys taken out, so an error never puts one in the log (v0.4.7 item 3)."""
    for secret in secrets:
        if secret and len(secret) >= 6:
            text = text.replace(secret, "•••")
    return _SECRET_PARAM.sub(r"\1•••", text)


class DriveTimeError(Exception):
    """A service couldn't give a drive time. Its text never carries a key."""

    def __init__(self, message: str, *secrets: str | None) -> None:
        super().__init__(scrub(message, *secrets))


class GoogleStopped(DriveTimeError):
    """Google wasn't asked: today's budget is used, or Google refused a request (its own daily
    cap). Not retried until tomorrow (v0.6.0 item 0.5.6-14)."""


GOOGLE_DAILY_BUDGET = 120
GOOGLE_DAYS_KEPT = 7
STOP_BUDGET = "budget"
STOP_REFUSED = "refused"
STOP_PAUSED = "paused"
PAUSE_AFTER_REFUSAL = 3600
START_KEPT_SECONDS = 14 * 86400


def is_google_refusal(status: int, text: str) -> bool:
    """A refusal: HTTP 429, or RESOURCE_EXHAUSTED in the answer."""
    return status == 429 or "RESOURCE_EXHAUSTED" in (text or "")


def is_daily_refusal(text: str) -> bool:
    """Google's daily cap: RESOURCE_EXHAUSTED naming a per-day quota (anything else, such as a
    per-minute 429, pauses Google for an hour instead)."""
    lower = (text or "").lower()
    return "RESOURCE_EXHAUSTED" in (text or "") and ("per day" in lower or "perday" in lower or "per_day" in lower)


def _local_day() -> str:
    from homeassistant.util import dt as dt_util  # noqa: PLC0415

    return dt_util.now().date().isoformat()


class GoogleUsage:
    """Google requests per day (v0.6.0 item 0.5.6-14), kept in a dict the bridge saves with its
    state: {"days": {"2026-11-26": 12}, "refused_on": "2026-11-26", "paused_until": epoch,
    "last": {trip: [seconds, epoch]}}. Never holds the key."""

    def __init__(self, store: dict[str, Any], budget: int | None = None, today=None, clock=None) -> None:
        self.store = store
        self.budget = int(budget or GOOGLE_DAILY_BUDGET)
        self._today = today or _local_day
        self._clock = clock or time.time

    def today(self) -> int:
        return int((self.store.get("days") or {}).get(self._today(), 0))

    def stopped(self) -> str | None:
        """Why Google isn't asked today, or None."""
        if self.store.get("refused_on") == self._today():
            return STOP_REFUSED
        until = self.store.get("paused_until")
        if isinstance(until, (int, float)) and self._clock() < until:
            return STOP_PAUSED
        if self.today() >= self.budget:
            return STOP_BUDGET
        return None

    def note_request(self) -> None:
        day = self._today()
        days = dict(self.store.get("days") or {})
        days[day] = int(days.get(day, 0)) + 1
        self.store["days"] = {k: days[k] for k in sorted(days)[-GOOGLE_DAYS_KEPT:]}

    def note_refused(self, daily: bool = True) -> None:
        if daily:
            self.store["refused_on"] = self._today()
        else:
            self.store["paused_until"] = self._clock() + PAUSE_AFTER_REFUSAL

    def logged_today(self) -> bool:
        """Whether today's stop was already logged (logged once a day, at info)."""
        if self.store.get("logged_on") == self._today():
            return True
        self.store["logged_on"] = self._today()
        return False

    def last_seconds(self, trip: str) -> float | None:
        """The last answer for the trip, as a starting guess, if under 14 days old."""
        value = (self.store.get("last") or {}).get(trip)
        if not isinstance(value, (list, tuple)) or len(value) != 2:
            return None
        seconds, at = value
        if not isinstance(seconds, (int, float)) or not isinstance(at, (int, float)):
            return None
        return float(seconds) if self._clock() - at < START_KEPT_SECONDS else None

    def keep_seconds(self, trip: str, seconds: float) -> None:
        last = dict(self.store.get("last") or {})
        last.pop(trip, None)
        last[trip] = [seconds, self._clock()]
        self.store["last"] = dict(list(last.items())[-10:])


def google_attributes(store: dict[str, Any] | None, budget: int | None, today: str | None = None) -> dict[str, Any]:
    """The drive-time sensor's Google attributes: today's count, the last 7 days, the budget and
    whether Google is paused for the day."""
    usage = GoogleUsage(store if store is not None else {}, budget, (lambda: today) if today else None)
    return {
        "google_requests_today": usage.today(),
        "google_requests_by_day": dict(usage.store.get("days") or {}),
        "google_daily_budget": usage.budget,
        "google_paused": usage.stopped(),
    }


@dataclass
class Place:
    """Where a drive goes: coordinates when the calendar had them, else the address."""

    text: str = ""
    latitude: float | None = None
    longitude: float | None = None

    @property
    def has_coordinates(self) -> bool:
        return self.latitude is not None and self.longitude is not None

    @property
    def latlon(self) -> str:
        return f"{self.latitude},{self.longitude}"


def utc(when: datetime) -> str:
    """ISO 8601 in UTC with Z, which every service takes without query-string trouble."""
    return when.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class AppleMaps:
    """Apple Maps Server API: a Maps key (.p8) signs a token, exchanged for a 30-minute access token."""

    BASE = "https://maps-api.apple.com/v1"

    def __init__(self, session: ClientSession, key: str, key_id: str, team_id: str) -> None:
        self._session = session
        self._key = key.strip()
        self._key_id = key_id.strip()
        self._team_id = team_id
        self._access: tuple[str, float] | None = None

    def _auth_token(self) -> str:
        import jwt  # noqa: PLC0415 (PyJWT ships with Home Assistant)

        now = int(time.time())
        return jwt.encode({"iss": self._team_id, "iat": now, "exp": now + 1800}, self._key, algorithm="ES256",
                          headers={"kid": self._key_id, "typ": "JWT"})

    async def _access_token(self) -> str:
        if self._access and time.time() < self._access[1] - 60:
            return self._access[0]
        data = await self._get("/token", token=self._auth_token())
        token = data.get("accessToken")
        if not token:
            raise DriveTimeError("Apple Maps gave no access token")
        self._access = (token, time.time() + int(data.get("expiresInSeconds") or 1800))
        return token

    async def _get(self, path: str, *, token: str, params: dict[str, str] | None = None) -> dict[str, Any]:
        try:
            async with self._session.get(f"{self.BASE}{path}", params=params, timeout=TIMEOUT,
                                         headers={"Authorization": f"Bearer {token}"}) as response:
                if response.status != 200:
                    raise DriveTimeError(f"Apple Maps {response.status}: {(await response.text())[:150]}")
                return await response.json()
        except (ClientError, TimeoutError) as err:
            raise DriveTimeError(f"Apple Maps: {err}") from err

    async def locate(self, place: Place) -> Place:
        if place.has_coordinates:
            return place
        data = await self._get("/geocode", token=await self._access_token(), params={"q": place.text})
        results = data.get("results") or []
        if not results:
            raise DriveTimeError(f"Apple Maps couldn't find {place.text}")
        coordinate = results[0].get("coordinate") or {}
        return Place(place.text, coordinate.get("latitude"), coordinate.get("longitude"))

    async def drive_seconds(self, origin: Place, destination: Place, arrive_by: datetime) -> float:
        destination = await self.locate(destination)
        data = await self._get("/etas", token=await self._access_token(), params={
            "origin": origin.latlon, "destinations": destination.latlon,
            "transportType": "Automobile", "arrivalDate": utc(arrive_by),
        })
        etas = data.get("etas") or []
        if not etas or etas[0].get("expectedTravelTimeSeconds") is None:
            raise DriveTimeError("Apple Maps found no route")
        return float(etas[0]["expectedTravelTimeSeconds"])


class TomTom:
    """TomTom Routing API: takes an arrival time directly, with its traffic for that time."""

    def __init__(self, session: ClientSession, key: str) -> None:
        self._session = session
        self._key = key.strip()

    async def _get(self, url: str, params: dict[str, str]) -> dict[str, Any]:
        try:
            async with self._session.get(url, params={"key": self._key, **params}, timeout=TIMEOUT) as response:
                if response.status != 200:
                    raise DriveTimeError(f"TomTom {response.status}: {(await response.text())[:150]}", self._key)
                return await response.json()
        except (ClientError, TimeoutError) as err:
            raise DriveTimeError(f"TomTom: {err}", self._key) from err

    async def locate(self, place: Place) -> Place:
        if place.has_coordinates:
            return place
        data = await self._get(f"https://api.tomtom.com/search/2/geocode/{quote(place.text)}.json", {"limit": "1"})
        results = data.get("results") or []
        if not results:
            raise DriveTimeError(f"TomTom couldn't find {place.text}", self._key)
        position = results[0].get("position") or {}
        return Place(place.text, position.get("lat"), position.get("lon"))

    async def drive_seconds(self, origin: Place, destination: Place, arrive_by: datetime) -> float:
        destination = await self.locate(destination)
        data = await self._get(
            f"https://api.tomtom.com/routing/1/calculateRoute/{origin.latlon}:{destination.latlon}/json",
            {"travelMode": "car", "routeType": "fastest", "traffic": "true", "arriveAt": utc(arrive_by)},
        )
        routes = data.get("routes") or []
        if not routes:
            raise DriveTimeError("TomTom found no route", self._key)
        return float(routes[0]["summary"]["travelTimeInSeconds"])


class Google:
    """Google Routes API: departure times only, so it works back from the arrival (as the app's
    DepartureSolver does)."""

    def __init__(self, session: ClientSession, key: str, bundle_id: str | None = None, pessimistic: bool = False,
                 usage: GoogleUsage | None = None) -> None:
        self._session = session
        self._key = key.strip()
        self._bundle_id = bundle_id
        self._pessimistic = pessimistic
        # v0.6.0 item 0.5.6-14: counted per day, stopped at the budget or Google's refusal.
        self._usage = usage

    async def _duration(self, origin: Place, destination: Place, depart: datetime) -> float:
        if self._usage is not None:
            stop = self._usage.stopped()
            if stop:
                raise GoogleStopped({STOP_REFUSED: "Google paused for today: refused (its daily cap)",
                                     STOP_PAUSED: "Google paused for an hour: refused",
                                     STOP_BUDGET: "Google paused for today: daily budget used"}[stop])
            self._usage.note_request()
        def waypoint(place: Place) -> dict[str, Any]:
            if place.has_coordinates:
                return {"location": {"latLng": {"latitude": place.latitude, "longitude": place.longitude}}}
            return {"address": place.text}

        headers = {"X-Goog-Api-Key": self._key, "X-Goog-FieldMask": "routes.duration"}
        if self._bundle_id:
            # A key restricted to the iOS app accepts requests that name it.
            headers["X-Ios-Bundle-Identifier"] = self._bundle_id
        body = {
            "origin": waypoint(origin), "destination": waypoint(destination), "travelMode": "DRIVE",
            "routingPreference": "TRAFFIC_AWARE_OPTIMAL", "trafficModel": "PESSIMISTIC" if self._pessimistic else "BEST_GUESS",
            "departureTime": utc(depart),
        }
        try:
            async with self._session.post("https://routes.googleapis.com/directions/v2:computeRoutes", json=body,
                                          headers=headers, timeout=TIMEOUT) as response:
                if response.status != 200:
                    text = await response.text()
                    if is_google_refusal(response.status, text):
                        # Google's daily cap: no retry today; any other refusal: an hour.
                        daily = is_daily_refusal(text)
                        if self._usage is not None:
                            self._usage.note_refused(daily)
                        raise GoogleStopped(f"Google refused ({response.status}), paused {'for today' if daily else 'for an hour'}",
                                            self._key)
                    raise DriveTimeError(f"Google {response.status}: {text[:150]}", self._key)
                data = await response.json()
        except (ClientError, TimeoutError) as err:
            raise DriveTimeError(f"Google: {err}", self._key) from err
        routes = data.get("routes") or []
        if not routes or not routes[0].get("duration"):
            raise DriveTimeError("Google found no route", self._key)
        return float(str(routes[0]["duration"]).rstrip("s"))

    async def drive_seconds(self, origin: Place, destination: Place, arrive_by: datetime, now: datetime | None = None) -> float:
        trip = f"{_where(origin)}|{_where(destination)}|{'worst' if self._pessimistic else 'typical'}"
        # v0.6.0 item 0.5.6-14: start from the last answer for this trip (usually 1-2 requests).
        start = self._usage.last_seconds(trip) if self._usage is not None else None
        seconds = await solve_departure(arrive_by, now or datetime.now(arrive_by.tzinfo),
                                        lambda depart: self._duration(origin, destination, depart), start_seconds=start)
        if self._usage is not None:
            self._usage.keep_seconds(trip, seconds)
        return seconds


def _where(place: Place) -> str:
    return f"{place.latitude:.4f},{place.longitude:.4f}" if place.has_coordinates else place.text


async def solve_departure(arrive_by: datetime, now: datetime, duration, max_requests: int = 4, tolerance: float = 60,
                          start_seconds: float | None = None) -> float:
    """The time to allow (arrival − the latest departure found that still arrives in time).
    `start_seconds`: the last answer for this trip, the first guess (else 30 min)."""
    earliest = now + timedelta(minutes=1)
    depart = max(arrive_by - timedelta(seconds=start_guess(start_seconds, arrive_by, now)), earliest)
    latest_on_time: datetime | None = None
    longest = 0.0
    for _ in range(max_requests):
        trip = await duration(depart)
        longest = max(longest, trip)
        arrives = depart + timedelta(seconds=trip)
        if arrives <= arrive_by and (latest_on_time is None or depart > latest_on_time):
            latest_on_time = depart
        late = (arrives - arrive_by).total_seconds()
        if abs(late) <= tolerance:
            # Landed: this try decides, not an earlier (stale) on-time guess. On time: leave at
            # this departure; a little late: its trip plus the lateness.
            return (arrive_by - depart).total_seconds() if late <= 0 else trip + late
        following = max(arrive_by - timedelta(seconds=trip), earliest)
        if following == depart:
            break
        depart = following
    if latest_on_time is not None:
        return (arrive_by - latest_on_time).total_seconds()
    return longest


def start_guess(start_seconds: float | None, arrive_by: datetime, now: datetime) -> float:
    """The first try: the last answer when sensible (at most 3 hours, leaving after now), else 30 min."""
    fallback = 30 * 60.0
    if not start_seconds or start_seconds <= 0 or start_seconds > 3 * 3600:
        return fallback
    room = (arrive_by - now).total_seconds() - 120
    return float(start_seconds) if start_seconds < room else min(fallback, max(room, 60.0))


async def drive_seconds(services: list[Any], origin: Place, destination: Place, arrive_by: datetime) -> float:
    """The longest answer of the services that gave one (one service: its answer)."""
    answers: list[float] = []
    errors: list[str] = []
    stopped: list[str] = []
    for service in services:
        try:
            answers.append(await service.drive_seconds(origin, destination, arrive_by))
        except GoogleStopped as err:
            stopped.append(str(err))
        except DriveTimeError as err:
            errors.append(str(err))
        except Exception as err:  # noqa: BLE001 - v0.6.0 item 36: any service error falls back to the phone
            errors.append(f"{type(err).__name__}: {err}")
    if not answers:
        if stopped and not errors:
            raise GoogleStopped("; ".join(stopped))
        raise DriveTimeError("; ".join(errors + stopped) or "no drive-time service set up")
    if errors:
        _LOGGER.info("Bedtime Bridge drive time: %s", "; ".join(errors))
    return max(answers)
