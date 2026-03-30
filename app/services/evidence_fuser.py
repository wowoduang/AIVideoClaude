import re
from typing import Dict, List

from loguru import logger
from app.utils import utils
from app.services.timeline_allocator import estimate_char_budget


# ── Simple keyword-based emotion hints ──────────────────────────
_EMOTION_KEYWORDS: List[tuple] = [
    ("angry", ["愤怒", "生气", "愤懨", "发火", "可恶", "混蛋", "该死", "滚", "打死"]),
    ("sad", ["伤心", "难过", "哭", "泪", "悲伤", "心痛", "想念", "离开", "再见"]),
    ("happy", ["开心", "高兴", "笑", "哈哈", "太好了", "棒", "幸福", "喜欢"]),
    ("surprise", ["呃", "不会吧", "真的吗", "怎么可能", "惊讶", "天啊", "我的天"]),
    ("fear", ["害怕", "恐惧", "可怕", "打死我", "救命", "危险", "小心"]),
    ("tension", ["反转", "竟然", "没想到", "秘密", "真相", "隐藏", "暗中"]),
]


def _detect_emotion(text: str) -> str:
    """Detect a simple emotion hint from subtitle text via keyword matching."""
    if not text:
        return "neutral"
    for emotion, keywords in _EMOTION_KEYWORDS:
        for kw in keywords:
            if kw in text:
                return emotion
    return "neutral"


# ── Lightweight entity extraction ───────────────────────────────
_ENTITY_PATTERNS = [
    # Quoted names / terms in Chinese
    ("quoted", re.compile(r"[「『“\"]’([^」』”\"]+)[」』”\"]’")),
    # Titles / honorifics followed by a name
    ("person", re.compile(r"(?:老师|医生|老板|总裁|局长|队长|老大|小姐|先生|女士)(?:[A-Za-z一-鿿]{1,4})")),
    # Place names ending with common suffixes
    ("place", re.compile(r"[一-鿿]{2,4}(?:市|区|县|镇|村|山|河|湖|公司|医院|学校)")),
]


def _extract_entities(text: str) -> List[str]:
    """Extract simple named entities from text using regex patterns."""
    if not text:
        return []
    entities: List[str] = []
    seen: set = set()
    for _label, pattern in _ENTITY_PATTERNS:
        for m in pattern.finditer(text):
            entity = m.group(0).strip()
            if entity and entity not in seen:
                seen.add(entity)
                entities.append(entity)
    return entities


def parse_visual_analysis_results(results: List[Dict], selected_frames: List[Dict]) -> Dict[str, List[Dict]]:
    frame_map = {record["frame_path"]: record for record in selected_frames}
    observations: Dict[str, List[Dict]] = {}
    file_cursor = 0
    ordered_paths = [item["frame_path"] for item in selected_frames]

    for result in results or []:
        if "error" in result:
            continue
        response = result.get("response", "")
        parsed = _try_parse_json(response)
        if not parsed:
            continue
        frame_observations = parsed.get("frame_observations", []) or []
        for obs in frame_observations:
            if file_cursor >= len(ordered_paths):
                break
            frame_path = ordered_paths[file_cursor]
            scene_id = frame_map.get(frame_path, {}).get("scene_id", "unknown")
            observations.setdefault(scene_id, []).append({
                "frame_path": frame_path,
                "timestamp_seconds": frame_map.get(frame_path, {}).get("timestamp_seconds", 0.0),
                "observation": obs.get("observation", "").strip(),
            })
            file_cursor += 1
    return observations


def fuse_scene_evidence(
    scenes: List[Dict],
    frame_records: List[Dict],
    visual_observations: Dict[str, List[Dict]],
    context_prev: int = 2,
    context_next: int = 1,
) -> List[Dict]:
    """Construct evidence packages from aligned scenes and frame data.

    Parameters
    ----------
    scenes : list
        Aligned scene dicts.
    frame_records : list
        Frame selection records.
    visual_observations : dict
        Per-scene visual observation dicts.
    context_prev : int
        Number of preceding segments’ summaries to include in the
        ``context_window`` (PRD default: 2).
    context_next : int
        Number of following segments’ summaries to include (PRD default: 1).
    """
    scene_frames: Dict[str, List[Dict]] = {}
    for record in frame_records or []:
        scene_frames.setdefault(record["scene_id"], []).append(record)

    evidence: List[Dict] = []
    scene_list = list(scenes or [])

    for idx, scene in enumerate(scene_list):
        scene_id = scene["scene_id"]
        frames = sorted(scene_frames.get(scene_id, []), key=lambda x: x["timestamp_seconds"])
        visuals = visual_observations.get(scene_id, [])
        visual_summary = " ".join([v.get("observation", "") for v in visuals[:3]]).strip()
        duration = scene["end"] - scene["start"]
        char_budget = estimate_char_budget(duration)
        subtitle_text = scene.get("subtitle_text", "")

        # PRD M7: entities extraction
        entities = _extract_entities(subtitle_text)

        # PRD M7: emotion_hint
        emotion_hint = _detect_emotion(subtitle_text)

        # PRD M7: confidence (average of subtitle segment confidences)
        confidence = _compute_scene_confidence(scene)

        # PRD M7: context_window (prev N + next M segment summaries)
        context_window = _build_context_window(
            scene_list, idx, context_prev, context_next
        )

        evidence.append({
            "scene_id": scene_id,
            "start": scene["start"],
            "end": scene["end"],
            "timestamp": f"{utils.format_time(scene['start'])}-{utils.format_time(scene['end'])}",
            "subtitle_ids": scene.get("subtitle_ids", []),
            "subtitle_text": subtitle_text,
            "frame_paths": [f["frame_path"] for f in frames],
            "visual_summary": visual_summary,
            "char_budget": char_budget,
            "entities": entities,
            "emotion_hint": emotion_hint,
            "confidence": confidence,
            "context_window": context_window,
        })
    logger.info(f"证据融合完成: {len(evidence)} 个 scene evidence")
    return evidence


def _compute_scene_confidence(scene: Dict) -> float:
    """Compute average confidence for a scene from its subtitle segments."""
    # If aligned subtitle segments carry confidence, average them
    # Otherwise default to 1.0 (external subtitles are high confidence)
    confidences = scene.get("_subtitle_confidences", [])
    if not confidences:
        return 1.0
    return round(sum(confidences) / len(confidences), 3)


def _build_context_window(
    scenes: List[Dict], current_idx: int, prev_n: int, next_n: int,
) -> Dict[str, List[str]]:
    """Build a context window with summaries from neighboring scenes."""
    prev_summaries: List[str] = []
    for i in range(max(0, current_idx - prev_n), current_idx):
        text = (scenes[i].get("subtitle_text", "") or "").strip()
        if text:
            prev_summaries.append(text[:60])

    next_summaries: List[str] = []
    for i in range(current_idx + 1, min(len(scenes), current_idx + 1 + next_n)):
        text = (scenes[i].get("subtitle_text", "") or "").strip()
        if text:
            next_summaries.append(text[:60])

    return {"prev": prev_summaries, "next": next_summaries}


def _try_parse_json(response_text: str):
    import json

    if not response_text:
        return None
    content = response_text.strip()
    try:
        if "```json" in content:
            content = content.split("```json", 1)[1].split("```", 1)[0].strip()
        elif "```" in content:
            content = content.split("```", 1)[1].split("```", 1)[0].strip()
        return json.loads(content)
    except Exception:
        return None
