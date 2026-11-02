"""HTTP client for https://soutubot.moe reverse image search.

Public web API reverse-engineered from the site SPA:

1. GET homepage HTML, parse ``window.GLOBAL.m``
2. Build short-lived ``X-Api-Key``:
   ``base64(str(unix**2 + ua_len**2 + m)).reverse().strip('=')``
3. POST multipart ``/api/search`` with fields ``file`` + ``factor``
4. Optional GET ``/api/results/{id}`` to rehydrate a previous result page

factor:
- 1.2 = normal mode
- 1.4 = strict mode
"""

from __future__ import annotations

import asyncio
import base64
import logging
import re
import time
from dataclasses import dataclass
from typing import Any, Optional

import httpx

logger = logging.getLogger(__name__)

DEFAULT_BASE_URL = "https://soutubot.moe"
DEFAULT_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/126.0.0.0 Safari/537.36"
)

# Host defaults used by the SPA when composing full gallery URLs.
SOURCE_DEFAULT_HOSTS: dict[str, str] = {
    "nhentai": "nhentai.net",
    "ehentai": "e-hentai.org",
    "panda": "panda.chaika.moe",
}

SOURCE_LABELS: dict[str, str] = {
    "nhentai": "nHentai",
    "ehentai": "E-Hentai",
    "panda": "Panda Backup",
}


class SoutubotError(Exception):
    """Soutubot API / network error with optional status code."""

    def __init__(self, message: str, *, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


@dataclass(frozen=True)
class SourceCatalog:
    """Static source metadata matching the SPA."""

    hosts: dict[str, str]
    labels: dict[str, str]

    @classmethod
    def default(cls) -> "SourceCatalog":
        return cls(hosts=dict(SOURCE_DEFAULT_HOSTS), labels=dict(SOURCE_LABELS))

    def host_for(self, source: str) -> str:
        return self.hosts.get(str(source or "").strip().lower(), "")

    def label_for(self, source: str) -> str:
        key = str(source or "").strip().lower()
        return self.labels.get(key, key or "unknown")

    def build_url(self, source: str, path: str) -> str:
        host = self.host_for(source)
        path = str(path or "").strip()
        if not host or not path:
            return ""
        if not path.startswith("/"):
            path = "/" + path
        return f"https://{host}{path}"


def build_api_key(*, unix_ts: int | None = None, ua: str = DEFAULT_UA, m: int = 0) -> str:
    """Generate the ephemeral X-Api-Key used by soutubot.moe."""
    ts = int(time.time() if unix_ts is None else unix_ts)
    ua_len = len(ua or "")
    payload = str(ts * ts + ua_len * ua_len + int(m or 0))
    encoded = base64.b64encode(payload.encode("utf-8")).decode("ascii")
    return encoded[::-1].replace("=", "")


def parse_global_m(html: str) -> int:
    """Extract ``window.GLOBAL.m`` integer from homepage HTML."""
    if not html:
        raise SoutubotError("empty homepage HTML")
    # Prefer the explicit `m: <digits>` field inside GLOBAL.
    match = re.search(r"\bm\s*:\s*(\d{6,})", html)
    if match:
        return int(match.group(1))
    # Fallback: any long digit after GLOBAL.
    match = re.search(r"GLOBAL\s*=\s*\{[\s\S]{0,400}?m\s*:\s*(\d+)", html)
    if match:
        return int(match.group(1))
    raise SoutubotError("failed to parse GLOBAL.m from homepage")


def normalize_factor(strict: bool | None = None, factor: float | None = None) -> float:
    if factor is not None:
        try:
            value = float(factor)
        except (TypeError, ValueError) as exc:
            raise SoutubotError(f"invalid factor: {factor}") from exc
        if value <= 0:
            raise SoutubotError(f"factor must be positive: {value}")
        return value
    return 1.4 if strict else 1.2


def guess_filename_and_content_type(image_bytes: bytes, filename: str = "") -> tuple[str, str]:
    name = (filename or "").strip()
    if image_bytes.startswith(b"\xff\xd8\xff"):
        return (name or "search.jpg"), "image/jpeg"
    if image_bytes.startswith(b"\x89PNG\r\n\x1a\n"):
        return (name or "search.png"), "image/png"
    if image_bytes.startswith(b"GIF87a") or image_bytes.startswith(b"GIF89a"):
        return (name or "search.gif"), "image/gif"
    if image_bytes.startswith(b"RIFF") and b"WEBP" in image_bytes[:16]:
        return (name or "search.webp"), "image/webp"
    return (name or "search.bin"), "application/octet-stream"


class SoutubotClient:
    """Async client for soutubot.moe."""

    def __init__(
        self,
        *,
        base_url: str = DEFAULT_BASE_URL,
        user_agent: str = DEFAULT_UA,
        timeout_seconds: float = 60.0,
        max_retries: int = 1,
        catalog: SourceCatalog | None = None,
    ) -> None:
        self.base_url = (base_url or DEFAULT_BASE_URL).rstrip("/")
        self.user_agent = user_agent or DEFAULT_UA
        self.timeout_seconds = float(timeout_seconds or 60.0)
        self.max_retries = max(0, int(max_retries or 0))
        self.catalog = catalog or SourceCatalog.default()
        self._m: int | None = None
        self._m_fetched_at: float = 0.0
        self._m_ttl_seconds: float = 300.0
        self._lock = asyncio.Lock()

    def _client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            base_url=self.base_url,
            timeout=httpx.Timeout(self.timeout_seconds, connect=15.0),
            headers={
                "User-Agent": self.user_agent,
                "Accept": "application/json, text/plain, */*",
                "X-Requested-With": "XMLHttpRequest",
                "Origin": self.base_url,
                "Referer": f"{self.base_url}/",
            },
            follow_redirects=True,
        )

    async def refresh_m(self, *, force: bool = False) -> int:
        """Fetch homepage and cache GLOBAL.m."""
        now = time.time()
        if (
            not force
            and self._m is not None
            and (now - self._m_fetched_at) < self._m_ttl_seconds
        ):
            return self._m
        async with self._lock:
            now = time.time()
            if (
                not force
                and self._m is not None
                and (now - self._m_fetched_at) < self._m_ttl_seconds
            ):
                return self._m
            async with self._client() as client:
                resp = await client.get("/", headers={"Accept": "text/html,application/xhtml+xml"})
                if resp.status_code >= 400:
                    raise SoutubotError(
                        f"homepage HTTP {resp.status_code}",
                        status_code=resp.status_code,
                    )
                m_value = parse_global_m(resp.text)
            self._m = m_value
            self._m_fetched_at = time.time()
            logger.debug("[Soutubot] refreshed GLOBAL.m=%s", m_value)
            return m_value

    async def search_bytes(
        self,
        image_bytes: bytes,
        *,
        strict: bool = False,
        factor: float | None = None,
        filename: str = "",
    ) -> dict[str, Any]:
        """Search with raw image bytes. Returns API JSON dict."""
        if not image_bytes:
            raise SoutubotError("empty image bytes")
        if len(image_bytes) > 20 * 1024 * 1024:
            raise SoutubotError("image too large (>20MB)")

        factor_value = normalize_factor(strict=strict, factor=factor)
        fname, content_type = guess_filename_and_content_type(image_bytes, filename)

        last_error: Exception | None = None
        attempts = self.max_retries + 1
        for attempt in range(attempts):
            try:
                m_value = await self.refresh_m(force=attempt > 0)
                api_key = build_api_key(ua=self.user_agent, m=m_value)
                files = {
                    "file": (fname, image_bytes, content_type),
                }
                data = {"factor": str(factor_value)}
                headers = {"X-Api-Key": api_key}
                async with self._client() as client:
                    resp = await client.post(
                        "/api/search",
                        data=data,
                        files=files,
                        headers=headers,
                    )
                if resp.status_code == 401:
                    # Key expired / m stale — force refresh and retry.
                    self._m = None
                    last_error = SoutubotError(
                        "unauthorized (refresh page / sync system time)",
                        status_code=401,
                    )
                    continue
                if resp.status_code == 422:
                    detail = _safe_json_message(resp)
                    raise SoutubotError(
                        detail or "invalid image (dimensions/format rejected)",
                        status_code=422,
                    )
                if resp.status_code >= 400:
                    detail = _safe_json_message(resp) or resp.text[:200]
                    raise SoutubotError(
                        f"search HTTP {resp.status_code}: {detail}",
                        status_code=resp.status_code,
                    )
                payload = resp.json()
                if not isinstance(payload, dict):
                    raise SoutubotError(f"unexpected response type: {type(payload).__name__}")
                payload.setdefault("factor", factor_value)
                return payload
            except SoutubotError as exc:
                last_error = exc
                if exc.status_code == 401 and attempt + 1 < attempts:
                    continue
                raise
            except httpx.TimeoutException as exc:
                last_error = SoutubotError(f"search timeout: {exc}")
                if attempt + 1 < attempts:
                    await asyncio.sleep(0.4 * (attempt + 1))
                    continue
                raise last_error from exc
            except httpx.HTTPError as exc:
                last_error = SoutubotError(f"network error: {exc}")
                if attempt + 1 < attempts:
                    await asyncio.sleep(0.4 * (attempt + 1))
                    continue
                raise last_error from exc
        raise last_error or SoutubotError("search failed")

    async def search_base64(
        self,
        image_base64: str,
        *,
        strict: bool = False,
        factor: float | None = None,
        filename: str = "",
    ) -> dict[str, Any]:
        raw = (image_base64 or "").strip()
        if not raw:
            raise SoutubotError("empty image base64")
        if "," in raw and raw.lower().startswith("data:"):
            raw = raw.split(",", 1)[1].strip()
        try:
            image_bytes = base64.b64decode(raw, validate=False)
        except Exception as exc:  # noqa: BLE001
            raise SoutubotError(f"invalid base64 image: {exc}") from exc
        return await self.search_bytes(
            image_bytes,
            strict=strict,
            factor=factor,
            filename=filename,
        )

    async def get_result(self, result_id: str | int) -> dict[str, Any]:
        rid = str(result_id or "").strip()
        if not rid:
            raise SoutubotError("empty result id")
        async with self._client() as client:
            resp = await client.get(f"/api/results/{rid}")
        if resp.status_code >= 400:
            raise SoutubotError(
                f"result HTTP {resp.status_code}",
                status_code=resp.status_code,
            )
        payload = resp.json()
        if not isinstance(payload, dict):
            raise SoutubotError("unexpected result payload")
        return payload


def _safe_json_message(resp: httpx.Response) -> str:
    try:
        data = resp.json()
    except Exception:
        return ""
    if isinstance(data, dict):
        msg = data.get("message")
        if isinstance(msg, str) and msg.strip():
            return msg.strip()
        errors = data.get("errors")
        if isinstance(errors, dict):
            parts: list[str] = []
            for key, value in errors.items():
                if isinstance(value, list):
                    parts.append(f"{key}: {'; '.join(str(x) for x in value)}")
                else:
                    parts.append(f"{key}: {value}")
            return "; ".join(parts)
    return ""


def enrich_results(
    payload: dict[str, Any],
    *,
    catalog: SourceCatalog | None = None,
    min_similarity: float = 0.0,
    max_results: int = 8,
) -> list[dict[str, Any]]:
    """Normalize + enrich API result rows for LLM / chat display."""
    cat = catalog or SourceCatalog.default()
    rows = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(rows, list):
        return []

    enriched: list[dict[str, Any]] = []
    for item in rows:
        if not isinstance(item, dict):
            continue
        try:
            similarity = float(item.get("similarity") or 0.0)
        except (TypeError, ValueError):
            similarity = 0.0
        if similarity < float(min_similarity or 0.0):
            continue
        source = str(item.get("source") or "").strip().lower()
        subject_path = str(item.get("subjectPath") or "").strip()
        page_path = str(item.get("pagePath") or "").strip()
        title = str(item.get("title") or "").strip()
        language = str(item.get("language") or "").strip()
        page = item.get("page")
        try:
            page_no = int(page) if page is not None and str(page).strip() != "" else None
        except (TypeError, ValueError):
            page_no = None
        gallery_url = cat.build_url(source, subject_path)
        page_url = cat.build_url(source, page_path)
        enriched.append(
            {
                "source": source,
                "source_label": cat.label_for(source),
                "title": title,
                "language": language,
                "page": page_no,
                "similarity": round(similarity, 2),
                "gallery_url": gallery_url,
                "page_url": page_url,
                "subject_path": subject_path,
                "page_path": page_path,
                "preview_image_url": str(item.get("previewImageUrl") or "").strip(),
            }
        )
        if len(enriched) >= max(1, int(max_results or 8)):
            break
    return enriched
