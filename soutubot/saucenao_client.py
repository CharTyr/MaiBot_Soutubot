"""SauceNAO JSON API client.

Docs (beta): https://saucenao.com/user.php?page=search-api
Endpoint: POST/GET https://saucenao.com/search.php
output_type=2 → JSON
"""

from __future__ import annotations

import base64
import logging
from typing import Any, Optional

import httpx

from .client import guess_filename_and_content_type

logger = logging.getLogger(__name__)

DEFAULT_SAUCENAO_URL = "https://saucenao.com/search.php"
DEFAULT_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/126.0.0.0 Safari/537.36"
)


class SauceNaoError(Exception):
    def __init__(self, message: str, *, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


class SauceNaoClient:
    """Async SauceNAO search client."""

    def __init__(
        self,
        *,
        api_key: str,
        endpoint: str = DEFAULT_SAUCENAO_URL,
        timeout_seconds: float = 45.0,
        user_agent: str = DEFAULT_UA,
        db: int = 999,
        numres: int = 8,
        dedupe: int = 2,
        hide: int = 0,
        min_similarity: float = 50.0,
    ) -> None:
        self.api_key = (api_key or "").strip()
        self.endpoint = (endpoint or DEFAULT_SAUCENAO_URL).strip()
        self.timeout_seconds = float(timeout_seconds or 45.0)
        self.user_agent = user_agent or DEFAULT_UA
        self.db = int(db if db is not None else 999)
        self.numres = max(1, int(numres or 8))
        self.dedupe = int(dedupe if dedupe is not None else 2)
        self.hide = int(hide if hide is not None else 0)
        self.min_similarity = float(min_similarity or 0.0)

    def enabled(self) -> bool:
        return bool(self.api_key)

    def _form_fields(self) -> dict[str, str]:
        if not self.api_key:
            raise SauceNaoError("SauceNAO api_key 未配置")
        return {
            "api_key": self.api_key,
            "output_type": "2",
            "db": str(self.db),
            "numres": str(self.numres),
            "dedupe": str(self.dedupe),
            "hide": str(self.hide),
        }

    async def search_bytes(self, image_bytes: bytes, *, filename: str = "") -> dict[str, Any]:
        if not image_bytes:
            raise SauceNaoError("empty image bytes")
        if len(image_bytes) > 20 * 1024 * 1024:
            raise SauceNaoError("image too large (>20MB)")

        fname, content_type = guess_filename_and_content_type(image_bytes, filename)
        data = self._form_fields()
        files = {"file": (fname, image_bytes, content_type)}
        try:
            async with httpx.AsyncClient(
                timeout=httpx.Timeout(self.timeout_seconds, connect=15.0),
                headers={"User-Agent": self.user_agent},
                follow_redirects=True,
            ) as client:
                resp = await client.post(self.endpoint, data=data, files=files)
        except httpx.TimeoutException as exc:
            raise SauceNaoError(f"SauceNAO timeout: {exc}") from exc
        except httpx.HTTPError as exc:
            raise SauceNaoError(f"SauceNAO network error: {exc}") from exc

        if resp.status_code >= 400:
            raise SauceNaoError(
                f"SauceNAO HTTP {resp.status_code}: {resp.text[:200]}",
                status_code=resp.status_code,
            )
        try:
            payload = resp.json()
        except Exception as exc:  # noqa: BLE001
            raise SauceNaoError(f"SauceNAO invalid JSON: {exc}") from exc
        if not isinstance(payload, dict):
            raise SauceNaoError("SauceNAO unexpected payload type")

        header = payload.get("header") if isinstance(payload.get("header"), dict) else {}
        status = header.get("status")
        try:
            status_i = int(status)
        except (TypeError, ValueError):
            status_i = 0
        # status: 0 ok, >0 server error, <0 client error (bad image / out of searches)
        if status_i < 0:
            msg = str(header.get("message") or f"client error status={status_i}")
            raise SauceNaoError(msg, status_code=status_i)
        if status_i > 0:
            msg = str(header.get("message") or f"server error status={status_i}")
            raise SauceNaoError(msg, status_code=status_i)
        return payload

    async def search_base64(self, image_base64: str, *, filename: str = "") -> dict[str, Any]:
        raw = (image_base64 or "").strip()
        if not raw:
            raise SauceNaoError("empty image base64")
        if "," in raw and raw.lower().startswith("data:"):
            raw = raw.split(",", 1)[1].strip()
        try:
            image_bytes = base64.b64decode(raw, validate=False)
        except Exception as exc:  # noqa: BLE001
            raise SauceNaoError(f"invalid base64: {exc}") from exc
        return await self.search_bytes(image_bytes, filename=filename)

    async def search_url(self, image_url: str) -> dict[str, Any]:
        url = (image_url or "").strip()
        if not url:
            raise SauceNaoError("empty image url")
        params = self._form_fields()
        params["url"] = url
        try:
            async with httpx.AsyncClient(
                timeout=httpx.Timeout(self.timeout_seconds, connect=15.0),
                headers={"User-Agent": self.user_agent},
                follow_redirects=True,
            ) as client:
                resp = await client.get(self.endpoint, params=params)
        except httpx.TimeoutException as exc:
            raise SauceNaoError(f"SauceNAO timeout: {exc}") from exc
        except httpx.HTTPError as exc:
            raise SauceNaoError(f"SauceNAO network error: {exc}") from exc
        if resp.status_code >= 400:
            raise SauceNaoError(
                f"SauceNAO HTTP {resp.status_code}: {resp.text[:200]}",
                status_code=resp.status_code,
            )
        payload = resp.json()
        if not isinstance(payload, dict):
            raise SauceNaoError("SauceNAO unexpected payload type")
        header = payload.get("header") if isinstance(payload.get("header"), dict) else {}
        try:
            status_i = int(header.get("status") or 0)
        except (TypeError, ValueError):
            status_i = 0
        if status_i != 0:
            raise SauceNaoError(
                str(header.get("message") or f"status={status_i}"),
                status_code=status_i,
            )
        return payload


def enrich_saucenao_results(
    payload: dict[str, Any],
    *,
    min_similarity: float = 50.0,
    max_results: int = 8,
    include_hidden: bool = True,
) -> list[dict[str, Any]]:
    """Normalize SauceNAO JSON results."""
    header = payload.get("header") if isinstance(payload, dict) else {}
    if not isinstance(header, dict):
        header = {}
    try:
        header_min = float(header.get("minimum_similarity") or 0)
    except (TypeError, ValueError):
        header_min = 0.0
    threshold = max(float(min_similarity or 0), 0.0)

    rows = payload.get("results") if isinstance(payload, dict) else None
    if not isinstance(rows, list):
        return []

    enriched: list[dict[str, Any]] = []
    for item in rows:
        if not isinstance(item, dict):
            continue
        h = item.get("header") if isinstance(item.get("header"), dict) else {}
        d = item.get("data") if isinstance(item.get("data"), dict) else {}
        try:
            sim = float(h.get("similarity") or 0.0)
        except (TypeError, ValueError):
            sim = 0.0
        if sim < threshold:
            continue
        try:
            hidden = int(h.get("hidden") or 0)
        except (TypeError, ValueError):
            hidden = 0
        if hidden and not include_hidden:
            continue

        urls = d.get("ext_urls") if isinstance(d.get("ext_urls"), list) else []
        urls = [str(u).strip() for u in urls if str(u).strip()]
        title = _first_str(
            d,
            (
                "title",
                "source",
                "eng_name",
                "jp_name",
                "material",
                "member_name",
                "author_name",
                "creator",
            ),
        )
        author = _first_str(
            d,
            (
                "member_name",
                "author_name",
                "creator",
                "author",
                "company",
            ),
        )
        if isinstance(d.get("creator"), list):
            author = ", ".join(str(x) for x in d.get("creator") if x) or author

        index_name = str(h.get("index_name") or "").strip()
        index_id = h.get("index_id")
        thumbnail = str(h.get("thumbnail") or "").strip()
        # Extra ids for common indexes
        extra_bits: list[str] = []
        for key in (
            "pixiv_id",
            "danbooru_id",
            "gelbooru_id",
            "yandere_id",
            "sankaku_id",
            "anime-db_id",
            "anidb_aid",
            "mal_id",
            "part",
            "year",
            "est_time",
        ):
            if d.get(key) not in (None, ""):
                extra_bits.append(f"{key}={d.get(key)}")

        enriched.append(
            {
                "similarity": round(sim, 2),
                "index_id": index_id,
                "index_name": index_name,
                "title": title,
                "author": author,
                "urls": urls,
                "url": urls[0] if urls else "",
                "thumbnail": thumbnail,
                "hidden": hidden,
                "below_header_min": bool(header_min and sim < header_min),
                "extra": ", ".join(extra_bits),
                "raw_data_keys": sorted(str(k) for k in d.keys()),
            }
        )
        if len(enriched) >= max(1, int(max_results or 8)):
            break
    return enriched


def format_saucenao_card(
    results: list[dict[str, Any]],
    *,
    header: dict[str, Any] | None = None,
    min_similarity: float = 50.0,
    max_lines: int = 8,
) -> str:
    header = header or {}
    lines: list[str] = ["【SauceNAO 搜图结果】"]
    meta: list[str] = []
    if header.get("short_remaining") is not None or header.get("long_remaining") is not None:
        meta.append(
            f"配额 short={header.get('short_remaining')}/long={header.get('long_remaining')}"
        )
    if header.get("minimum_similarity") is not None:
        meta.append(f"站点阈值≈{header.get('minimum_similarity')}")
    if header.get("results_returned") is not None:
        meta.append(f"返回={header.get('results_returned')}")
    if meta:
        lines.append(" · ".join(str(x) for x in meta))

    if not results:
        lines.append(
            f"未找到 ≥{min_similarity}% 的可靠匹配。"
            "可换更清晰原图，或降低 min_similarity。"
        )
        return "\n".join(lines)

    for idx, item in enumerate(results[: max(1, int(max_lines or 8))], start=1):
        sim = item.get("similarity")
        index_name = item.get("index_name") or f"index {item.get('index_id')}"
        title = str(item.get("title") or "(无标题)").strip()
        if len(title) > 90:
            title = title[:87] + "..."
        author = str(item.get("author") or "").strip()
        url = item.get("url") or ""
        warn = " ⚠低可信" if item.get("below_header_min") else ""
        # explicit band for planner ranking
        try:
            sim_f = float(sim or 0)
        except (TypeError, ValueError):
            sim_f = 0.0
        if item.get("below_header_min"):
            band = "不可靠"
        elif sim_f >= 80:
            band = "高"
        elif sim_f >= 60:
            band = "中"
        elif sim_f >= 50:
            band = "低"
        else:
            band = "不可靠"
        line = f"{idx}. [{sim}%|{band}]{warn} {index_name}"
        if title:
            line += f" | {title}"
        if author and author not in title:
            line += f" / {author}"
        if url:
            line += f"\n   {url}"
        # secondary urls
        urls = item.get("urls") or []
        for extra_url in urls[1:3]:
            if extra_url and extra_url != url:
                line += f"\n   {extra_url}"
        lines.append(line)

    lines.append(
        "用法：优先采用高相似度且链接可打开的结果；"
        "说明来源站/作者/作品名；不要编造未返回的信息。"
    )
    return "\n".join(lines)


def _first_str(data: dict[str, Any], keys: tuple[str, ...]) -> str:
    for key in keys:
        value = data.get(key)
        if value is None:
            continue
        if isinstance(value, list):
            text = ", ".join(str(x).strip() for x in value if str(x).strip())
            if text:
                return text
            continue
        text = str(value).strip()
        if text:
            return text
    return ""
