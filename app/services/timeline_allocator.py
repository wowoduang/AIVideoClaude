from typing import Dict, List

from loguru import logger


def estimate_char_budget(duration: float, chars_per_second: float = 4.0, reserve_ratio: float = 0.85) -> int:
    return max(8, int(duration * chars_per_second * reserve_ratio))


def trim_text_to_budget(text: str, budget: int) -> str:
    text = (text or "").strip()
    if len(text) <= budget:
        return text
    soft_budget = max(6, budget - 1)
    trimmed = text[:soft_budget]
    for punct in ["，", "。", "！", "？", ",", ".", "!", "?"]:
        idx = trimmed.rfind(punct)
        if idx >= int(soft_budget * 0.6):
            trimmed = trimmed[: idx + 1]
            break
    if len(trimmed) > budget:
        trimmed = trimmed[:budget]
    return trimmed.rstrip("，。！？,.!? ") + "…"


def apply_timeline_budget(items: List[Dict]) -> List[Dict]:
    result: List[Dict] = []
    for item in items or []:
        new_item = dict(item)
        duration = float(item.get("duration", 0) or 0)
        if duration <= 0 and item.get("start") is not None and item.get("end") is not None:
            duration = max(0.5, float(item["end"]) - float(item["start"]))
        budget = estimate_char_budget(duration)
        new_item["char_budget"] = budget
        original_narration = item.get("narration", "")
        trimmed_narration = trim_text_to_budget(original_narration, budget)
        if len(trimmed_narration) < len(original_narration):
            logger.warning(f"后置截断触发: scene_id={item.get('scene_id', 'unknown')}, 预算={budget}, 原文长度={len(original_narration)}, 截断后长度={len(trimmed_narration)}")
        new_item["narration"] = trimmed_narration
        result.append(new_item)
    return result
