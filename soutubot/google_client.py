"""Google reverse-image via SerpAPI Google Lens.

Lens needs a public image URL. Chat images are base64 → temp host → Lens.

Docs: https://serpapi.com/google-lens-api
Endpoint: https://serpapi.com/search.json?engine=google_lens&url=...
"""

from __future__ import annotations

import base64
import logging
from typing import Any
from urllib.parse import urlparse

import httpx

from .client import guess_filename_and_content_type

logger = logging.getLogger(__name__)

DEFAULT_SERPAPI_URL = "https://serpapi.com/search.json"
DEFAULT_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/126.0.0.0 Safari/537.36"
)

# Temporary public hosts for Lens (url-only API). Prefer short TTL when possible.
TEMP_HOST_ENDPOINTS = (
    {
        "name": "catbox",
        "url": "https://catbox.moe/user/api.php",
        "fields": {"reqtype": "fileupload"},
        "file_field": "fileToUpload",
    },
    {
        "name": "tmpfiles",
        "url": "https://tmpfiles.org/api/v1/upload",
        "fields": {},
        "file_field": "file",
        "json_url": True,
    },
    {
        "name": "litterbox",
        "url": "https://litterbox.catbox.moe/resources/internals/api.php",
        "fields": {"reqtype": "fileupload", "time": "1h"},
        "file_field": "fileToUpload",
    },
    {
        "name": "0x0",
        "url": "https://0x0.st",
        "fields": {},
        "file_field": "file",
    },
)


class GoogleLensError(Exception):
    def __init__(self, message: str, *, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


class GoogleLensClient:
    """SerpAPI Google Lens client (illustration source supplement)."""

    def __init__(
        self,
        *,
        api_key: str,
        endpoint: str = DEFAULT_SERPAPI_URL,
        timeout_seconds: float = 60.0,
        user_agent: str = DEFAULT_UA,
        hl: str = "zh-cn",
        country: str = "",
        search_type: str = "visual_matches",
        max_results: int = 8,
    ) -> None:
        self.api_key = (api_key or "").strip()
        self.endpoint = (endpoint or DEFAULT_SERPAPI_URL).strip()
        self.timeout_seconds = float(timeout_seconds or 60.0)
        self.user_agent = user_agent or DEFAULT_UA
        self.hl = (hl or "zh-cn").strip()
        self.country = (country or "").strip()
        self.search_type = (search_type or "visual_matches").strip() or "visual_matches"
        self.max_results = max(1, int(max_results or 8))

    def enabled(self) -> bool:
        return bool(self.api_key)

    async def upload_temp(self, image_bytes: bytes, *, filename: str = "") -> str:
        """Upload image to a temporary public host; return URL."""
        if not image_bytes:
            raise GoogleLensError("empty image bytes")
        fname, content_type = guess_filename_and_content_type(image_bytes, filename)
        last_err: Exception | None = None
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(self.timeout_seconds, connect=15.0),
            headers={"User-Agent": self.user_agent},
            follow_redirects=True,
        ) as client:
            for host in TEMP_HOST_ENDPOINTS:
                try:
                    files = {host["file_field"]: (fname, image_bytes, content_type)}
                    data = dict(host.get("fields") or {})
                    resp = await client.post(host["url"], data=data, files=files)
                    if resp.status_code >= 400:
                        last_err = GoogleLensError(
                            f"temp host {host['name']} HTTP {resp.status_code}",
                            status_code=resp.status_code,
                        )
                        continue
                    text = (resp.text or "").strip()
                    # litterbox/catbox/0x0 return plain URL
                    if text.startswith("http://") or text.startswith("https://"):
                        url = text.splitlines()[0].strip()
                        if url:
                            logger.info("[GoogleLens] temp upload via %s ok", host["name"])
                            return url
                    # tmpfiles / JSON responses
                    try:
                        payload = resp.json()
                        if isinstance(payload, dict):
                            # tmpfiles: {status,data:{url}}
                            data = payload.get("data")
                            if isinstance(data, dict) and str(data.get("url") or "").startswith("http"):
                                url = str(data["url"]).strip()
                                # direct download path for tmpfiles.org
                                if "tmpfiles.org/" in url and "/dl/" not in url:
                                    url = url.replace("tmpfiles.org/", "tmpfiles.org/dl/", 1)
                                return url
                            for key in ("url", "files"):
                                val = payload.get(key)
                                if isinstance(val, str) and val.startswith("http"):
                                    return val
                                if isinstance(val, list) and val:
                                    first = val[0]
                                    if isinstance(first, str) and first.startswith("http"):
                                        return first
                                    if isinstance(first, dict) and str(first.get("url") or "").startswith("http"):
                                        return str(first["url"])
                    except Exception:
                        pass
                    last_err = GoogleLensError(f"temp host {host['name']} unexpected response")
                except Exception as exc:  # noqa: BLE001
                    last_err = GoogleLensError(f"temp host {host['name']} error: {exc}")
                    continue
        raise last_err or GoogleLensError("all temp hosts failed")

    async def search_url(self, image_url: str) -> dict[str, Any]:
        url = (image_url or "").strip()
        if not url:
            raise GoogleLensError("empty image url")
        if not self.api_key:
            raise GoogleLensError("SerpAPI api_key 未配置")

        params: dict[str, str] = {
            "engine": "google_lens",
            "url": url,
            "api_key": self.api_key,
            "type": self.search_type,
            "hl": self.hl,
        }
        if self.country:
            params["country"] = self.country

        try:
            async with httpx.AsyncClient(
                timeout=httpx.Timeout(self.timeout_seconds, connect=15.0),
                headers={"User-Agent": self.user_agent},
                follow_redirects=True,
            ) as client:
                resp = await client.get(self.endpoint, params=params)
        except httpx.TimeoutException as exc:
            raise GoogleLensError(f"Google Lens timeout: {exc}") from exc
        except httpx.HTTPError as exc:
            raise GoogleLensError(f"Google Lens network error: {exc}") from exc

        if resp.status_code >= 400:
            raise GoogleLensError(
                f"Google Lens HTTP {resp.status_code}: {resp.text[:200]}",
                status_code=resp.status_code,
            )
        try:
            payload = resp.json()
        except Exception as exc:  # noqa: BLE001
            raise GoogleLensError(f"Google Lens invalid JSON: {exc}") from exc
        if not isinstance(payload, dict):
            raise GoogleLensError("Google Lens unexpected payload")
        if payload.get("error"):
            raise GoogleLensError(str(payload.get("error")))
        status = str((payload.get("search_metadata") or {}).get("status") or "")
        if status and status.lower() not in {"success", "processing"}:
            raise GoogleLensError(f"Google Lens status={status}")
        return payload

    async def search_bytes(self, image_bytes: bytes, *, filename: str = "") -> dict[str, Any]:
        public_url = await self.upload_temp(image_bytes, filename=filename)
        payload = await self.search_url(public_url)
        # stash temp url for debugging (not secret)
        if isinstance(payload, dict):
            payload.setdefault("_temp_image_url", public_url)
        return payload

    async def search_base64(self, image_base64: str, *, filename: str = "") -> dict[str, Any]:
        raw = (image_base64 or "").strip()
        if not raw:
            raise GoogleLensError("empty image base64")
        if "," in raw and raw.lower().startswith("data:"):
            raw = raw.split(",", 1)[1].strip()
        try:
            image_bytes = base64.b64decode(raw, validate=False)
        except Exception as exc:  # noqa: BLE001
            raise GoogleLensError(f"invalid base64: {exc}") from exc
        return await self.search_bytes(image_bytes, filename=filename)


def enrich_google_results(
    payload: dict[str, Any],
    *,
    max_results: int = 8,
) -> list[dict[str, Any]]:
    """Normalize SerpAPI Google Lens visual_matches into common rows."""
    matches = payload.get("visual_matches") if isinstance(payload, dict) else None
    if not isinstance(matches, list):
        matches = []

    out: list[dict[str, Any]] = []
    for idx, item in enumerate(matches):
        if not isinstance(item, dict):
            continue
        link = str(item.get("link") or "").strip()
        title = str(item.get("title") or "").strip()
        source = str(item.get("source") or "").strip()
        exact = bool(item.get("exact_matches"))
        # Lens does not always give similarity; approximate rank score
        position = item.get("position")
        try:
            pos_i = int(position) if position is not None else idx + 1
        except (TypeError, ValueError):
            pos_i = idx + 1
        # higher for earlier positions / exact matches
        sim = 92.0 if exact else max(40.0, 85.0 - (pos_i - 1) * 3.5)
        out.append(
            {
                "similarity": round(sim, 2),
                "score": sim,
                "title": title,
                "source": source or _domain_label(link),
                "url": link,
                "link": link,
                "thumbnail": str(item.get("thumbnail") or "").strip(),
                "exact_match": exact,
                "position": pos_i,
                "author": "",
            }
        )
        if len(out) >= max(1, int(max_results or 8)):
            break
    return out


def format_google_card(
    results: list[dict[str, Any]],
    *,
    max_lines: int = 6,
) -> str:
    lines = ["【Google 搜图结果】", "用途：插画出处补充源（SerpAPI Lens）。创作者平台优先，搬运站降权。"]
    if not results:
        lines.append("未找到可用视觉匹配。")
        return "\n".join(lines)
    for idx, item in enumerate(results[: max(1, int(max_lines or 6))], start=1):
        title = str(item.get("title") or "(无标题)").strip()
        if len(title) > 80:
            title = title[:77] + "..."
        source = item.get("source") or "?"
        sim = item.get("similarity")
        exact = " · exact" if item.get("exact_match") else ""
        url = item.get("url") or ""
        # band-ish for display
        try:
            sim_f = float(sim or 0)
        except (TypeError, ValueError):
            sim_f = 0.0
        if item.get("exact_match") or sim_f >= 80:
            band = "高"
        elif sim_f >= 60:
            band = "中"
        elif sim_f >= 50:
            band = "低"
        else:
            band = "不可靠"
        line = f"{idx}. [{sim}%|{band}]{exact} {source} | {title}"
        if url:
            line += f"\n   {url}"
        lines.append(line)
    lines.append("用法：插画优先 X/Pixiv 等创作者链；Google 作补充，勿只信搬运站。")
    return "\n".join(lines)


def _domain_label(url: str) -> str:
    try:
        host = urlparse(url).netloc.lower()
    except Exception:
        return "web"
    if host.startswith("www."):
        host = host[4:]
    return host or "web"
