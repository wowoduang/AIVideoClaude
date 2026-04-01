from __future__ import annotations

import json
import re
from typing import Dict, List

from loguru import logger


def classify_segment_role(text: str, position_ratio: float) -> str:
    text = (text or "").strip()
    role_keywords = {
        "twist": ["原来", "竟然", "没想到", "突然", "其实"],
        "climax": ["终于", "决战", "关键", "爆发", "当场"],
        "conflict": ["争吵", "质问", "威胁", "冲突", "对峙"],
        "resolution": ["和解", "离开", "结束", "平静", "释然"],
        "setup": ["开始", "第一次", "介绍", "来到", "得知"],
    }
    for role in ("twist", "climax", "conflict", "resolution", "setup"):
        if any(kw in text for kw in role_keywords[role]):
            return role
    if position_ratio < 0.15:
        return "setup"
    if position_ratio > 0.85:
        return "resolution"
    return "development"



def _role_to_attraction(role: str) -> str:
    return "高" if role in {"climax", "twist"} else ("中" if role in {"conflict", "development"} else "低")



def _extract_key_dialogue(text: str) -> List[str]:
    lines = [x.strip() for x in re.split(r"[。！？!?]", text or "") if x.strip()]
    return lines[:2]



def _core_event(text: str, visual_only: bool) -> str:
    text = (text or "").strip()
    if visual_only and not text:
        return "画面发生明显动作变化"
    lines = _extract_key_dialogue(text)
    if lines:
        return lines[0][:20]
    return "信息较少，场景继续推进"



def add_local_understanding(evidence_list: List[Dict]) -> List[Dict]:
    total = len(evidence_list or [])
    for idx, pkg in enumerate(evidence_list or []):
        position_ratio = idx / max(total - 1, 1)
        text = (pkg.get("subtitle_text") or pkg.get("main_text_evidence") or "").strip()
        role = classify_segment_role(text, position_ratio)
        understanding = {
            "characters": list((pkg.get("entities") or {}).get("characters") or []),
            "core_event": _core_event(text, bool(pkg.get("visual_only"))),
            "key_dialogue": _extract_key_dialogue(text),
            "conflict_or_twist": text[:24] if role in {"conflict", "twist"} else None,
            "emotion": pkg.get("emotion_hint") or "平静",
        }
        pkg["plot_role"] = role
        pkg["attraction_level"] = _role_to_attraction(role)
        pkg["local_understanding"] = understanding
    return evidence_list



def build_global_summary(
    evidence_list: List[Dict],
    api_key: str = "",
    base_url: str = "",
    model: str = "",
) -> Dict:
    """Build a robust global summary even without LLM access.

    Keep the signature compatible with the current caller.
    """
    items = evidence_list or []
    if not items:
        return {
            "main_storyline": "",
            "character_relations": [],
            "unresolved_tensions": [],
            "entity_map": {},
            "arc": "unknown",
            "key_segments": [],
        }

    all_chars = []
    key_segments = []
    for pkg in items:
        und = pkg.get("local_understanding") or {}
        for c in und.get("characters") or []:
            if c not in all_chars:
                all_chars.append(c)
        if pkg.get("attraction_level") == "高":
            key_segments.append(pkg.get("segment_id"))

    main_events = [
        (pkg.get("local_understanding") or {}).get("core_event")
        for pkg in items[:6]
        if (pkg.get("local_understanding") or {}).get("core_event")
    ]
    storyline = "；".join(main_events[:4])[:60]

    tensions = []
    for pkg in items:
        twist = (pkg.get("local_understanding") or {}).get("conflict_or_twist")
        if twist and twist not in tensions:
            tensions.append(twist)

    summary = {
        "main_storyline": storyline or "视频围绕人物关系与事件推进展开。",
        "character_relations": [{"a": all_chars[i], "relation": "相关人物", "b": all_chars[i + 1]} for i in range(len(all_chars) - 1)],
        "unresolved_tensions": tensions[:6],
        "entity_map": {name: name for name in all_chars},
        "arc": items[-1].get("plot_role", "development"),
        "key_segments": [x for x in key_segments if x],
    }
    logger.info("全局剧情摘要完成: key_segments={}", len(summary["key_segments"]))
    return summary
