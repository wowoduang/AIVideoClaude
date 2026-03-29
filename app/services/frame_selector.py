import os
from typing import Dict, List

from loguru import logger


def parse_keyframe_timestamp(path: str) -> float:
    filename = os.path.basename(path)
    parts = filename.split("_")
    if len(parts) < 3:
        return 0.0
    raw = parts[-1].split(".")[0]
    if raw.isdigit() and len(raw) >= 9:
        h = int(raw[0:2])
        m = int(raw[2:4])
        s = int(raw[4:6])
        ms = int(raw[6:9])
        return h * 3600 + m * 60 + s + ms / 1000.0
    try:
        return int(raw) / 1000.0
    except Exception:
        return 0.0


def select_representative_frames(
    scenes: List[Dict],
    keyframe_files: List[str],
    frames_per_scene: int = 2,
) -> List[Dict]:
    if not scenes or not keyframe_files:
        return []

    indexed = [{"frame_path": p, "timestamp_seconds": parse_keyframe_timestamp(p)} for p in keyframe_files]
    records: List[Dict] = []

    for scene in scenes:
        scene_frames = [f for f in indexed if scene["start"] <= f["timestamp_seconds"] <= scene["end"]]
        if not scene_frames:
            center = (scene["start"] + scene["end"]) / 2
            indexed_sorted = sorted(indexed, key=lambda x: abs(x["timestamp_seconds"] - center))
            scene_frames = indexed_sorted[:1]
        selected = _pick_sparse(scene_frames, frames_per_scene)
        for item in selected:
            records.append({
                "scene_id": scene["scene_id"],
                "frame_path": item["frame_path"],
                "timestamp_seconds": item["timestamp_seconds"],
            })
    logger.info(f"代表帧选择完成: {len(records)} 张，平均每个scene约 {frames_per_scene} 张")
    return records


def _pick_sparse(items: List[Dict], limit: int) -> List[Dict]:
    if not items:
        return []
    if len(items) <= limit:
        return items
    items = sorted(items, key=lambda x: x["timestamp_seconds"])
    if limit == 1:
        return [items[len(items) // 2]]
    if limit == 2:
        return [items[0], items[-1]]
    result = [items[0]]
    step = (len(items) - 1) / (limit - 1)
    for i in range(1, limit - 1):
        result.append(items[round(i * step)])
    result.append(items[-1])
    return result
