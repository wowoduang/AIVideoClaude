from __future__ import annotations

import os
import tempfile
from typing import Dict, List, Optional

from loguru import logger

try:
    import cv2
    _CV2_AVAILABLE = True
except Exception:
    _CV2_AVAILABLE = False


def extract_representative_frames_for_scenes(
    video_path: str,
    scenes: List[Dict],
    *,
    visual_mode: str = "auto",
    output_dir: str = "",
    max_frames_dialogue: int = 1,
    max_frames_visual_only: int = 3,
    max_frames_long_scene: int = 3,
    long_scene_threshold: float = 30.0,
    jpeg_quality: int = 80,
    max_edge: int = 960,
) -> List[Dict]:
    if visual_mode == "off":
        logger.info("代表帧抽取关闭: visual_mode=off")
        return []

    if not _CV2_AVAILABLE:
        logger.warning("OpenCV 不可用，跳过代表帧抽取")
        return []

    if not video_path or not os.path.isfile(video_path):
        logger.warning("视频文件不存在，跳过代表帧抽取")
        return []

    if not scenes:
        return []

    if not output_dir:
        output_dir = os.path.join(tempfile.gettempdir(), "ainarra_frames")
    os.makedirs(output_dir, exist_ok=True)

    cap = cv2.VideoCapture(video_path)
    if not cap.isOpened():
        logger.warning("无法打开视频，跳过代表帧抽取")
        return []

    records: List[Dict] = []
    for scene in scenes:
        timestamps = _choose_timestamps(
            scene=scene,
            visual_mode=visual_mode,
            max_frames_dialogue=max_frames_dialogue,
            max_frames_visual_only=max_frames_visual_only,
            max_frames_long_scene=max_frames_long_scene,
            long_scene_threshold=long_scene_threshold,
        )

        for ts in timestamps:
            frame = _read_frame_at(cap, ts)
            if frame is None:
                continue
            frame = _resize_keep_ratio(frame, max_edge=max_edge)

            frame_name = f"{scene['scene_id']}_{ts:.2f}.jpg".replace(":", "_")
            frame_path = os.path.join(output_dir, frame_name)
            cv2.imwrite(frame_path, frame, [int(cv2.IMWRITE_JPEG_QUALITY), int(jpeg_quality)])

            records.append({
                "scene_id": scene["scene_id"],
                "frame_path": frame_path,
                "timestamp_seconds": round(float(ts), 3),
                "visual_only": bool(scene.get("visual_only", False)),
            })

    cap.release()
    logger.info(f"代表帧抽取完成: {len(records)} 张")
    return records


def _choose_timestamps(
    scene: Dict,
    visual_mode: str,
    max_frames_dialogue: int,
    max_frames_visual_only: int,
    max_frames_long_scene: int,
    long_scene_threshold: float,
) -> List[float]:
    start = float(scene.get("start", 0.0) or 0.0)
    end = float(scene.get("end", start) or start)
    duration = max(end - start, 0.1)
    candidates = [float(x) for x in scene.get("keyframe_candidates", []) or []]

    visual_only = bool(scene.get("visual_only", False))
    subtitle_text = (scene.get("subtitle_text") or "").strip()
    subtitle_len = len(subtitle_text)

    if visual_only:
        n = max_frames_visual_only
    elif duration >= long_scene_threshold:
        n = max_frames_long_scene
    elif visual_mode == "boost":
        n = min(max_frames_visual_only, 2 if subtitle_len >= 12 else 3)
    elif subtitle_len < 12 and duration > 5:
        n = min(max_frames_visual_only, 2)
    else:
        n = max_frames_dialogue

    if n <= 0:
        return []

    if not candidates:
        if n == 1:
            return [round((start + end) / 2.0, 3)]
        step = duration / (n + 1)
        return [round(start + step * (i + 1), 3) for i in range(n)]

    if len(candidates) <= n:
        return [round(float(x), 3) for x in candidates]

    if n == 1:
        mid = candidates[len(candidates) // 2]
        return [round(float(mid), 3)]

    idxs = []
    total = len(candidates) - 1
    for i in range(n):
        idx = round(i * total / (n - 1))
        idxs.append(idx)
    return [round(float(candidates[i]), 3) for i in idxs]


def _read_frame_at(cap, ts: float):
    cap.set(cv2.CAP_PROP_POS_MSEC, max(ts, 0.0) * 1000.0)
    ok, frame = cap.read()
    if not ok:
        return None
    return frame


def _resize_keep_ratio(frame, max_edge: int = 960):
    h, w = frame.shape[:2]
    long_edge = max(h, w)
    if long_edge <= max_edge:
        return frame
    scale = max_edge / float(long_edge)
    new_w = max(int(w * scale), 1)
    new_h = max(int(h * scale), 1)
    return cv2.resize(frame, (new_w, new_h))