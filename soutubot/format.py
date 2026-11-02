"""Format soutubot results for Planner / chat."""

from __future__ import annotations

from typing import Any


def format_search_card(
    results: list[dict[str, Any]],
    *,
    result_id: Any = "",
    execution_time: Any = None,
    factor: Any = None,
    min_similarity: float = 28.0,
    max_lines: int = 8,
) -> str:
    """Build a compact Chinese evidence card for Planner/replyer."""
    if not results:
        return (
            "【Soutubot 搜图结果】\n"
            "未找到可用匹配。可换更清晰的截图/裁切局部再试，"
            "或降低 min_similarity。"
        )

    lines: list[str] = ["【Soutubot 搜图结果】"]
    meta_bits: list[str] = []
    if result_id not in (None, ""):
        meta_bits.append(f"id={result_id}")
    if execution_time not in (None, ""):
        meta_bits.append(f"耗时={execution_time}")
    if factor not in (None, ""):
        try:
            fval = float(factor)
            mode = "严格" if fval > 1.2 else "常规"
            meta_bits.append(f"模式={mode}({fval})")
        except (TypeError, ValueError):
            meta_bits.append(f"factor={factor}")
    if meta_bits:
        lines.append(" · ".join(meta_bits))

    strong = [r for r in results if float(r.get("similarity") or 0) >= float(min_similarity)]
    weak = [r for r in results if float(r.get("similarity") or 0) < float(min_similarity)]
    display = strong if strong else results
    if not strong and results:
        lines.append(f"提示：最高相似度低于 {min_similarity}，结果可能不准，请自行判断。")

    for idx, item in enumerate(display[: max(1, int(max_lines or 8))], start=1):
        sim = item.get("similarity")
        title = str(item.get("title") or "(无标题)").strip()
        if len(title) > 80:
            title = title[:77] + "..."
        source = item.get("source_label") or item.get("source") or "?"
        lang = item.get("language") or "?"
        page = item.get("page")
        page_part = f" P{page}" if page not in (None, "") else ""
        gallery = item.get("gallery_url") or ""
        page_url = item.get("page_url") or ""
        url = page_url or gallery
        line = f"{idx}. [{sim}%|{_soutu_band(sim)}] {source}/{lang}{page_part} | {title}"
        if url:
            line += f"\n   {url}"
        elif gallery and gallery != url:
            line += f"\n   {gallery}"
        lines.append(line)

    if strong and weak:
        lines.append(f"（另有 {len(weak)} 条低于 {min_similarity}% 的低匹配结果已折叠）")

    lines.append(
        "用法：优先采用可信度高/中且链接可打开的结果；"
        "回答写清来源站、标题、页码与链接，不要编造未返回的本子信息。"
    )
    return "\n".join(lines)


def _soutu_band(sim: Any) -> str:
    try:
        value = float(sim or 0)
    except (TypeError, ValueError):
        return "不可靠"
    if value >= 70:
        return "高"
    if value >= 45:
        return "中"
    if value >= 28:
        return "低"
    return "不可靠"
