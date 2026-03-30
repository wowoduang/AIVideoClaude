from typing import Dict, List

from loguru import logger


# ── Overflow severity thresholds (PRD) ───────────────────────────
_OVERFLOW_WARN_SECONDS = 0.5   # Narration exceeds budget by < 0.5s
_OVERFLOW_ERROR_SECONDS = 1.5  # Narration exceeds budget by >= 1.5s


def estimate_char_budget(
    duration: float,
    chars_per_second: float = 4.0,
    reserve_ratio: float = 0.85,
) -> int:
    """Calculate the character budget for a segment based on its duration."""
    return max(8, int(duration * chars_per_second * reserve_ratio))


def fit_check(
    narration: str,
    duration: float,
    chars_per_second: float = 4.0,
) -> Dict:
    """Check whether narration text fits within the time budget.

    Returns a dict with:
        ``fits``       – bool, True if narration fits,
        ``budget``     – int, calculated char budget,
        ``actual``     – int, actual narration length,
        ``overflow``   – int, chars over budget (0 if fits),
        ``severity``   – str, one of ``"ok"`` / ``"warn"`` / ``"error"``.
    """
    budget = estimate_char_budget(duration, chars_per_second)
    actual = len((narration or "").strip())
    overflow = max(0, actual - budget)
    overflow_seconds = overflow / chars_per_second if overflow > 0 else 0.0

    if overflow == 0:
        severity = "ok"
    elif overflow_seconds < _OVERFLOW_WARN_SECONDS:
        severity = "warn"
    else:
        severity = "error"

    return {
        "fits": overflow == 0,
        "budget": budget,
        "actual": actual,
        "overflow": overflow,
        "overflow_seconds": round(overflow_seconds, 2),
        "severity": severity,
    }


def trim_text_to_budget(text: str, budget: int) -> str:
    """Trim text to fit within budget, cutting at punctuation when possible."""
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


def apply_timeline_budget(
    items: List[Dict],
    auto_trim: bool = True,
) -> List[Dict]:
    """Apply timeline budget to all script items.

    For each item, calculates the char budget from its duration and
    optionally trims the narration to fit.

    Parameters
    ----------
    items : list
        Script item dicts.
    auto_trim : bool
        If True (default), narration is trimmed to fit the budget.
        If False, only ``char_budget`` and ``fit_check`` are added.
    """
    result: List[Dict] = []
    warn_count = 0
    error_count = 0

    for item in items or []:
        new_item = dict(item)
        duration = float(item.get("duration", 0) or 0)
        if duration <= 0 and item.get("start") is not None and item.get("end") is not None:
            duration = max(0.5, float(item["end"]) - float(item["start"]))
        budget = estimate_char_budget(duration)
        new_item["char_budget"] = budget

        original_narration = item.get("narration", "")
        check = fit_check(original_narration, duration)

        if auto_trim and not check["fits"]:
            trimmed_narration = trim_text_to_budget(original_narration, budget)
            if check["severity"] == "error":
                error_count += 1
                logger.warning(
                    f"严重超预算截断: scene_id={item.get('scene_id', 'unknown')}, "
                    f"预算={budget}, 原文={len(original_narration)}, "
                    f"溢出={check['overflow_seconds']}s"
                )
            elif check["severity"] == "warn":
                warn_count += 1
            new_item["narration"] = trimmed_narration
        else:
            new_item["narration"] = original_narration

        result.append(new_item)

    if warn_count or error_count:
        logger.info(
            f"时间线预算检查: {warn_count} 个轻微溢出, {error_count} 个严重溢出"
        )
    return result
