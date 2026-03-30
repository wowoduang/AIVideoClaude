import os
from typing import Dict

from loguru import logger

from app.services import subtitle
from app.services.subtitle_normalizer import dump_segments_to_srt, normalize_segments, parse_srt_file
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


def build_subtitle_segments(video_path: str, explicit_subtitle_path: str = "", regenerate: bool = False) -> Dict:
    subtitle_path = explicit_subtitle_path or ""
    source = "none"
    error = ""

    if subtitle_path and os.path.exists(subtitle_path):
        source = "external_srt"
        logger.info(f"使用外挂字幕: {subtitle_path}")
    else:
        subtitle_dir = utils.temp_dir("subtitles")
        os.makedirs(subtitle_dir, exist_ok=True)
        video_hash = utils.md5(video_path + str(os.path.getmtime(video_path)))
        subtitle_path = os.path.join(subtitle_dir, f"{video_hash}.srt")
        if not os.path.exists(subtitle_path) or regenerate:
            logger.info("未检测到外挂字幕，开始从视频自动生成字幕")
            generated = subtitle.extract_audio_and_create_subtitle(video_path, subtitle_path)
            if not generated or not os.path.exists(generated):
                error = "auto_subtitle_failed"
        source = "generated_srt" if os.path.exists(subtitle_path) else "none"

    segments = parse_srt_file(subtitle_path) if subtitle_path and os.path.exists(subtitle_path) else []
    normalized = normalize_segments(segments)
    if normalized and subtitle_path:
        dump_segments_to_srt(normalized, subtitle_path)
    if not normalized and not error and source != "external_srt":
        error = "empty_subtitle_segments"
    logger.info(f"字幕流水线完成: source={source}, segments={len(normalized)}")
    return {
        "subtitle_path": subtitle_path if os.path.exists(subtitle_path) else "",
        "segments": normalized,
        "source": source,
        "success": bool(normalized),
        "error": error,
    }
