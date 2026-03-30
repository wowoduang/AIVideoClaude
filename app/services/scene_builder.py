from typing import Dict, List

from loguru import logger


# Default thresholds per PRD
_FORCE_SPLIT_GAP = 4.0        # Subtitle gap > 4s forces scene boundary
_MICRO_SCENE_THRESHOLD = 2.0  # Scenes shorter than 2s are micro-scenes
_KEYFRAME_CANDIDATES = 4      # Max keyframe candidate timestamps per scene


def build_scenes_from_subtitles(
    subtitle_segments: List[Dict],
    max_scene_duration: float = 9.0,
    max_gap: float = 1.2,
    min_scene_duration: float = 1.0,
    force_split_gap: float = _FORCE_SPLIT_GAP,
    merge_micro: bool = True,
    micro_threshold: float = _MICRO_SCENE_THRESHOLD,
) -> List[Dict]:
    """Build scene segments from subtitle timing.

    Parameters
    ----------
    subtitle_segments : list
        Normalized subtitle dicts with ``start``, ``end``, ``text``, ``seg_id``.
    max_scene_duration : float
        Maximum allowed scene duration before forcing a split.
    max_gap : float
        Normal gap threshold – consecutive subtitles with a gap larger
        than this start a new scene.
    min_scene_duration : float
        Minimum scene duration; shorter scenes are extended.
    force_split_gap : float
        Subtitle gap > this value **always** forces a scene boundary,
        even if other heuristics would merge (PRD rule: 4s).
    merge_micro : bool
        If True, micro-scenes (< *micro_threshold* seconds) are merged
        into the preceding scene.
    micro_threshold : float
        Duration below which a scene is considered a micro-scene (PRD: 2s).
    """
    if not subtitle_segments:
        return []

    scenes: List[Dict] = []
    current: Dict = {
        "scene_id": "scene_001",
        "start": subtitle_segments[0]["start"],
        "end": subtitle_segments[0]["end"],
        "subtitle_ids": [subtitle_segments[0]["seg_id"]],
        "subtitle_texts": [subtitle_segments[0]["text"]],
    }

    for seg in subtitle_segments[1:]:
        gap = seg["start"] - current["end"]
        next_duration = seg["end"] - current["start"]

        # PRD: subtitle gap > force_split_gap always forces split
        should_split = (
            gap > force_split_gap
            or gap > max_gap
            or next_duration > max_scene_duration
        )

        if should_split:
            scenes.append(current)
            current = {
                "scene_id": f"scene_{len(scenes)+1:03d}",
                "start": seg["start"],
                "end": seg["end"],
                "subtitle_ids": [seg["seg_id"]],
                "subtitle_texts": [seg["text"]],
            }
        else:
            current["end"] = max(current["end"], seg["end"])
            current["subtitle_ids"].append(seg["seg_id"])
            current["subtitle_texts"].append(seg["text"])

    scenes.append(current)

    # ── Merge micro-scenes (PRD: < 2s merged with previous) ──────────
    if merge_micro and len(scenes) > 1:
        merged: List[Dict] = [scenes[0]]
        for scene in scenes[1:]:
            duration = scene["end"] - scene["start"]
            if duration < micro_threshold:
                prev = merged[-1]
                prev["end"] = max(prev["end"], scene["end"])
                prev["subtitle_ids"].extend(scene["subtitle_ids"])
                prev["subtitle_texts"].extend(scene["subtitle_texts"])
            else:
                merged.append(scene)
        scenes = merged
        # Re-number scene_ids after merge
        for idx, scene in enumerate(scenes, start=1):
            scene["scene_id"] = f"scene_{idx:03d}"

    # ── Post-processing ───────────────────────────────────────────
    for scene in scenes:
        if scene["end"] - scene["start"] < min_scene_duration:
            scene["end"] = scene["start"] + min_scene_duration
        scene["duration"] = scene["end"] - scene["start"]
        scene["subtitle_text"] = " ".join(scene.get("subtitle_texts", [])).strip()
        # PRD: keyframe_candidates – 2-4 evenly spaced timestamps
        scene["keyframe_candidates"] = _compute_keyframe_candidates(
            scene["start"], scene["end"]
        )

    logger.info(f"场景构建完成: {len(subtitle_segments)} 条字幕 -> {len(scenes)} 个 scene")
    return scenes


def _compute_keyframe_candidates(
    start: float, end: float, max_candidates: int = _KEYFRAME_CANDIDATES,
) -> List[float]:
    """Compute evenly spaced keyframe candidate timestamps within a scene."""
    duration = end - start
    if duration <= 0:
        return [start]
    count = min(max_candidates, max(2, int(duration / 2.0) + 1))
    if count <= 1:
        return [round((start + end) / 2.0, 3)]
    step = duration / (count - 1)
    return [round(start + i * step, 3) for i in range(count)]


def build_fallback_scenes_from_keyframes(keyframe_files: List[str], fallback_interval: float = 3.0) -> List[Dict]:
    scenes: List[Dict] = []
    for idx, frame in enumerate(keyframe_files or [], start=1):
        start = (idx - 1) * fallback_interval
        end = start + fallback_interval
        scenes.append({
            "scene_id": f"scene_{idx:03d}",
            "start": start,
            "end": end,
            "duration": fallback_interval,
            "subtitle_ids": [],
            "subtitle_text": "",
            "subtitle_texts": [],
            "fallback_frame": frame,
        })
    return scenes
