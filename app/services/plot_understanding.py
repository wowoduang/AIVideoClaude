"""M8: Plot understanding module.

Two-layer understanding:
  - Layer 1 (local): Per-segment plot role classification
  - Layer 2 (global): Whole-video plot summary and arc detection

The local layer runs without an LLM (rule-based), while the global
layer can optionally call an LLM for richer summaries.
"""

import json
from typing import Any, Dict, List

from loguru import logger


# ── Layer 1: Local (per-segment) understanding ────────────────────

# Plot-role keywords used for lightweight classification.
_ROLE_KEYWORDS: Dict[str, List[str]] = {
    "setup": ["介绍", "背景", "从前", "曾经", "开始", "一天"],
    "conflict": ["矛盾", "冲突", "争吵", "反对", "不同意", "问题", "危机", "困难"],
    "climax": ["高潮", "爆发", "终于", "真相", "揭露", "摊牌", "决战"],
    "twist": ["反转", "没想到", "竟然", "突然", "意外", "其实", "原来"],
    "resolution": ["解决", "和好", "结局", "最终", "后来", "从此"],
}


def classify_segment_role(text: str, position_ratio: float = 0.5) -> str:
    """Classify a segment's plot role based on text keywords and position.

    Parameters
    ----------
    text : str
        Subtitle text of the segment.
    position_ratio : float
        Segment position as a ratio (0.0 = start, 1.0 = end).

    Returns
    -------
    str
        One of: ``"setup"``, ``"conflict"``, ``"climax"``,
        ``"twist"``, ``"resolution"``, ``"development"``.
    """
    if not text:
        return "development"

    # Keyword matching (first match wins, ordered by dramatic weight)
    for role in ("twist", "climax", "conflict", "resolution", "setup"):
        for kw in _ROLE_KEYWORDS[role]:
            if kw in text:
                return role

    # Positional heuristic when no keywords match
    if position_ratio < 0.15:
        return "setup"
    if position_ratio > 0.85:
        return "resolution"
    return "development"


def add_local_understanding(evidence_list: List[Dict]) -> List[Dict]:
    """Annotate each evidence package with local plot understanding.

    Adds ``plot_role`` and ``attraction_level`` to each item in-place
    and returns the same list.
    """
    total = len(evidence_list)
    for idx, pkg in enumerate(evidence_list):
        position_ratio = idx / max(total - 1, 1)
        text = pkg.get("subtitle_text", "")
        role = classify_segment_role(text, position_ratio)
        pkg["plot_role"] = role
        pkg["attraction_level"] = _role_to_attraction(role)
    return evidence_list


def _role_to_attraction(role: str) -> str:
    """Map plot role to attraction level (高/中/低)."""
    high_roles = {"climax", "twist"}
    medium_roles = {"conflict", "resolution"}
    if role in high_roles:
        return "高"
    if role in medium_roles:
        return "中"
    return "低"


# ── Layer 2: Global (per-video) understanding ─────────────────────

def build_global_summary(
    evidence_list: List[Dict],
    *,
    api_key: str = "",
    base_url: str = "",
    model: str = "",
) -> Dict[str, Any]:
    """Build a global plot summary from all evidence packages.

    When LLM credentials are provided, uses the LLM for a richer
    summary.  Otherwise falls back to a rule-based summary.

    Returns
    -------
    dict with keys:
        ``synopsis`` – one-paragraph plot summary,
        ``arc``      – detected narrative arc type,
        ``key_segments`` – list of high-attraction segment IDs.
    """
    if not evidence_list:
        return {"synopsis": "", "arc": "unknown", "key_segments": []}

    # Collect high-attraction segments
    key_segments = [
        pkg["scene_id"]
        for pkg in evidence_list
        if pkg.get("attraction_level") == "高"
    ]

    # Try LLM-based summary if credentials available
    if api_key and model:
        try:
            return _llm_global_summary(
                evidence_list, api_key, base_url, model, key_segments
            )
        except Exception as e:
            logger.warning(f"LLM全局摘要失败，回退到规则摘要: {e}")

    # Rule-based fallback
    return _rule_based_global_summary(evidence_list, key_segments)


def _rule_based_global_summary(
    evidence_list: List[Dict], key_segments: List[str],
) -> Dict[str, Any]:
    """Build a simple global summary without LLM."""
    # Detect narrative arc from plot_role distribution
    roles = [pkg.get("plot_role", "development") for pkg in evidence_list]
    arc = _detect_arc(roles)

    # Build synopsis from subtitle texts
    all_texts = [
        (pkg.get("subtitle_text", "") or "")[:40]
        for pkg in evidence_list
        if pkg.get("subtitle_text")
    ]
    synopsis = "；".join(all_texts[:8])
    if len(all_texts) > 8:
        synopsis += "……"

    return {
        "synopsis": synopsis,
        "arc": arc,
        "key_segments": key_segments,
    }


def _detect_arc(roles: List[str]) -> str:
    """Detect narrative arc type from sequence of plot roles."""
    if not roles:
        return "unknown"

    has_twist = "twist" in roles
    has_climax = "climax" in roles
    has_conflict = "conflict" in roles

    if has_twist and has_climax:
        return "twist_climax"
    if has_twist:
        return "twist"
    if has_climax:
        return "rising_action"
    if has_conflict:
        return "conflict_driven"
    return "linear"


def _llm_global_summary(
    evidence_list: List[Dict],
    api_key: str,
    base_url: str,
    model: str,
    key_segments: List[str],
) -> Dict[str, Any]:
    """Use LLM to generate a richer global plot summary."""
    from openai import OpenAI

    # Build a condensed version of the evidence for the prompt
    lines = []
    for pkg in evidence_list:
        role = pkg.get("plot_role", "development")
        text = (pkg.get("subtitle_text", "") or "")[:80]
        lines.append(f"[{pkg['scene_id']}] role={role} text={text}")

    evidence_text = "\n".join(lines)

    prompt = f"""请根据以下视频片段信息，生成一个简洁的剧情摘要。

要求：
1. synopsis: 用2-3句话概括整个视频的主要剧情。
2. arc: 叙事弧线类型（如 twist_climax / rising_action / conflict_driven / linear / twist）。
3. 输出 JSON 格式。

片段信息：
{evidence_text}

输出格式：
{{"synopsis": "...", "arc": "..."}}
"""

    client = OpenAI(api_key=api_key, base_url=base_url)
    response = client.chat.completions.create(
        model=model,
        messages=[
            {"role": "system", "content": "你是一名专业的影视分析师。"},
            {"role": "user", "content": prompt},
        ],
        temperature=0.5,
        response_format={"type": "json_object"},
    )
    content = response.choices[0].message.content if response.choices else "{}"
    result = json.loads(content.strip())
    result["key_segments"] = key_segments
    logger.info(f"LLM全局摘要完成: arc={result.get('arc')}")
    return result
