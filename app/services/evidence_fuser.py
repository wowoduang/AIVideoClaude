from typing import Dict, List

from loguru import logger
from app.utils import utils
from app.services.timeline_allocator import estimate_char_budget


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


def fuse_scene_evidence(scenes: List[Dict], frame_records: List[Dict], visual_observations: Dict[str, List[Dict]]) -> List[Dict]:
    scene_frames: Dict[str, List[Dict]] = {}
    for record in frame_records or []:
        scene_frames.setdefault(record["scene_id"], []).append(record)

    evidence: List[Dict] = []
    for scene in scenes or []:
        scene_id = scene["scene_id"]
        frames = sorted(scene_frames.get(scene_id, []), key=lambda x: x["timestamp_seconds"])
        visuals = visual_observations.get(scene_id, [])
        visual_summary = " ".join([v.get("observation", "") for v in visuals[:3]]).strip()
        duration = scene["end"] - scene["start"]
        char_budget = estimate_char_budget(duration)
        evidence.append({
            "scene_id": scene_id,
            "start": scene["start"],
            "end": scene["end"],
            "timestamp": f"{utils.format_time(scene['start'])}-{utils.format_time(scene['end'])}",
            "subtitle_ids": scene.get("subtitle_ids", []),
            "subtitle_text": scene.get("subtitle_text", ""),
            "frame_paths": [f["frame_path"] for f in frames],
            "visual_summary": visual_summary,
            "char_budget": char_budget,
        })
    logger.info(f"证据融合完成: {len(evidence)} 个 scene evidence")
    return evidence


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
