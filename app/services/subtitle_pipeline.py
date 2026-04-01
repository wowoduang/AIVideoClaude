from __future__ import annotations

import os
import shutil
from typing import Dict, Tuple

from loguru import logger

from app.services import subtitle
from app.services.subtitle_normalizer import (
    dump_segments_to_srt,
    normalize_segments,
    parse_subtitle_file,
)
from app.utils import utils


CANDIDATE_SUBTITLE_ATTRS = [
    "subtitle_path",
    "subtitle_file",
    "subtitle_origin_path",
    "video_subtitle_path",
]


def resolve_explicit_subtitle_path(params=None, session_state=None) -> str:
    if params is not None:
        for attr in CANDIDATE_SUBTITLE_ATTRS:
            value = getattr(params, attr, None)
            if value:
                return value
    if session_state:
        for attr in CANDIDATE_SUBTITLE_ATTRS:
            value = session_state.get(attr)
            if value:
                return value
    return ""


def _detect_subtitle_source(subtitle_path: str) -> str:
    if not subtitle_path:
        return "none"
    ext = os.path.splitext(subtitle_path)[1].lower()
    source_map = {
        ".srt": "external_srt",
        ".ass": "external_ass",
        ".ssa": "external_ass",
        ".vtt": "external_vtt",
    }
    return source_map.get(ext, "external_srt")


def _build_generated_subtitle_paths(video_path: str) -> Tuple[str, str, str]:
    video_hash = utils.md5(video_path + str(os.path.getmtime(video_path)))
    subtitle_dir = utils.temp_dir("subtitles")
    os.makedirs(subtitle_dir, exist_ok=True)
    temp_path = os.path.join(subtitle_dir, f"{video_hash}.srt")

    persistent_dir = utils.subtitle_dir()
    os.makedirs(persistent_dir, exist_ok=True)
    video_name = os.path.splitext(os.path.basename(video_path))[0]
    safe_video_name = "".join(ch if ch.isalnum() or ch in {"-", "_"} else "_" for ch in video_name).strip("_") or "video"
    persistent_name = f"{safe_video_name}__auto_{video_hash[:8]}.srt"
    persistent_path = os.path.join(persistent_dir, persistent_name)
    return video_hash, temp_path, persistent_path


def _persist_generated_subtitle(temp_path: str, persistent_path: str) -> str:
    if not temp_path or not os.path.exists(temp_path):
        return ""
    os.makedirs(os.path.dirname(persistent_path), exist_ok=True)
    shutil.copyfile(temp_path, persistent_path)
    logger.info(f"已保存自动生成字幕到资源目录: {persistent_path}")
    return persistent_path


def build_subtitle_segments(video_path: str, explicit_subtitle_path: str = "", regenerate: bool = False) -> Dict:
    subtitle_path = explicit_subtitle_path or ""
    source = "none"
    error = ""
    persisted_generated_path = ""
    generated_temp_path = ""

    if subtitle_path and os.path.exists(subtitle_path):
        source = _detect_subtitle_source(subtitle_path)
        logger.info(f"使用外挂字幕: {subtitle_path} (format={source})")
    else:
        _, temp_path, persistent_path = _build_generated_subtitle_paths(video_path)
        generated_temp_path = temp_path

        selected_generated_path = ""
        if not regenerate:
            if os.path.exists(persistent_path):
                selected_generated_path = persistent_path
                logger.info(f"检测到已保存的自动字幕，直接复用: {persistent_path}")
            elif os.path.exists(temp_path):
                persisted_generated_path = _persist_generated_subtitle(temp_path, persistent_path)
                selected_generated_path = persisted_generated_path or temp_path
                logger.info(f"检测到缓存字幕，直接复用: {selected_generated_path}")

        if not selected_generated_path:
            logger.info("未检测到外挂字幕，开始从视频自动生成字幕")
            generated = subtitle.extract_audio_and_create_subtitle(video_path, temp_path)
            if generated and os.path.exists(generated):
                persisted_generated_path = _persist_generated_subtitle(generated, persistent_path)
                selected_generated_path = persisted_generated_path or generated
            else:
                error = "auto_subtitle_failed"

        subtitle_path = selected_generated_path
        source = "generated_srt" if subtitle_path and os.path.exists(subtitle_path) else "none"

    segments = parse_subtitle_file(subtitle_path) if subtitle_path and os.path.exists(subtitle_path) else []
    normalized = normalize_segments(segments)

    if normalized and subtitle_path and subtitle_path.lower().endswith(".srt"):
        dump_segments_to_srt(normalized, subtitle_path)

    if normalized and generated_temp_path and subtitle_path and subtitle_path != generated_temp_path:
        try:
            dump_segments_to_srt(normalized, generated_temp_path)
        except Exception as exc:
            logger.warning(f"回写缓存字幕失败: {exc}")

    if not normalized and not error and not source.startswith("external"):
        error = "empty_subtitle_segments"

    logger.info(
        f"字幕流水线完成: source={source}, segments={len(normalized)}, subtitle_path={subtitle_path or 'NONE'}"
    )
    return {
        "subtitle_path": subtitle_path if subtitle_path and os.path.exists(subtitle_path) else "",
        "segments": normalized,
        "source": source,
        "success": bool(normalized),
        "error": error,
        "generated_temp_path": generated_temp_path if generated_temp_path and os.path.exists(generated_temp_path) else "",
        "generated_saved_path": persisted_generated_path if persisted_generated_path and os.path.exists(persisted_generated_path) else (
            subtitle_path if source == "generated_srt" and subtitle_path.startswith(utils.subtitle_dir()) else ""
        ),
    }
