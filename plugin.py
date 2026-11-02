"""MaiBot 搜图插件：给 Planner 的以图搜源工具。

分工：
- Soutubot：漫画/本子/页局部（主 nHentai 系）
- SauceNAO：插画出处（Pixiv / Booru / X 等；创作者平台优先）

主工具：
- search_image：按意图路由；默认 auto 双引擎

后备：
- search_soutubot / search_saucenao

可选命令：/soutu /sauce /soutu_status
"""

from __future__ import annotations

import asyncio
import base64
import logging
import time
from typing import Any, Optional, Set

from maibot_sdk import Command, Field, MaiBotPlugin, PluginConfigBase, Tool
from maibot_sdk.types import ToolParameterInfo, ToolParamType

from .soutubot.client import (
    SOURCE_DEFAULT_HOSTS,
    SoutubotClient,
    SoutubotError,
    SourceCatalog,
    enrich_results,
)
from .soutubot.format import format_search_card
from .soutubot.image_utils import (
    extract_images_from_message,
    extract_message_dict,
    iter_component_dicts,
    peel_envelope,
    project_root_from_plugin_file,
)
from .soutubot.saucenao_client import (
    SauceNaoClient,
    SauceNaoError,
    enrich_saucenao_results,
    format_saucenao_card,
)
from .soutubot.google_client import (
    GoogleLensClient,
    GoogleLensError,
    enrich_google_results,
    format_google_card,
)
from .soutubot.verdict import format_verdict_block

logger = logging.getLogger(__name__)

PLUGIN_ID = "chartyr.soutubot"
PLUGIN_VERSION = "0.3.0"


class PluginSectionConfig(PluginConfigBase):
    __ui_label__ = "插件"
    __ui_icon__ = "package"
    __ui_order__ = 0

    enabled: bool = Field(default=True, description="是否启用插件")
    config_version: str = Field(default=PLUGIN_VERSION, description="配置版本")


class SearchSectionConfig(PluginConfigBase):
    __ui_label__ = "Soutubot"
    __ui_icon__ = "search"
    __ui_order__ = 1

    base_url: str = Field(default="https://soutubot.moe", description="Soutubot 站点根 URL")
    timeout_seconds: int = Field(default=60, description="HTTP 超时秒数")
    max_retries: int = Field(default=1, description="401/网络错误重试次数")
    default_strict: bool = Field(default=False, description="默认是否严格模式（factor 1.4）")
    min_similarity: float = Field(default=28.0, description="结果卡折叠阈值；SPA 默认 28")
    max_results: int = Field(default=6, description="返回给 Planner / 聊天的最大结果条数")
    user_agent: str = Field(
        default=(
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/126.0.0.0 Safari/537.36"
        ),
        description="请求 User-Agent（Soutubot 的 X-Api-Key 依赖它，勿乱改）",
    )
    cooldown_seconds: int = Field(default=8, description="同 stream 冷却秒数，防刷")
    send_preview: bool = Field(
        default=False,
        description="命令路径是否额外发送首条结果预览图（耗流量，默认关）",
    )


class SauceNaoSectionConfig(PluginConfigBase):
    __ui_label__ = "SauceNAO"
    __ui_icon__ = "image"
    __ui_order__ = 2

    enabled: bool = Field(default=True, description="是否启用 SauceNAO")
    api_key: str = Field(default="", description="SauceNAO API Key（勿提交公开仓库）")
    endpoint: str = Field(
        default="https://saucenao.com/search.php",
        description="SauceNAO search.php 地址",
    )
    timeout_seconds: int = Field(default=45, description="HTTP 超时秒数")
    db: int = Field(default=999, description="数据库：999=全部；或具体 index 编号")
    numres: int = Field(default=8, description="请求结果数量")
    dedupe: int = Field(default=2, description="0/1/2 去重级别，默认 2")
    hide: int = Field(default=0, description="0 显示全部；1-3 更强隐藏（API 仍返回 hidden 字段）")
    min_similarity: float = Field(default=50.0, description="结果卡主列表阈值")
    max_results: int = Field(default=6, description="返回条数上限")
    include_hidden: bool = Field(default=True, description="是否包含 hidden=1 的结果")


class GoogleSectionConfig(PluginConfigBase):
    __ui_label__ = "Google 搜图"
    __ui_icon__ = "globe"
    __ui_order__ = 3

    enabled: bool = Field(default=True, description="是否启用 Google Lens（经 SerpAPI）")
    api_key: str = Field(default="", description="SerpAPI API Key（勿提交公开仓库）")
    endpoint: str = Field(
        default="https://serpapi.com/search.json",
        description="SerpAPI search.json 地址",
    )
    timeout_seconds: int = Field(default=60, description="HTTP 超时秒数（含临时图床上传）")
    hl: str = Field(default="zh-cn", description="Lens 界面语言")
    country: str = Field(default="", description="可选国家代码，如 us / jp")
    search_type: str = Field(
        default="visual_matches",
        description="Lens type：visual_matches / exact_matches / all",
    )
    max_results: int = Field(default=6, description="返回条数上限")
    with_illustration: bool = Field(
        default=True,
        description="插画意图时是否默认并联 Google（与 SauceNAO 一起）",
    )


class HostSectionConfig(PluginConfigBase):
    __ui_label__ = "Soutubot 来源站"
    __ui_icon__ = "link"
    __ui_order__ = 4

    nhentai_host: str = Field(default="nhentai.net", description="nHentai 域名")
    ehentai_host: str = Field(default="e-hentai.org", description="E-Hentai 域名（可改 exhentai.org）")
    panda_host: str = Field(default="panda.chaika.moe", description="Panda Backup 域名")


class AdminSectionConfig(PluginConfigBase):
    __ui_label__ = "管理员"
    __ui_icon__ = "shield"
    __ui_order__ = 5

    admin_users: list[str] = Field(
        default_factory=list,
        description="允许 /soutu_status 的账号，格式 platform:id；空=不鉴权",
    )


class SoutubotPluginConfig(PluginConfigBase):
    plugin: PluginSectionConfig = Field(default_factory=PluginSectionConfig)
    search: SearchSectionConfig = Field(default_factory=SearchSectionConfig)
    saucenao: SauceNaoSectionConfig = Field(default_factory=SauceNaoSectionConfig)
    google: GoogleSectionConfig = Field(default_factory=GoogleSectionConfig)
    hosts: HostSectionConfig = Field(default_factory=HostSectionConfig)
    admin: AdminSectionConfig = Field(default_factory=AdminSectionConfig)


def _tool_param(
    name: str,
    param_type: ToolParamType,
    description: str,
    required: bool = False,
) -> ToolParameterInfo:
    return ToolParameterInfo(
        name=name,
        param_type=param_type,
        description=description,
        required=required,
    )


def extract_command_actor(kwargs: dict | None) -> tuple[str, str]:
    raw = kwargs or {}
    message = raw.get("message") or {}
    if not isinstance(message, dict):
        message = {}
    platform = str(message.get("platform") or raw.get("platform") or "").strip()
    user_info = message.get("user_info")
    if not isinstance(user_info, dict) or not str(user_info.get("user_id") or "").strip():
        message_info = message.get("message_info") or {}
        if isinstance(message_info, dict):
            user_info = message_info.get("user_info") or {}
        else:
            user_info = {}
    if not isinstance(user_info, dict):
        user_info = {}
    user_id = str(user_info.get("user_id") or raw.get("user_id") or "").strip()
    return platform, user_id


def _match_admin(platform: str, user_id: str, admin_users: list[str]) -> bool:
    if not admin_users:
        return True
    if not user_id:
        return False
    full = f"{platform}:{user_id}" if platform else user_id
    for item in admin_users:
        item = str(item or "").strip()
        if not item:
            continue
        if item == full or item == user_id or item.endswith(f":{user_id}"):
            return True
    return False


def _extract_stream_id(kwargs: dict[str, Any], stream_id: str = "") -> str:
    if stream_id and str(stream_id).strip():
        return str(stream_id).strip()
    for key in ("stream_id", "chat_id", "chat_stream_id", "session_id"):
        val = kwargs.get(key)
        if val:
            return str(val).strip()
    message = kwargs.get("message")
    if isinstance(message, dict):
        for key in ("stream_id", "chat_id", "session_id"):
            if message.get(key):
                return str(message.get(key)).strip()
        info = message.get("message_info") or {}
        if isinstance(info, dict):
            for key in ("stream_id", "chat_id"):
                if info.get(key):
                    return str(info.get(key)).strip()
    return ""


def _as_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return False
    text = str(value).strip().lower()
    return text in {"1", "true", "yes", "y", "on", "strict", "严格"}


def _mask_key(key: str) -> str:
    text = str(key or "").strip()
    if not text:
        return "(empty)"
    if len(text) <= 8:
        return text[:2] + "***"
    return text[:4] + "…" + text[-4:]


class SoutubotPlugin(MaiBotPlugin):
    """Soutubot + SauceNAO + Google Lens 搜图插件。"""

    config_model = SoutubotPluginConfig

    def __init__(self) -> None:
        super().__init__()
        self._client: Optional[SoutubotClient] = None
        self._saucenao: Optional[SauceNaoClient] = None
        self._google: Optional[GoogleLensClient] = None
        self._cooldown: dict[str, float] = {}
        self._bg_tasks: Set[asyncio.Task] = set()

    # ------------------------------------------------------------------
    # lifecycle
    # ------------------------------------------------------------------

    async def on_load(self) -> None:
        self._rebuild_client()
        sn = self._saucenao_cfg()
        gg = self._google_cfg()
        logger.info(
            "[Soutubot] loaded v%s saucenao=%s google=%s",
            PLUGIN_VERSION,
            getattr(sn, "enabled", False) and bool(getattr(sn, "api_key", "")),
            getattr(gg, "enabled", False) and bool(getattr(gg, "api_key", "")),
        )

    async def on_unload(self) -> None:
        for task in list(self._bg_tasks):
            if not task.done():
                task.cancel()
        self._bg_tasks.clear()
        self._client = None
        self._saucenao = None
        self._google = None
        logger.info("[Soutubot] unloaded")

    async def on_config_update(self, scope: str, config_data: dict[str, Any], version: str) -> None:
        del scope, config_data, version
        self._rebuild_client()
        logger.info("[Soutubot] config updated")

    def get_components(self) -> list[dict[str, Any]]:
        """Planner 默认只常驻 search_image；专用引擎工具 deferred 减噪音。"""
        components = super().get_components()
        for comp in components:
            name = str(comp.get("name") or "")
            metadata = comp.get("metadata")
            if not isinstance(metadata, dict):
                metadata = {}
                comp["metadata"] = metadata
            if name == "search_image":
                metadata["visibility"] = "visible"
            elif name in {"search_soutubot", "search_saucenao", "search_google"}:
                metadata.setdefault("visibility", "deferred")
        return components

    def _search_cfg(self) -> SearchSectionConfig:
        try:
            return self.config.search
        except Exception:
            return SearchSectionConfig()

    def _saucenao_cfg(self) -> SauceNaoSectionConfig:
        try:
            return self.config.saucenao
        except Exception:
            return SauceNaoSectionConfig()

    def _google_cfg(self) -> GoogleSectionConfig:
        try:
            return self.config.google
        except Exception:
            return GoogleSectionConfig()

    def _rebuild_client(self) -> None:
        cfg = self._search_cfg()
        hosts = getattr(self.config, "hosts", HostSectionConfig())
        catalog = SourceCatalog(
            hosts={
                "nhentai": getattr(hosts, "nhentai_host", None) or SOURCE_DEFAULT_HOSTS["nhentai"],
                "ehentai": getattr(hosts, "ehentai_host", None) or SOURCE_DEFAULT_HOSTS["ehentai"],
                "panda": getattr(hosts, "panda_host", None) or SOURCE_DEFAULT_HOSTS["panda"],
            },
            labels={
                "nhentai": "nHentai",
                "ehentai": "E-Hentai",
                "panda": "Panda Backup",
            },
        )
        self._client = SoutubotClient(
            base_url=cfg.base_url or "https://soutubot.moe",
            user_agent=cfg.user_agent,
            timeout_seconds=float(cfg.timeout_seconds or 60),
            max_retries=int(cfg.max_retries or 0),
            catalog=catalog,
        )

        sn = self._saucenao_cfg()
        if getattr(sn, "enabled", True) and str(getattr(sn, "api_key", "") or "").strip():
            self._saucenao = SauceNaoClient(
                api_key=str(sn.api_key).strip(),
                endpoint=str(getattr(sn, "endpoint", "") or "https://saucenao.com/search.php"),
                timeout_seconds=float(getattr(sn, "timeout_seconds", 45) or 45),
                user_agent=cfg.user_agent,
                db=int(getattr(sn, "db", 999) or 999),
                numres=int(getattr(sn, "numres", 8) or 8),
                dedupe=int(getattr(sn, "dedupe", 2) if getattr(sn, "dedupe", 2) is not None else 2),
                hide=int(getattr(sn, "hide", 0) or 0),
                min_similarity=float(getattr(sn, "min_similarity", 50) or 50),
            )
        else:
            self._saucenao = None

        gg = self._google_cfg()
        if getattr(gg, "enabled", True) and str(getattr(gg, "api_key", "") or "").strip():
            self._google = GoogleLensClient(
                api_key=str(gg.api_key).strip(),
                endpoint=str(getattr(gg, "endpoint", "") or "https://serpapi.com/search.json"),
                timeout_seconds=float(getattr(gg, "timeout_seconds", 60) or 60),
                user_agent=cfg.user_agent,
                hl=str(getattr(gg, "hl", "zh-cn") or "zh-cn"),
                country=str(getattr(gg, "country", "") or ""),
                search_type=str(getattr(gg, "search_type", "visual_matches") or "visual_matches"),
                max_results=int(getattr(gg, "max_results", 6) or 6),
            )
        else:
            self._google = None

    def _spawn(self, coro: Any) -> None:
        task = asyncio.create_task(coro)
        self._bg_tasks.add(task)
        task.add_done_callback(self._bg_tasks.discard)

    def _check_cooldown(self, stream_id: str) -> tuple[bool, float]:
        cfg = self._search_cfg()
        cd = float(cfg.cooldown_seconds or 0)
        if cd <= 0 or not stream_id:
            return True, 0.0
        now = time.time()
        last = self._cooldown.get(stream_id, 0.0)
        remain = cd - (now - last)
        if remain > 0:
            return False, remain
        return True, 0.0

    def _mark_cooldown(self, stream_id: str) -> None:
        if stream_id:
            self._cooldown[stream_id] = time.time()

    # ------------------------------------------------------------------
    # message / image
    # ------------------------------------------------------------------

    async def _get_message_dict(
        self,
        message_id: str,
        stream_id: str = "",
        *,
        include_binary: bool = True,
    ) -> dict[str, Any] | None:
        mid = str(message_id or "").strip()
        if not mid:
            return None
        try:
            raw = await self.ctx.call_capability(
                "message.get_by_id",
                message_id=mid,
                chat_id=stream_id or "",
                include_binary_data=include_binary,
            )
        except TypeError:
            try:
                raw = await self.ctx.call_capability(
                    "message.get_by_id",
                    message_id=mid,
                    chat_id=stream_id or "",
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning("[Soutubot] message.get_by_id failed: %s", exc)
                raw = None
        except Exception as exc:  # noqa: BLE001
            logger.warning("[Soutubot] message.get_by_id failed: %s", exc)
            raw = None

        msg = extract_message_dict(raw)
        if msg:
            return msg

        try:
            try:
                api_id: Any = int(mid)
            except ValueError:
                api_id = mid
            raw = await self.ctx.call_capability(
                "api.call",
                api_name="adapter.napcat.message.get_msg",
                version="1",
                args={"message_id": api_id},
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("[Soutubot] QQ get_msg failed: %s", exc)
            return None

        payload = peel_envelope(raw)
        if not isinstance(payload, dict):
            return None
        if payload.get("success") is False:
            return None
        for key in ("result", "data"):
            nested = payload.get(key)
            if isinstance(nested, dict) and (
                "message" in nested or "raw_message" in nested or "message_id" in nested
            ):
                payload = nested
                break
        if isinstance(payload.get("data"), dict):
            data = payload["data"]
            if "message" in data or "raw_message" in data:
                payload = data
        return payload if isinstance(payload, dict) else None

    def _images_from_message(self, message: dict[str, Any]) -> list[dict[str, str]]:
        root = project_root_from_plugin_file(__file__)
        return extract_images_from_message(message, max_count=1, project_root=root)

    async def _resolve_image_from_msg_id(
        self,
        msg_id: str,
        stream_id: str = "",
    ) -> tuple[str, str]:
        message = await self._get_message_dict(msg_id, stream_id)
        if not message:
            return "", f"未找到消息 {msg_id}"
        images = self._images_from_message(message)
        if not images:
            return "", f"消息 {msg_id} 中没有可解码的图片"
        return images[0]["base64"], ""

    async def _resolve_image_from_command_kwargs(
        self,
        kwargs: dict[str, Any],
    ) -> tuple[str, str]:
        stream_id = _extract_stream_id(kwargs)
        message = kwargs.get("message")
        message_dict: dict[str, Any] | None = None
        if isinstance(message, dict):
            message_dict = message
        else:
            mid = str(kwargs.get("message_id") or "").strip()
            if mid:
                message_dict = await self._get_message_dict(mid, stream_id)

        if message_dict:
            images = self._images_from_message(message_dict)
            if images:
                return images[0]["base64"], ""

            reply_ids = self._collect_reply_ids(message_dict)
            for rid in reply_ids:
                b64, err = await self._resolve_image_from_msg_id(rid, stream_id)
                if b64:
                    return b64, ""
                logger.info("[Soutubot] reply image miss: %s %s", rid, err)

        return "", "请发送图片，或回复一张图片后再发命令"

    def _collect_reply_ids(self, message: dict[str, Any]) -> list[str]:
        ids: list[str] = []
        seen: set[str] = set()

        def add(value: Any) -> None:
            text = str(value or "").strip()
            if text and text not in seen:
                seen.add(text)
                ids.append(text)

        for key in ("reply_to", "reply_id", "reply_message_id"):
            add(message.get(key))
        info = message.get("message_info")
        if isinstance(info, dict):
            for key in ("reply_to", "reply_id"):
                add(info.get(key))

        for source in (message.get("raw_message"), message.get("message")):
            for component in iter_component_dicts(source):
                ctype = str(component.get("type") or "").lower()
                if ctype not in {"reply", "quote"}:
                    continue
                data = component.get("data") if isinstance(component.get("data"), dict) else {}
                add(
                    component.get("target_message_id")
                    or component.get("id")
                    or component.get("message_id")
                    or data.get("target_message_id")
                    or data.get("id")
                    or data.get("message_id")
                )
        return ids

    # ------------------------------------------------------------------
    # search core
    # ------------------------------------------------------------------

    async def _run_soutubot(
        self,
        image_base64: str,
        *,
        strict: bool = False,
    ) -> tuple[bool, str, dict[str, Any]]:
        if not self._client:
            self._rebuild_client()
        assert self._client is not None
        cfg = self._search_cfg()
        try:
            payload = await self._client.search_base64(image_base64, strict=strict)
        except SoutubotError as exc:
            return False, f"Soutubot 搜索失败：{exc}", {}
        except Exception as exc:  # noqa: BLE001
            logger.exception("[Soutubot] unexpected search error")
            return False, f"Soutubot 搜索异常：{exc}", {}

        results = enrich_results(
            payload,
            catalog=self._client.catalog,
            min_similarity=0.0,
            max_results=int(cfg.max_results or 6),
        )
        card = format_search_card(
            results,
            result_id=payload.get("id"),
            execution_time=payload.get("executionTime"),
            factor=payload.get("factor"),
            min_similarity=float(cfg.min_similarity or 28),
            max_lines=int(cfg.max_results or 6),
        )
        meta = {
            "engine": "soutubot",
            "result_id": payload.get("id"),
            "execution_time": payload.get("executionTime"),
            "factor": payload.get("factor"),
            "count": len(results),
            "top_similarity": results[0]["similarity"] if results else 0,
            "results": results,
            "card": card,
            "preview_image_url": (results[0].get("preview_image_url") if results else ""),
        }
        return True, card, meta

    async def _run_saucenao(
        self,
        image_base64: str,
    ) -> tuple[bool, str, dict[str, Any]]:
        sn_cfg = self._saucenao_cfg()
        if not getattr(sn_cfg, "enabled", True):
            return False, "SauceNAO 已在配置中禁用", {}
        if not self._saucenao:
            self._rebuild_client()
        if not self._saucenao:
            return False, "SauceNAO 未配置 api_key", {}

        try:
            payload = await self._saucenao.search_base64(image_base64)
        except SauceNaoError as exc:
            return False, f"SauceNAO 搜索失败：{exc}", {}
        except Exception as exc:  # noqa: BLE001
            logger.exception("[SauceNAO] unexpected search error")
            return False, f"SauceNAO 搜索异常：{exc}", {}

        header = payload.get("header") if isinstance(payload.get("header"), dict) else {}
        results = enrich_saucenao_results(
            payload,
            min_similarity=float(getattr(sn_cfg, "min_similarity", 50) or 50),
            max_results=int(getattr(sn_cfg, "max_results", 6) or 6),
            include_hidden=bool(getattr(sn_cfg, "include_hidden", True)),
        )
        # if filtered empty but raw has results, show top raw under threshold note
        if not results:
            results = enrich_saucenao_results(
                payload,
                min_similarity=0.0,
                max_results=int(getattr(sn_cfg, "max_results", 6) or 6),
                include_hidden=bool(getattr(sn_cfg, "include_hidden", True)),
            )
            # keep only top few and let card explain threshold
            card = format_saucenao_card(
                results[: max(1, int(getattr(sn_cfg, "max_results", 6) or 6))],
                header=header,
                min_similarity=float(getattr(sn_cfg, "min_similarity", 50) or 50),
                max_lines=int(getattr(sn_cfg, "max_results", 6) or 6),
            )
        else:
            card = format_saucenao_card(
                results,
                header=header,
                min_similarity=float(getattr(sn_cfg, "min_similarity", 50) or 50),
                max_lines=int(getattr(sn_cfg, "max_results", 6) or 6),
            )

        meta = {
            "engine": "saucenao",
            "count": len(results),
            "top_similarity": results[0]["similarity"] if results else 0,
            "results": results,
            "card": card,
            "header": {
                "short_remaining": header.get("short_remaining"),
                "long_remaining": header.get("long_remaining"),
                "minimum_similarity": header.get("minimum_similarity"),
                "results_returned": header.get("results_returned"),
                "user_id": header.get("user_id"),
                "account_type": header.get("account_type"),
            },
            "preview_image_url": (results[0].get("thumbnail") if results else ""),
        }
        return True, card, meta

    async def _run_google(
        self,
        image_base64: str,
    ) -> tuple[bool, str, dict[str, Any]]:
        gg_cfg = self._google_cfg()
        if not getattr(gg_cfg, "enabled", True):
            return False, "Google 搜图已在配置中禁用", {}
        if not self._google:
            self._rebuild_client()
        if not self._google:
            return False, "Google 搜图未配置 SerpAPI api_key", {}

        try:
            payload = await self._google.search_base64(image_base64)
        except GoogleLensError as exc:
            return False, f"Google 搜图失败：{exc}", {}
        except Exception as exc:  # noqa: BLE001
            logger.exception("[GoogleLens] unexpected search error")
            return False, f"Google 搜图异常：{exc}", {}

        results = enrich_google_results(
            payload,
            max_results=int(getattr(gg_cfg, "max_results", 6) or 6),
        )
        card = format_google_card(
            results,
            max_lines=int(getattr(gg_cfg, "max_results", 6) or 6),
        )
        meta = {
            "engine": "google",
            "count": len(results),
            "top_similarity": results[0]["similarity"] if results else 0,
            "results": results,
            "card": card,
            "temp_image_url": payload.get("_temp_image_url") if isinstance(payload, dict) else "",
            "preview_image_url": (results[0].get("thumbnail") if results else ""),
        }
        return True, card, meta

    async def _run_both(
        self,
        image_base64: str,
        *,
        strict: bool = False,
        engines: str = "both",
        intent: str = "auto",
    ) -> tuple[bool, str, dict[str, Any]]:
        mode = str(engines or "both").strip().lower()
        intent_n = str(intent or "auto").strip().lower()
        gg_cfg = self._google_cfg()

        # Product routing:
        # - manga/comic/page crop → Soutubot
        # - illustration → SauceNAO (+ Google if configured)
        # - both/auto → both manga+illust engines, verdict decides
        if mode in {"auto", ""}:
            if intent_n in {"manga", "doujin", "comic", "本子", "漫画", "页", "局部"}:
                mode = "soutubot"
            elif intent_n in {"illustration", "illust", "pixiv", "插画", "同人图", "画师"}:
                mode = "illustration"
            else:
                mode = "both"

        want_soutu = mode in {"both", "all", "soutubot", "soutu", "doujin", "本子", "漫画"}
        want_sauce = mode in {
            "both",
            "all",
            "saucenao",
            "sauce",
            "nao",
            "插画",
            "illustration",
            "illust",
        }
        want_google = mode in {
            "google",
            "lens",
            "google_lens",
            "illustration",
            "illust",
            "both",
            "all",
            "插画",
        }
        # Google only as illustration supplement
        if want_google:
            if not getattr(gg_cfg, "with_illustration", True) and mode in {"both", "all", "illustration", "illust", "插画"}:
                want_google = False
            if mode in {"soutubot", "soutu", "doujin", "本子", "漫画"}:
                want_google = False
            if not self._google and not (
                getattr(gg_cfg, "enabled", False) and str(getattr(gg_cfg, "api_key", "") or "").strip()
            ):
                want_google = False

        tasks: dict[str, Any] = {}
        if want_soutu:
            tasks["soutubot"] = asyncio.create_task(self._run_soutubot(image_base64, strict=strict))
        if want_sauce:
            tasks["saucenao"] = asyncio.create_task(self._run_saucenao(image_base64))
        if want_google:
            tasks["google"] = asyncio.create_task(self._run_google(image_base64))

        if not tasks:
            return False, "未选择可用搜图引擎（检查 SauceNAO/Google 配置）", {}

        parts: list[str] = []
        meta: dict[str, Any] = {"engines": {}, "intent": intent_n, "route": mode}
        any_ok = False
        top = 0.0
        sauce_results: list[dict[str, Any]] = []
        soutu_results: list[dict[str, Any]] = []
        google_results: list[dict[str, Any]] = []

        for name, task in tasks.items():
            ok, card, engine_meta = await task
            meta["engines"][name] = {
                "ok": ok,
                "top_similarity": engine_meta.get("top_similarity", 0),
                "count": engine_meta.get("count", 0),
            }
            if ok:
                any_ok = True
                try:
                    top = max(top, float(engine_meta.get("top_similarity") or 0))
                except (TypeError, ValueError):
                    pass
                results = engine_meta.get("results") or []
                if name == "saucenao" and isinstance(results, list):
                    sauce_results = results
                if name == "soutubot" and isinstance(results, list):
                    soutu_results = results
                if name == "google" and isinstance(results, list):
                    google_results = results
            parts.append(card if card else f"{name} 失败")

        verdict_intent = intent_n if intent_n not in {"", "auto"} else (
            "manga"
            if mode in {"soutubot", "soutu"}
            else "illustration"
            if mode in {"saucenao", "illustration", "google", "illust"}
            else "auto"
        )
        verdict = ""
        if any_ok and (sauce_results or soutu_results or google_results):
            verdict = format_verdict_block(
                saucenao_results=sauce_results,
                soutubot_results=soutu_results,
                google_results=google_results,
                intent=verdict_intent,
            )
            joined = verdict + "\n\n" + "\n\n".join(parts)
        else:
            joined = "\n\n".join(parts)

        meta["top_similarity"] = top
        meta["card"] = joined
        meta["verdict"] = verdict
        meta["results_saucenao"] = sauce_results
        meta["results_soutubot"] = soutu_results
        meta["results_google"] = google_results
        return any_ok, joined, meta

    async def _send_text(self, text: str, stream_id: str) -> None:
        if not stream_id or not text:
            return
        try:
            await self.ctx.send.text(text=text, stream_id=stream_id)
        except TypeError:
            await self.ctx.send.text(text, stream_id)

    async def _maybe_send_preview(self, meta: dict[str, Any], stream_id: str) -> None:
        cfg = self._search_cfg()
        if not cfg.send_preview or not stream_id:
            return
        url = str(meta.get("preview_image_url") or "").strip()
        if not url:
            results = meta.get("results") or []
            if results:
                url = str(
                    results[0].get("preview_image_url")
                    or results[0].get("thumbnail")
                    or ""
                ).strip()
        if not url:
            return
        try:
            import httpx

            async with httpx.AsyncClient(timeout=20.0, follow_redirects=True) as client:
                resp = await client.get(url)
                if resp.status_code >= 400 or not resp.content:
                    return
                b64 = base64.b64encode(resp.content).decode("ascii")
            await self.ctx.send.image(b64, stream_id)
        except Exception as exc:  # noqa: BLE001
            logger.warning("[Soutubot] preview send failed: %s", exc)

    # ------------------------------------------------------------------
    # Tools
    # ------------------------------------------------------------------

    def _tool_gate(
        self,
        msg_id: str,
        stream_id: str,
        kwargs: dict[str, Any],
    ) -> tuple[str, str, str | None]:
        """返回 (mid, sid, error)。error 非空表示应直接失败返回。"""
        if not getattr(self.config.plugin, "enabled", True):
            return "", "", "搜图插件已禁用"
        mid = str(msg_id or "").strip()
        if not mid:
            return "", "", (
                "缺少 msg_id。请传入聊天记录里【含图片】那条消息的 message_id；"
                "不要传纯文字消息 id。"
            )
        sid = _extract_stream_id(kwargs, stream_id)
        ok_cd, remain = self._check_cooldown(sid or mid)
        if not ok_cd:
            return mid, sid, f"搜图冷却中，请 {remain:.0f}s 后再试"
        return mid, sid, None

    @staticmethod
    def _planner_tail() -> str:
        return (
            "\n\n【给回复用】优先采用【可信度裁决】里的「最可信」条目。"
            "分工提醒：漫画/本子/页局部看 Soutubot；插画出处看 SauceNAO，"
            "且插画优先 X/Pixiv 等创作者平台，不要只因为镜像站百分比高就改口。"
            "reference_info 写 1-2 条中文事实：作品/作者/本子名 + 可信度 + 链接。"
            "低/不可靠时要明说可能不准，不要编造。"
        )

    @Tool(
        "search_image",
        description=(
            "以图搜源工具。按图片类型分流："
            "漫画/本子/页局部 → Soutubot（nHentai 等）；"
            "插画/单图/画师图 → SauceNAO + Google Lens（Pixiv/X 等，创作者平台优先）。"
            "优先调用场景："
            "①问出处/sauce/来源/作者/pixiv id/本子名/哪本；"
            "②贴了漫画页或插画并追问信息；"
            "③讨论是否搬运/原作者；"
            "④需要给出 nhentai 或 pixiv/x 链接。"
            "engines：auto/both=按需；soutubot=漫画；saucenao/illustration=插画(含Google)；google=仅Google。"
            "intent：manga|illustration|auto。"
            "必须提供含图片 msg_id。表情包闲聊不要调用。"
        ),
        parameters=[
            _tool_param(
                "msg_id",
                ToolParamType.STRING,
                "含图片的消息 ID。优先用户刚发/被引用的那条图。",
                True,
            ),
            _tool_param(
                "engines",
                ToolParamType.STRING,
                "auto/both；soutubot；saucenao/illustration；google。默认 both。",
            ),
            _tool_param(
                "intent",
                ToolParamType.STRING,
                "manga=漫画/本子/页局部；illustration=插画；auto=未知。默认 auto。",
            ),
            _tool_param(
                "strict",
                ToolParamType.BOOLEAN,
                "仅 Soutubot：true=严格匹配，适合漫画局部截图。",
            ),
            _tool_param(
                "stream_id",
                ToolParamType.STRING,
                "可选会话 stream_id，帮助取图。",
            ),
        ],
    )
    async def handle_search_image(
        self,
        msg_id: str = "",
        engines: str = "both",
        intent: str = "auto",
        strict: Any = False,
        stream_id: str = "",
        **kwargs: Any,
    ) -> tuple[bool, str, int]:
        mid, sid, gate_err = self._tool_gate(msg_id, stream_id, kwargs)
        if gate_err:
            return False, gate_err, 0

        image_b64, err = await self._resolve_image_from_msg_id(mid, sid)
        if not image_b64:
            return False, err or "取图失败", 0

        ok, card, meta = await self._run_both(
            image_b64,
            strict=_as_bool(strict),
            engines=engines or "both",
            intent=intent or "auto",
        )
        card = (card or "") + self._planner_tail()
        if ok:
            self._mark_cooldown(sid or mid)
            conf = 1 if float(meta.get("top_similarity") or 0) >= 50 else 0
            return True, card, conf
        return False, card, 0

    @Tool(
        "search_saucenao",
        description=(
            "仅 SauceNAO：搜插画出处（Pixiv/X/Booru 等）。"
            "适合单张插画、同人图、画师作品；结果优先采信创作者平台。"
            "漫画整页/本子局部请用 search_soutubot 或 search_image(intent=manga)。"
            "一般插画也可用 search_image(intent=illustration) 并联 Google。"
            "必须提供含图片 msg_id。"
        ),
        parameters=[
            _tool_param("msg_id", ToolParamType.STRING, "含图片的消息 ID。", True),
            _tool_param("stream_id", ToolParamType.STRING, "可选会话 stream_id。"),
        ],
    )
    async def handle_search_saucenao(
        self,
        msg_id: str = "",
        stream_id: str = "",
        **kwargs: Any,
    ) -> tuple[bool, str, int]:
        mid, sid, gate_err = self._tool_gate(msg_id, stream_id, kwargs)
        if gate_err:
            return False, gate_err, 0

        image_b64, err = await self._resolve_image_from_msg_id(mid, sid)
        if not image_b64:
            return False, err or "取图失败", 0

        ok, card, meta = await self._run_saucenao(image_b64)
        # illustration-oriented verdict even for single engine
        if ok and meta.get("results"):
            verdict = format_verdict_block(
                saucenao_results=meta.get("results") or [],
                intent="illustration",
            )
            card = verdict + "\n\n" + (card or "")
        card = (card or "") + self._planner_tail()
        if ok:
            self._mark_cooldown(sid or mid)
            thr = float(self._saucenao_cfg().min_similarity or 50)
            conf = 1 if float(meta.get("top_similarity") or 0) >= thr else 0
            return True, card, conf
        return False, card, 0

    @Tool(
        "search_google",
        description=(
            "仅 Google Lens 搜图（经 SerpAPI）：插画出处补充源。"
            "适合 SauceNAO 没命中时的网页/社交转载检索；仍优先 X/Pixiv 创作者链。"
            "漫画/本子请用 Soutubot。一般优先 search_image(intent=illustration)。"
            "必须提供含图片 msg_id。"
        ),
        parameters=[
            _tool_param("msg_id", ToolParamType.STRING, "含图片的消息 ID。", True),
            _tool_param("stream_id", ToolParamType.STRING, "可选会话 stream_id。"),
        ],
    )
    async def handle_search_google(
        self,
        msg_id: str = "",
        stream_id: str = "",
        **kwargs: Any,
    ) -> tuple[bool, str, int]:
        mid, sid, gate_err = self._tool_gate(msg_id, stream_id, kwargs)
        if gate_err:
            return False, gate_err, 0

        image_b64, err = await self._resolve_image_from_msg_id(mid, sid)
        if not image_b64:
            return False, err or "取图失败", 0

        ok, card, meta = await self._run_google(image_b64)
        if ok and meta.get("results"):
            verdict = format_verdict_block(
                google_results=meta.get("results") or [],
                intent="illustration",
            )
            card = verdict + "\n\n" + (card or "")
        card = (card or "") + self._planner_tail()
        if ok:
            self._mark_cooldown(sid or mid)
            conf = 1 if float(meta.get("top_similarity") or 0) >= 60 else 0
            return True, card, conf
        return False, card, 0

    @Tool(
        "search_soutubot",
        description=(
            "仅 Soutubot：搜漫画/本子/页局部（主 nHentai，也含 eh 等）。"
            "适合本子页、漫画截图、局部裁切；不是插画作者溯源。"
            "插画请用 search_saucenao / search_google 或 search_image(intent=illustration)。"
            "必须提供含图片 msg_id。"
        ),
        parameters=[
            _tool_param("msg_id", ToolParamType.STRING, "含图片的消息 ID。", True),
            _tool_param(
                "strict",
                ToolParamType.BOOLEAN,
                "严格模式，适合漫画局部截图；默认 false。",
            ),
            _tool_param("stream_id", ToolParamType.STRING, "可选会话 stream_id。"),
        ],
    )
    async def handle_search_soutubot(
        self,
        msg_id: str = "",
        strict: Any = False,
        stream_id: str = "",
        **kwargs: Any,
    ) -> tuple[bool, str, int]:
        mid, sid, gate_err = self._tool_gate(msg_id, stream_id, kwargs)
        if gate_err:
            return False, gate_err, 0

        image_b64, err = await self._resolve_image_from_msg_id(mid, sid)
        if not image_b64:
            return False, err or "取图失败", 0

        ok, card, meta = await self._run_soutubot(image_b64, strict=_as_bool(strict))
        if ok and meta.get("results"):
            verdict = format_verdict_block(
                soutubot_results=meta.get("results") or [],
                intent="manga",
            )
            card = verdict + "\n\n" + (card or "")
        card = (card or "") + self._planner_tail()
        if ok:
            self._mark_cooldown(sid or mid)
            conf = 1 if float(meta.get("top_similarity") or 0) >= float(self._search_cfg().min_similarity) else 0
            return True, card, conf
        return False, card, 0

    # ------------------------------------------------------------------
    # Commands
    # ------------------------------------------------------------------

    @Command(
        "soutu",
        description="以图搜本（Soutubot）：回复图片或本条带图后发送 /soutu [strict]",
        pattern=r"^/soutu(?:\s+(?P<mode>strict|普通|常规|严格))?\s*$",
    )
    async def handle_soutu_command(self, stream_id: str = "", **kwargs: Any) -> dict[str, Any]:
        return await self._handle_image_command(
            stream_id=stream_id,
            kwargs=kwargs,
            engine="soutubot",
            waiting_text="在搜了在搜了…（Soutubot）",
        )

    @Command(
        "sauce",
        description="插画搜图：/sauce（SauceNAO+Google）/ /sauce google / /sauce all",
        pattern=r"^/sauce(?:\s+(?P<mode>all|both|soutu|soutubot|saucenao|nao|google|lens))?\s*$",
    )
    async def handle_sauce_command(self, stream_id: str = "", **kwargs: Any) -> dict[str, Any]:
        mode = ""
        matched = kwargs.get("matched_groups") or kwargs.get("groups") or {}
        if isinstance(matched, dict):
            mode = str(matched.get("mode") or "").strip().lower()
        text = str(kwargs.get("text") or "").lower()
        if not mode:
            if "all" in text or "both" in text:
                mode = "both"
            elif "soutu" in text:
                mode = "soutubot"
            elif "google" in text or "lens" in text:
                mode = "google"
        engine = "illustration"
        if mode in {"all", "both"}:
            engine = "both"
        elif mode in {"soutu", "soutubot"}:
            engine = "soutubot"
        elif mode in {"google", "lens"}:
            engine = "google"
        elif mode in {"saucenao", "nao"}:
            engine = "saucenao"
        waiting = {
            "saucenao": "在搜了在搜了…（SauceNAO）",
            "google": "在搜了在搜了…（Google Lens）",
            "illustration": "在搜了在搜了…（SauceNAO + Google）",
            "soutubot": "在搜了在搜了…（Soutubot）",
            "both": "在搜了在搜了…（全引擎）",
        }.get(engine, "在搜了在搜了…")
        return await self._handle_image_command(
            stream_id=stream_id,
            kwargs=kwargs,
            engine=engine,
            waiting_text=waiting,
        )

    async def _handle_image_command(
        self,
        *,
        stream_id: str,
        kwargs: dict[str, Any],
        engine: str,
        waiting_text: str,
    ) -> dict[str, Any]:
        send_kwargs = dict(kwargs)
        send_kwargs.pop("text", None)
        sid = _extract_stream_id(send_kwargs, stream_id)
        if not getattr(self.config.plugin, "enabled", True):
            msg = "搜图插件已禁用"
            await self._send_text(msg, sid)
            return {"success": False, "content": msg}

        ok_cd, remain = self._check_cooldown(sid)
        if not ok_cd:
            msg = f"搜图冷却中，请 {remain:.0f}s 后再试"
            await self._send_text(msg, sid)
            return {"success": False, "content": msg}

        mode = ""
        matched = kwargs.get("matched_groups") or kwargs.get("groups") or {}
        if isinstance(matched, dict):
            mode = str(matched.get("mode") or "").strip()
        text = str(kwargs.get("text") or "")
        if not mode and "strict" in text.lower():
            mode = "strict"
        if not mode and "严格" in text:
            mode = "strict"
        strict = mode.lower() in {"strict", "严格"}

        image_b64, err = await self._resolve_image_from_command_kwargs(send_kwargs)
        if not image_b64:
            await self._send_text(err, sid)
            return {"success": False, "content": err}

        await self._send_text(waiting_text, sid)
        self._spawn(
            self._background_command_search(
                image_b64,
                sid,
                engine=engine,
                strict=strict,
            )
        )
        return {"success": True, "content": "已开始搜图"}

    async def _background_command_search(
        self,
        image_base64: str,
        stream_id: str,
        *,
        engine: str,
        strict: bool,
    ) -> None:
        try:
            eng = (engine or "soutubot").lower()
            if eng == "saucenao":
                ok, card, meta = await self._run_saucenao(image_base64)
                if ok and meta.get("results"):
                    card = format_verdict_block(
                        saucenao_results=meta.get("results") or [],
                        intent="illustration",
                    ) + "\n\n" + card
            elif eng in {"google", "lens"}:
                ok, card, meta = await self._run_google(image_base64)
                if ok and meta.get("results"):
                    card = format_verdict_block(
                        google_results=meta.get("results") or [],
                        intent="illustration",
                    ) + "\n\n" + card
            elif eng in {"illustration", "illust"}:
                ok, card, meta = await self._run_both(
                    image_base64,
                    strict=strict,
                    engines="illustration",
                    intent="illustration",
                )
            elif eng in {"both", "all"}:
                ok, card, meta = await self._run_both(
                    image_base64, strict=strict, engines="both", intent="auto"
                )
            else:
                ok, card, meta = await self._run_soutubot(image_base64, strict=strict)
                if ok and meta.get("results"):
                    card = format_verdict_block(
                        soutubot_results=meta.get("results") or [],
                        intent="manga",
                    ) + "\n\n" + card
            if ok:
                self._mark_cooldown(stream_id)
            await self._send_text(card, stream_id)
            if ok and eng not in {"both", "all", "illustration", "illust"}:
                await self._maybe_send_preview(meta, stream_id)
        except Exception as exc:  # noqa: BLE001
            logger.exception("[Soutubot] background search failed")
            await self._send_text(f"搜图失败：{exc}", stream_id)

    @Command(
        "soutu_status",
        description="检查搜图插件（Soutubot/SauceNAO/Google）状态",
        pattern=r"^/soutu_status\s*$",
    )
    async def handle_soutu_status(self, stream_id: str = "", **kwargs: Any) -> dict[str, Any]:
        send_kwargs = dict(kwargs)
        send_kwargs.pop("text", None)
        sid = _extract_stream_id(send_kwargs, stream_id)
        platform, user_id = extract_command_actor(send_kwargs)
        admins = list(getattr(self.config.admin, "admin_users", []) or [])
        if not _match_admin(platform, user_id, admins):
            msg = "只有管理员可以执行此命令"
            await self._send_text(msg, sid)
            return {"success": False, "content": msg}

        cfg = self._search_cfg()
        sn = self._saucenao_cfg()
        gg = self._google_cfg()
        lines = [
            "搜图插件状态",
            f"- enabled: {getattr(self.config.plugin, 'enabled', True)}",
            f"- version: {PLUGIN_VERSION}",
            f"- soutubot.base_url: {cfg.base_url}",
            f"- soutubot.min_similarity: {cfg.min_similarity}",
            f"- cooldown: {cfg.cooldown_seconds}s",
            f"- saucenao.enabled: {getattr(sn, 'enabled', False)}",
            f"- saucenao.api_key: {_mask_key(getattr(sn, 'api_key', ''))}",
            f"- google.enabled: {getattr(gg, 'enabled', False)}",
            f"- google.api_key: {_mask_key(getattr(gg, 'api_key', ''))}",
            f"- google.with_illustration: {getattr(gg, 'with_illustration', True)}",
        ]
        probe = "未探测"
        try:
            if not self._client:
                self._rebuild_client()
            assert self._client is not None
            m_val = await self._client.refresh_m(force=True)
            probe = f"ok m={m_val}"
        except Exception as exc:  # noqa: BLE001
            probe = f"fail: {exc}"
        lines.append(f"- soutubot.homepage: {probe}")

        sn_probe = "disabled"
        if self._saucenao:
            sn_probe = "configured"
        elif getattr(sn, "enabled", False):
            sn_probe = "enabled but missing api_key"
        lines.append(f"- saucenao.client: {sn_probe}")

        gg_probe = "disabled"
        if self._google:
            gg_probe = "configured"
        elif getattr(gg, "enabled", False):
            gg_probe = "enabled but missing api_key"
        lines.append(f"- google.client: {gg_probe}")

        content = "\n".join(lines)
        await self._send_text(content, sid)
        return {"success": True, "content": content}


def create_plugin() -> SoutubotPlugin:
    return SoutubotPlugin()
