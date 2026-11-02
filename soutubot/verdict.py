"""Credibility ranking for reverse-image search results.

Division of labor (product rule):
- Soutubot → manga / doujin / page crops (mainly nhentai family)
- SauceNAO → illustration source hunting (pixiv / booru / X / artist pages)

Planner should NOT treat SauceNAO% and Soutubot% as the same scale.
Creator platforms (X / Pixiv / artist sites) outrank repost / aggregator pages
when hunting illustration sources.
"""

from __future__ import annotations

import re
from typing import Any
from urllib.parse import urlparse


# Illustration source priority: creator platforms first.
CREATOR_PLATFORM_DOMAINS = (
    "pixiv.net",
    "www.pixiv.net",
    "i.pximg.net",
    "twitter.com",
    "x.com",
    "mobile.twitter.com",
    "pbs.twimg.com",
    "fanbox.cc",
    "www.fanbox.cc",
    "patreon.com",
    "www.patreon.com",
    "fantia.jp",
    "www.fantia.jp",
    "booth.pm",
    "skeb.jp",
    "www.skeb.jp",
    "artstation.com",
    "www.artstation.com",
    "deviantart.com",
    "www.deviantart.com",
    "cara.app",
    "bsky.app",
    "misskey.io",
)

# Useful but secondary illustration mirrors / indexes.
ILLUST_MIRROR_DOMAINS = (
    "danbooru.donmai.us",
    "yande.re",
    "konachan.com",
    "gelbooru.com",
    "chan.sankakucomplex.com",
    "e-hentai.org",
    "exhentai.org",
    "nhentai.net",
    "saucenao.com",
)

# Low-trust repost / social scrap hosts for illustration provenance.
REPOST_HOST_HINTS = (
    "pinterest.",
    "pinimg.com",
    "weibo.",
    "zhihu.com",
    "bilibili.com",
    "douyin.com",
    "tiktok.com",
    "reddit.com",
    "imgur.com",
    "tumblr.com",
    "facebook.com",
    "instagram.com",
    "baidu.com",
    "so.com",
    "sogou.com",
)


def _f(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def confidence_band(
    engine: str,
    similarity: float,
    *,
    below_header_min: bool = False,
    exact_match: bool = False,
) -> str:
    """Map engine-specific similarity to a band."""
    sim = _f(similarity)
    eng = (engine or "").lower()
    if below_header_min:
        return "不可靠"
    if eng in {"saucenao", "google", "google_lens"}:
        # SauceNAO / visual web search practice for illustrations.
        if exact_match or sim >= 80:
            return "高"
        if sim >= 60:
            return "中"
        if sim >= 50:
            return "低"
        return "不可靠"
    # Soutubot: manga / page crops. SPA hides <28.
    if sim >= 70:
        return "高"
    if sim >= 45:
        return "中"
    if sim >= 28:
        return "低"
    return "不可靠"


def band_score(band: str) -> int:
    return {"高": 3, "中": 2, "低": 1, "不可靠": 0}.get(band, 0)


def _norm_title(text: str) -> str:
    t = str(text or "").lower()
    t = re.sub(r"\[[^\]]*\]|\([^)]*\)|【[^】]*】|（[^）]*）", " ", t)
    t = re.sub(r"[^\w\u4e00-\u9fff]+", " ", t, flags=re.UNICODE)
    return re.sub(r"\s+", " ", t).strip()


def _domain(url: str) -> str:
    try:
        host = urlparse(str(url or "")).netloc.lower()
    except Exception:
        return ""
    if host.startswith("www."):
        host = host[4:]
    return host


def platform_tier(url: str, source_hint: str = "") -> str:
    """creator | mirror | repost | other"""
    host = _domain(url)
    blob = f"{host} {source_hint}".lower()
    if any(d in host or d in blob for d in CREATOR_PLATFORM_DOMAINS):
        return "creator"
    if any(d in host or d in blob for d in ILLUST_MIRROR_DOMAINS):
        return "mirror"
    if any(h in host or h in blob for h in REPOST_HOST_HINTS):
        return "repost"
    # SauceNAO index names often include Pixiv / Twitter even without clean URL.
    if re.search(r"\bpixiv\b|\btwitter\b|\bx\.com\b|\bfanbox\b|\bpatreon\b", blob):
        return "creator"
    return "other"


def platform_bonus(tier: str, *, engine_key: str) -> float:
    """Prefer creator platforms for illustration engines."""
    eng = (engine_key or "").lower()
    if eng in {"saucenao", "google", "google_lens"}:
        return {
            "creator": 35.0,
            "mirror": 8.0,
            "other": 0.0,
            "repost": -18.0,
        }.get(tier, 0.0)
    # Soutubot is manga-library oriented; nh/eh are expected, not "repost".
    if eng == "soutubot":
        return {
            "creator": 5.0,  # rare but fine
            "mirror": 12.0,  # nh/eh/panda etc.
            "other": 0.0,
            "repost": -5.0,
        }.get(tier, 0.0)
    return 0.0


def flatten_candidates(
    *,
    saucenao_results: list[dict[str, Any]] | None = None,
    soutubot_results: list[dict[str, Any]] | None = None,
    google_results: list[dict[str, Any]] | None = None,
    limit_per_engine: int = 5,
    intent: str = "auto",
) -> list[dict[str, Any]]:
    """Normalize heterogeneous engine rows into comparable candidates.

    intent:
      - manga: prefer / only trust Soutubot-style hits for ranking narrative
      - illustration: prefer SauceNAO/Google + creator platforms
      - auto: keep all, let score decide
    """
    out: list[dict[str, Any]] = []
    intent_n = (intent or "auto").strip().lower()

    for item in (saucenao_results or [])[: max(1, limit_per_engine)]:
        if intent_n in {"manga", "doujin", "comic", "本子", "漫画"}:
            # still include but will be down-weighted for manga intent
            pass
        sim = _f(item.get("similarity"))
        urls = item.get("urls") if isinstance(item.get("urls"), list) else []
        url = str(item.get("url") or (urls[0] if urls else "") or "").strip()
        title = str(item.get("title") or "").strip()
        author = str(item.get("author") or "").strip()
        below = bool(item.get("below_header_min"))
        source = str(item.get("index_name") or "SauceNAO")
        band = confidence_band("saucenao", sim, below_header_min=below)
        tier = platform_tier(url, source)
        out.append(
            {
                "engine": "SauceNAO",
                "engine_key": "saucenao",
                "role": "illustration",
                "similarity": sim,
                "band": band,
                "title": title,
                "author": author,
                "url": url,
                "source": source,
                "page": None,
                "below_header_min": below,
                "platform_tier": tier,
            }
        )

    for item in (google_results or [])[: max(1, limit_per_engine)]:
        sim = _f(item.get("similarity") or item.get("score") or 0)
        url = str(item.get("url") or item.get("link") or "").strip()
        title = str(item.get("title") or "").strip()
        source = str(item.get("source") or "Google")
        exact = bool(item.get("exact_match") or item.get("exact_matches"))
        band = confidence_band("google", sim if sim else (90 if exact else 55), exact_match=exact)
        tier = platform_tier(url, source)
        out.append(
            {
                "engine": "Google",
                "engine_key": "google",
                "role": "illustration",
                "similarity": sim if sim else (90.0 if exact else 55.0),
                "band": band,
                "title": title,
                "author": str(item.get("author") or "").strip(),
                "url": url,
                "source": source,
                "page": None,
                "below_header_min": False,
                "platform_tier": tier,
                "exact_match": exact,
            }
        )

    for item in (soutubot_results or [])[: max(1, limit_per_engine)]:
        sim = _f(item.get("similarity"))
        url = str(item.get("page_url") or item.get("gallery_url") or "").strip()
        title = str(item.get("title") or "").strip()
        band = confidence_band("soutubot", sim)
        source = str(item.get("source_label") or item.get("source") or "Soutubot")
        page = item.get("page")
        tier = platform_tier(url, source)
        out.append(
            {
                "engine": "Soutubot",
                "engine_key": "soutubot",
                "role": "manga",
                "similarity": sim,
                "band": band,
                "title": title,
                "author": "",
                "url": url,
                "source": source,
                "page": page,
                "below_header_min": False,
                "platform_tier": tier,
            }
        )

    # Soft filter by intent: keep all candidates but tag intent for scoring.
    for row in out:
        row["intent"] = intent_n
    return out


def score_candidate(item: dict[str, Any], *, agreement_bonus: int = 0) -> float:
    """Higher is better. Engine % is NOT used raw across engines."""
    band = band_score(str(item.get("band") or ""))
    sim = _f(item.get("similarity"))
    score = band * 100.0 + min(sim, 99.9)
    if item.get("url"):
        score += 3.0
    if item.get("below_header_min"):
        score -= 40.0
    if item.get("exact_match"):
        score += 12.0

    eng = str(item.get("engine_key") or "")
    tier = str(item.get("platform_tier") or "other")
    score += platform_bonus(tier, engine_key=eng)

    intent = str(item.get("intent") or "auto")
    role = str(item.get("role") or "")
    # Intent alignment
    if intent in {"manga", "doujin", "comic", "本子", "漫画"}:
        if role == "manga":
            score += 40.0
        elif role == "illustration":
            score -= 25.0
    elif intent in {"illustration", "illust", "pixiv", "插画", "同人图"}:
        if role == "illustration":
            score += 40.0
            if tier == "creator":
                score += 15.0  # extra push for X/pixiv when hunting illust source
        elif role == "manga":
            score -= 20.0

    score += float(agreement_bonus)
    return score


def detect_agreements(cands: list[dict[str, Any]]) -> dict[int, int]:
    """Return index -> bonus if another engine has similar title or same domain."""
    bonuses: dict[int, int] = {i: 0 for i in range(len(cands))}
    for i, a in enumerate(cands):
        for j, b in enumerate(cands):
            if i >= j:
                continue
            if a.get("engine_key") == b.get("engine_key"):
                continue
            ta, tb = _norm_title(str(a.get("title") or "")), _norm_title(str(b.get("title") or ""))
            da, db = _domain(str(a.get("url") or "")), _domain(str(b.get("url") or ""))
            agree = False
            if ta and tb and (ta in tb or tb in ta or _token_overlap(ta, tb) >= 0.5):
                agree = True
            if da and db and da == db:
                agree = True
            # author + title weak agree for illust engines
            aa, ab = _norm_title(str(a.get("author") or "")), _norm_title(str(b.get("author") or ""))
            if aa and ab and aa == ab and ta and tb and _token_overlap(ta, tb) >= 0.3:
                agree = True
            if agree:
                bonuses[i] += 25
                bonuses[j] += 25
    return bonuses


def _token_overlap(a: str, b: str) -> float:
    sa, sb = set(a.split()), set(b.split())
    if not sa or not sb:
        return 0.0
    inter = len(sa & sb)
    return inter / max(1, min(len(sa), len(sb)))


def pick_best(cands: list[dict[str, Any]]) -> tuple[dict[str, Any] | None, list[dict[str, Any]]]:
    if not cands:
        return None, []
    bonuses = detect_agreements(cands)
    ranked: list[dict[str, Any]] = []
    for i, item in enumerate(cands):
        row = dict(item)
        row["agreement_bonus"] = bonuses.get(i, 0)
        row["score"] = score_candidate(row, agreement_bonus=row["agreement_bonus"])
        ranked.append(row)
    ranked.sort(key=lambda x: _f(x.get("score")), reverse=True)
    return ranked[0], ranked


def format_verdict_block(
    *,
    saucenao_results: list[dict[str, Any]] | None = None,
    soutubot_results: list[dict[str, Any]] | None = None,
    google_results: list[dict[str, Any]] | None = None,
    intent: str = "auto",
    top_n: int = 3,
) -> str:
    """Chinese verdict for Planner: who wins and why."""
    cands = flatten_candidates(
        saucenao_results=saucenao_results,
        soutubot_results=soutubot_results,
        google_results=google_results,
        intent=intent,
    )
    best, ranked = pick_best(cands)
    lines = ["【可信度裁决】"]
    lines.append(
        "分工：Soutubot=漫画/本子/页局部；SauceNAO(+Google)=插画出处。"
        "插画优先创作者平台（X / Pixiv / Fanbox 等），镜像站次之，搬运站降权。"
        "勿直接比较两引擎原始百分比；先看可信度带，再看平台层级。"
    )
    intent_n = (intent or "auto").strip().lower()
    if intent_n in {"manga", "doujin", "comic", "本子", "漫画"}:
        lines.append("当前意图：漫画/本子 → 优先 Soutubot。")
    elif intent_n in {"illustration", "illust", "pixiv", "插画", "同人图"}:
        lines.append("当前意图：插画 → 优先 SauceNAO/Google，且优先 X/Pixiv 等创作者链。")

    if not best:
        lines.append("结论：无可用候选。不要编造出处。")
        return "\n".join(lines)

    def short(item: dict[str, Any]) -> str:
        title = str(item.get("title") or "(无标题)").strip()
        if len(title) > 48:
            title = title[:45] + "..."
        page = item.get("page")
        page_s = f" P{page}" if page not in (None, "") else ""
        url = str(item.get("url") or "").strip()
        flags: list[str] = []
        if _f(item.get("agreement_bonus")) > 0:
            flags.append("互证")
        tier = str(item.get("platform_tier") or "")
        if tier == "creator":
            flags.append("创作者平台")
        elif tier == "mirror":
            flags.append("镜像/索引")
        elif tier == "repost":
            flags.append("疑似搬运")
        flag_s = (" · " + "/".join(flags)) if flags else ""
        role = "漫画" if item.get("role") == "manga" else "插画"
        base = (
            f"{item.get('engine')}[{role}] [{item.get('similarity')}%] "
            f"可信度={item.get('band')}{page_s} | {title}{flag_s}"
        )
        if url:
            base += f"\n   {url}"
        return base

    lines.append("最可信：")
    lines.append(short(best))

    alts = [x for x in ranked[1:top_n] if band_score(str(x.get("band"))) > 0]
    if alts:
        lines.append("备选：")
        for item in alts:
            lines.append(short(item))

    band = str(best.get("band") or "")
    tier = str(best.get("platform_tier") or "")
    if band == "高" and tier == "creator":
        lines.append("回答策略：可较明确给出创作者出处（X/Pixiv 等），附链接。")
    elif band == "高":
        lines.append("回答策略：可较明确给出出处；若非创作者平台，可顺带提可能是转载。")
    elif band == "中":
        lines.append("回答策略：可以说较像/很可能，附链接；避免一口咬死。")
    elif band == "低":
        lines.append("回答策略：只作参考，明确可能不准；建议换更清晰原图。")
    else:
        lines.append("回答策略：不足以下定论，不要假装找到了。")

    if tier == "creator":
        lines.append("加分项：命中创作者平台（X/Pixiv 等），插画出处可信度优先。")
    if _f(best.get("agreement_bonus")) > 0:
        lines.append("加分项：多引擎结果互相印证。")

    return "\n".join(lines)
