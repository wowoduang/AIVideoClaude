"""
plot_first_pipeline.py
----------------------
主流水线（覆盖原有实现）。

完整流程：
A. 字幕预处理
B. 全局粗理解（第一轮LLM）
C. 视频场景切片
D. 字幕语义粗分段
E. 视频边界融合
F. 精分段 + 打分
G. 差异化抽帧
H. 分段精理解（第二轮LLM）
I. 整合生成解说（第三轮LLM）
J. 持久化输出
"""
from __future__ import annotations

import json
import os
from datetime import datetime
from typing import Any, Callable, Dict, List, Optional

from loguru import logger

from app.services.boundary_fuser import fuse_boundaries, fused_to_dict
from app.services.evidence_fuser import fuse_scene_evidence
from app.services.plot_chunker import build_plot_chunks_from_subtitles
from app.services.plot_understanding import (
    add_local_understanding,
    build_global_summary,
    build_global_understanding,
    run_all_segment_analysis,
    run_narration_integration,
    run_global_revision,
    apply_revisions_to_script,
)
from app.services.pipeline_state import PipelineState
from app.services.preflight_check import PreflightError, validate_script_items
from app.services.representative_frames import extract_representative_frames_for_scenes
from app.services.scene_detector import detect_scenes
from app.services.script_fallback import ensure_script_shape
from app.services.segment_refiner import refine_segments, refined_to_dict
from app.services.subtitle_pipeline import build_subtitle_segments
from app.utils import utils


DEFAULT_SOURCE_TEXT_MAP = {
    "external_srt": "外挂 SRT 字幕",
    "external_ass": "外挂 ASS/SSA 字幕",
    "external_vtt": "外挂 VTT 字幕",
    "generated_srt": "自动生成字幕",
}


def run_plot_first_pipeline(
    video_path: str,
    subtitle_path: str = "",
    *,
    text_api_key: str = "",
    text_base_url: str = "",
    text_model: str = "",
    style: str = "short_drama",
    visual_mode: str = "boost",
    regenerate_subtitle: bool = False,
    film_type: str = "auto",          # auto|action|dialogue|documentary
    target_duration: int = 0,         # 解说目标时长（秒），0=自动
    style_examples: str = "",         # 风格参考文案
    progress_callback: Optional[Callable[[int, str], None]] = None,
) -> Dict[str, Any]:

    def _progress(pct: int, msg: str = "") -> None:
        if progress_callback:
            try:
                progress_callback(pct, msg)
            except Exception:
                pass

    try:
        return _run(
            video_path=video_path,
            subtitle_path=subtitle_path,
            text_api_key=text_api_key,
            text_base_url=text_base_url,
            text_model=text_model,
            style=style,
            visual_mode=visual_mode,
            regenerate_subtitle=regenerate_subtitle,
            film_type=film_type,
            target_duration=target_duration,
            style_examples=style_examples,
            progress=_progress,
        )
    except Exception as exc:
        logger.exception("剧情优先管线执行失败: {}", exc)
        return {
            "success": False, "error": str(exc),
            "subtitle_result": {}, "plot_chunks": [],
            "refined_segments": [], "frame_records": [],
            "scene_evidence": [], "global_summary": {},
            "script_items": [], "script_path": "", "analysis_path": "",
            "frame_output_dir": "",
        }


def _run(
    video_path: str,
    subtitle_path: str,
    text_api_key: str,
    text_base_url: str,
    text_model: str,
    style: str,
    visual_mode: str,
    regenerate_subtitle: bool,
    film_type: str,
    target_duration: int,
    style_examples: str,
    progress: Callable[[int, str], None],
) -> Dict[str, Any]:

    if not video_path or not os.path.isfile(video_path):
        raise FileNotFoundError(f"视频文件不存在: {video_path}")

    video_hash = utils.md5(video_path + str(os.path.getmtime(video_path)))
    output_paths = _build_output_paths(video_hash)
    state = PipelineState(video_id=video_hash, cache_dir=output_paths["state_dir"])

    # ── A. 字幕预处理 ────────────────────────────────────────
    progress(8, "提取字幕...")
    subtitle_result = build_subtitle_segments(
        video_path=video_path,
        explicit_subtitle_path=subtitle_path,
        regenerate=regenerate_subtitle,
    )
    subtitle_segments = subtitle_result.get("segments") or []
    if not subtitle_segments:
        raise ValueError(
            f"无法获取有效字幕 (source={subtitle_result.get('source')}, "
            f"error={subtitle_result.get('error')})"
        )
    logger.info("A 字幕预处理完成: segments={}", len(subtitle_segments))

    # ── B. 全局粗理解（第一轮LLM）────────────────────────────
    progress(18, "全局剧情理解（第一轮LLM）...")
    global_bible = build_global_understanding(
        subtitle_segments=subtitle_segments,
        api_key=text_api_key,
        base_url=text_base_url,
        model=text_model,
    )
    state.set_global_bible(global_bible)
    logger.info("B 全局理解完成: summary={}", global_bible.story_summary[:40])

    # ── C. 视频场景切片 ──────────────────────────────────────
    progress(28, "视频场景检测...")
    try:
        scenes = detect_scenes(video_path, film_type=film_type)
        logger.info("C 场景检测完成: {} 个场景", len(scenes))
    except Exception as e:
        logger.warning("场景检测失败，跳过视频边界: {}", e)
        scenes = []

    # ── D. 字幕语义粗分段 ────────────────────────────────────
    progress(36, "字幕语义分段...")
    # 获取视频时长，传给分段器用于补全末尾无声段
    video_duration = 0.0
    try:
        from app.services.media_duration import get_video_duration
        video_duration = get_video_duration(video_path) or 0.0
    except Exception:
        pass
    plot_chunks = build_plot_chunks_from_subtitles(
        subtitle_segments,
        video_duration=video_duration,
        fill_gaps=True,
        gap_threshold=10.0,
    )
    if not plot_chunks:
        raise ValueError("剧情块构建失败，未生成任何 plot chunk")
    logger.info("D 粗分段完成: {} 个剧情块", len(plot_chunks))

    # ── E. 视频边界融合 ──────────────────────────────────────
    progress(44, "融合视频边界...")
    fused = fuse_boundaries(plot_chunks, scenes)
    fused_dicts = [fused_to_dict(f) for f in fused]
    logger.info("E 边界融合完成: {} 个融合段", len(fused_dicts))

    # ── F. 精分段 + 打分 ─────────────────────────────────────
    progress(50, "精分段打分...")
    # 把 narrative_warnings 传给精分段，提升对应位置的歧义评分
    warnings_for_refiner = global_bible.narrative_warnings if global_bible else []
    refined = refine_segments(fused, narrative_warnings=warnings_for_refiner)
    refined_dicts = [refined_to_dict(r) for r in refined]
    logger.info("F 精分段完成: {} 个段落", len(refined_dicts))

    # ── G. 差异化抽帧 ────────────────────────────────────────
    progress(58, "差异化抽帧...")
    frame_output_dir = output_paths["frame_output_dir"]
    frame_records = _extract_frames_with_strategy(
        video_path=video_path,
        segments=refined_dicts,
        output_dir=frame_output_dir,
        visual_mode=visual_mode,
    )
    logger.info("G 抽帧完成: {} 张", len(frame_records))

    # ── H. 分段精理解（第二轮LLM）───────────────────────────
    progress(65, "分段精理解（第二轮LLM）...")
    segment_cards = run_all_segment_analysis(
        state=state,
        segments=refined_dicts,
        frame_records=frame_records,
        api_key=text_api_key,
        base_url=text_base_url,
        model=text_model,
    )
    logger.info("H 分段精理解完成: {} 段", len(segment_cards))

    # ── I. 整合生成解说（第三轮LLM）─────────────────────────
    progress(80, "整合生成解说文案（第三轮LLM）...")
    if target_duration <= 0:
        # 默认：原片时长的1/3
        total_dur = sum(r.end - r.start for r in refined)
        target_duration = max(60, int(total_dur / 3))

    script_items = run_narration_integration(
        state=state,
        target_duration=target_duration,
        style_examples=style_examples,
        api_key=text_api_key,
        base_url=text_base_url,
        model=text_model,
    )
    script_items = ensure_script_shape(script_items)
    if not script_items:
        raise ValueError("未生成有效脚本片段")

    # ── I-2. 全局回修（对应会话共识第10步）────────────────
    progress(86, "全局一致性回修...")
    try:
        revisions = run_global_revision(
            state=state,
            api_key=text_api_key,
            base_url=text_base_url,
            model=text_model,
        )
        if revisions:
            script_items = apply_revisions_to_script(script_items, revisions)
            script_items = ensure_script_shape(script_items)
            logger.info("全局回修完成：{} 条修订", len(revisions))
    except Exception as e:
        logger.warning("全局回修失败，跳过: {}", e)

    # 兜底：补充 evidence 信息供旧版下游使用
    scene_evidence = fuse_scene_evidence(
        scenes=refined_dicts,
        frame_records=frame_records,
        visual_observations={},
    )
    scene_evidence = add_local_understanding(scene_evidence)
    global_summary = build_global_summary(scene_evidence)
    for pkg in scene_evidence:
        pkg["_global_summary"] = global_summary
        pkg["evidence_mode"] = "plot_first_v2"

    warnings: List[str] = []
    try:
        validate_script_items(script_items)
    except PreflightError as exc:
        warnings.append(str(exc))
        logger.warning("脚本预检警告: {}", exc)

    # ── J. 持久化 ────────────────────────────────────────────
    progress(92, "保存结果...")
    state_path = state.save()
    analysis_payload = {
        "mode": "plot_first_v2",
        "subtitle_result": subtitle_result,
        "subtitle_segments": subtitle_segments,
        "global_bible": global_bible.__dict__,
        "plot_chunks": plot_chunks,
        "refined_segments": refined_dicts,
        "frame_records": frame_records,
        "scene_evidence": scene_evidence,
        "global_summary": global_summary,
        "script_items": script_items,
        "warnings": warnings,
        "state_path": state_path,
    }
    _write_json(output_paths["analysis_path"], analysis_payload)
    _write_json(output_paths["script_path"], script_items)
    logger.success("流水线完成: script_items={}, path={}", len(script_items), output_paths["script_path"])

    progress(100, "完成")
    return {
        "success": True, "error": "",
        "subtitle_result": subtitle_result,
        "subtitle_source_text": DEFAULT_SOURCE_TEXT_MAP.get(
            subtitle_result.get("source"), subtitle_result.get("source") or "未知来源"
        ),
        "global_bible": global_bible.__dict__,
        "plot_chunks": plot_chunks,
        "refined_segments": refined_dicts,
        "frame_records": frame_records,
        "scene_evidence": scene_evidence,
        "global_summary": global_summary,
        "script_items": script_items,
        "script_path": output_paths["script_path"],
        "analysis_path": output_paths["analysis_path"],
        "frame_output_dir": frame_output_dir,
        "state_path": state_path,
        "warnings": warnings,
    }


# ── 差异化抽帧 ────────────────────────────────────────────────

def _extract_frames_with_strategy(
    video_path: str,
    segments: List[Dict],
    output_dir: str,
    visual_mode: str = "boost",
) -> List[Dict]:
    """
    根据精分段的 frame_count 和 frame_strategy 差异化抽帧。
    高重要性/高歧义段多抽，低重要性段少抽。
    """
    records = []
    try:
        import cv2
        cap = cv2.VideoCapture(video_path)
        os.makedirs(output_dir, exist_ok=True)

        for seg in segments:
            seg_id = seg.get("segment_id", "")
            start = float(seg.get("start", 0))
            end = float(seg.get("end", 0))
            frame_count = int(seg.get("frame_count", 1))
            strategy = seg.get("frame_strategy", "center")

            # visual_mode=off 时跳过
            if visual_mode == "off":
                continue
            # skip 类型不抽帧
            if seg.get("segment_type") == "skip":
                continue

            timestamps = _choose_timestamps(start, end, frame_count, strategy,
                                            seg.get("keyframe_candidates", []))
            for ts in timestamps:
                cap.set(cv2.CAP_PROP_POS_MSEC, max(ts, 0) * 1000)
                ok, frame = cap.read()
                if not ok:
                    continue
                # 缩放
                h, w = frame.shape[:2]
                if max(h, w) > 960:
                    scale = 960 / max(h, w)
                    frame = cv2.resize(frame, (max(1, int(w * scale)), max(1, int(h * scale))))
                name = f"{seg_id}_{ts:.2f}.jpg".replace(":", "_")
                path = os.path.join(output_dir, name)
                cv2.imwrite(path, frame, [int(cv2.IMWRITE_JPEG_QUALITY), 80])
                records.append({
                    "segment_id": seg_id,
                    "scene_id": seg_id,
                    "frame_path": path,
                    "timestamp_seconds": round(ts, 3),
                    "visual_only": bool(seg.get("visual_only")),
                })

        cap.release()
    except Exception as e:
        logger.warning("差异化抽帧失败，跳过: {}", e)

    return records


def _choose_timestamps(
    start: float, end: float,
    count: int, strategy: str,
    candidates: List[float],
) -> List[float]:
    duration = max(end - start, 0.1)
    if candidates:
        if len(candidates) <= count:
            return [round(float(c), 3) for c in candidates]
        if count == 1:
            return [round(float(candidates[len(candidates) // 2]), 3)]
        idxs = [round(i * (len(candidates) - 1) / (count - 1)) for i in range(count)]
        return [round(float(candidates[i]), 3) for i in idxs]

    if count == 1 or strategy == "center":
        return [round((start + end) / 2, 3)]
    if strategy == "first_mid_last":
        return [round(start + 1, 3), round((start + end) / 2, 3), round(end - 1, 3)]
    # spread
    step = duration / (count + 1)
    return [round(start + step * (i + 1), 3) for i in range(count)]


# ── 工具函数 ──────────────────────────────────────────────────

def _build_output_paths(video_hash: str) -> Dict[str, str]:
    now = datetime.now().strftime("%Y%m%d_%H%M%S")
    analysis_dir = os.path.join(utils.storage_dir(), "temp", "analysis")
    frame_output_dir = os.path.join(utils.temp_dir("plot_frames"), video_hash)
    state_dir = os.path.join(utils.storage_dir(), "temp", "pipeline_state")
    script_path = os.path.join(utils.script_dir(), f"{video_hash}_plot_first_{now}.json")
    analysis_path = os.path.join(analysis_dir, f"{video_hash}_plot_first_{now}.json")

    for d in [analysis_dir, frame_output_dir, state_dir,
              os.path.dirname(script_path)]:
        os.makedirs(d, exist_ok=True)

    return {
        "analysis_path": analysis_path,
        "script_path": script_path,
        "frame_output_dir": frame_output_dir,
        "state_dir": state_dir,
    }


def _write_json(path: str, payload: Any) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
