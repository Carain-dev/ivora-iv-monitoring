"""Read-only client for the Blynk Cloud Device HTTPS API.

Only documented read endpoints are used (docs.blynk.io, "Device HTTPS API"):
  GET /external/api/isHardwareConnected?token=T
  GET /external/api/get?token=T&V1&V2
  GET /external/api/getAll?token=T
  GET /external/api/data/get?token=T&period=..&granularityType=..&format=..&pin=..

This client never writes to pins: there is no update/batch/event method, and
_get() refuses any path outside READ_ENDPOINTS.

The token is never logged or included in exceptions; use token_label() to
refer to it (last four characters).
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

DEFAULT_BASE_URL = "https://blynk.cloud"
READ_ENDPOINTS = frozenset({"isHardwareConnected", "get", "getAll", "data/get"})
HISTORY_PERIODS = frozenset({"HOUR", "DAY", "WEEK", "MONTH", "THREE_MONTHS"})
HISTORY_GRANULARITY = frozenset({"RAW_DATA", "MINUTE", "HOURLY", "DAILY"})
HISTORY_FORMATS = frozenset({"TS", "ISO_US", "ISO_SIMPLE"})
# Blynk answers a wrong region with 308 + Location (docs: HTTPS API Troubleshooting).
# Only follow redirects that stay on Blynk's own domain.
ALLOWED_REDIRECT_SUFFIX = ".blynk.cloud"


def token_label(token: str) -> str:
    return f"…{token[-4:]}" if token else "(none)"


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


class BlynkError(Exception):
    """Base error. Messages never contain the token."""


class BlynkApiError(BlynkError):
    """Blynk answered with an error body (e.g. invalid token, wrong pin)."""

    def __init__(self, status: int, message: str):
        super().__init__(f"HTTP {status}: {message}")
        self.status = status
        self.message = message


class BlynkUnavailable(BlynkError):
    """Network failure, timeout, or unexpected non-JSON/5xx response."""


@dataclass
class ApiResponse:
    """One HTTP exchange, with the server-receipt time we observed."""

    endpoint: str
    status: int
    body: Any
    received_at: datetime
    latency_ms: float
    final_host: str
    raw_text: str = field(repr=False, default="")


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class BlynkClient:
    def __init__(self, token: str, base_url: str = DEFAULT_BASE_URL, timeout: float = 10.0):
        if not token:
            raise ValueError("token is required")
        self._token = token
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self._opener = urllib.request.build_opener(_NoRedirect)

    @property
    def label(self) -> str:
        return token_label(self._token)

    # ---- documented read endpoints -------------------------------------

    def is_hardware_connected(self) -> ApiResponse:
        return self._get("isHardwareConnected", [])

    def get_pins(self, *pins: str) -> ApiResponse:
        """Latest stored value of one or more pins, e.g. get_pins("V0", "V1")."""
        if not pins:
            raise ValueError("at least one pin")
        for p in pins:
            if not _valid_pin(p):
                raise ValueError(f"bad pin name: {p!r}")
        # Docs use the bare form "&V1" (no "="), so pass value None.
        return self._get("get", [(p, None) for p in pins])

    def get_all(self) -> ApiResponse:
        """Every datastream that has a stored value, keyed by pin (v0, d0, a0 ...)."""
        return self._get("getAll", [])

    def history(
        self,
        period: str = "DAY",
        granularity: str = "RAW_DATA",
        fmt: str = "ISO_SIMPLE",
        pin: str | None = None,
        tz_name: str = "UTC",
    ) -> ApiResponse:
        """Request a history export. Blynk replies with {"link": ...} to a zip file."""
        if period not in HISTORY_PERIODS:
            raise ValueError(f"period must be one of {sorted(HISTORY_PERIODS)}")
        if granularity not in HISTORY_GRANULARITY:
            raise ValueError(f"granularity must be one of {sorted(HISTORY_GRANULARITY)}")
        if fmt not in HISTORY_FORMATS:
            raise ValueError(f"fmt must be one of {sorted(HISTORY_FORMATS)}")
        if granularity == "RAW_DATA" and period == "THREE_MONTHS":
            raise ValueError("Blynk does not support RAW_DATA for THREE_MONTHS")
        params: list[tuple[str, str | None]] = [
            ("period", period),
            ("granularityType", granularity),
            ("format", fmt),
            ("tzName", tz_name),
        ]
        if pin:
            if not _valid_pin(pin):
                raise ValueError(f"bad pin name: {pin!r}")
            params.append(("pin", pin))
        return self._get("data/get", params)

    def download_export(self, link: str) -> bytes:
        """Fetch the zip a history() call pointed to. Only Blynk-hosted links."""
        host = urllib.parse.urlsplit(link).hostname or ""
        if urllib.parse.urlsplit(link).scheme != "https" or not _is_blynk_host(host):
            raise BlynkError(f"refusing to download export from non-Blynk host {host!r}")
        try:
            with self._opener.open(link, timeout=max(self.timeout, 30)) as r:
                return r.read()
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            raise BlynkUnavailable(f"export download failed: {_safe(e, self._token)}") from None

    # ---- transport -----------------------------------------------------

    def _get(self, endpoint: str, params: list[tuple[str, str | None]], _hops: int = 0) -> ApiResponse:
        if endpoint not in READ_ENDPOINTS:
            raise BlynkError(f"endpoint {endpoint!r} is not a read endpoint")
        query = "&".join(
            [f"token={urllib.parse.quote(self._token, safe='')}"]
            + [k if v is None else f"{k}={urllib.parse.quote(v, safe='')}" for k, v in params]
        )
        url = f"{self.base_url}/external/api/{endpoint}?{query}"
        start = time.monotonic()
        try:
            with self._opener.open(urllib.request.Request(url, method="GET"), timeout=self.timeout) as r:
                status, text = r.status, r.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as e:
            status, text = e.code, e.read().decode("utf-8", "replace")
            if status in (301, 302, 307, 308) and _hops == 0:
                loc = e.headers.get("Location", "")
                host = urllib.parse.urlsplit(loc).hostname or ""
                if _is_blynk_host(host):
                    self.base_url = f"https://{host}"
                    return self._get(endpoint, params, _hops=1)
                raise BlynkUnavailable(f"redirect to non-Blynk host {host!r} refused") from None
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            raise BlynkUnavailable(f"{endpoint}: {_safe(e, self._token)}") from None
        latency_ms = (time.monotonic() - start) * 1000
        received_at = utc_now()

        body = _parse(text)
        if status >= 500:
            raise BlynkUnavailable(f"{endpoint}: HTTP {status}")
        if status >= 400:
            msg = body.get("error", {}).get("message") if isinstance(body, dict) else None
            raise BlynkApiError(status, _safe(msg or text[:200], self._token))
        return ApiResponse(
            endpoint=endpoint,
            status=status,
            body=body,
            received_at=received_at,
            latency_ms=round(latency_ms, 1),
            final_host=urllib.parse.urlsplit(self.base_url).hostname or "",
            raw_text=text,
        )


def _parse(text: str) -> Any:
    try:
        return json.loads(text)
    except ValueError:
        return text.strip()


def _valid_pin(p: str) -> bool:
    return len(p) >= 2 and p[0] in "VvDdAa" and p[1:].isdigit()


def _is_blynk_host(host: str) -> bool:
    return host == "blynk.cloud" or host.endswith(ALLOWED_REDIRECT_SUFFIX)


def _safe(obj: Any, token: str) -> str:
    s = str(obj)
    return s.replace(token, token_label(token)) if token else s
