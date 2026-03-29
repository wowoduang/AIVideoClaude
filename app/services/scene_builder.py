from typing import Dict, List

from loguru import logger



def build_scenes_from_subtitles(
    subtitle_segments: List[Dict],
    max_scene_duration: float = 9.0,
    max_gap: float = 1.2,
    min_scene_duration: float = 1.0,
) -> List[Dict]:
    if not subtitle_segments:
        return []

    scenes: List[Dict] = []
    current = {
        "scene_id": "scene_001",
        "start": subtitle_segments[0]["start"],
        "end": subtitle_segments[0]["end"],
        "subtitle_ids": [subtitle_segments[0]["seg_id"]],
        "subtitle_texts": [subtitle_segments[0]["text"]],
    }

    for seg in subtitle_segments[1:]:
        gap = seg["start"] - current["end"]
        next_duration = seg["end"] - current["start"]
        should_split = gap > max_gap or next_duration > max_scene_duration
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

    for scene in scenes:
        if scene["end"] - scene["start"] < min_scene_duration:
            scene["end"] = scene["start"] + min_scene_duration
        scene["duration"] = scene["end"] - scene["start"]
        scene["subtitle_text"] = " ".join(scene.get("subtitle_texts", [])).strip()

    logger.info(f"场景构建完成: {len(subtitle_segments)} 条字幕 -> {len(scenes)} 个 scene")
    return scenes


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
