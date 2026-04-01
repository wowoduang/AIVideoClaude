from __future__ import annotations

import json
import os
from typing import Any, Callable, Dict, List, Optional

from loguru import logger

from app.services.align_subtitle_scene import align_subtitles_to_scenes
from app.services.evidence_fuser import fuse_scene_evidence
from app.services.generate_narration_script import generate_narration_from_scene_evidence
from app.services.plot_understanding import add_local_understanding, build_global_summary
from app.services.preflight_check import PreflightError, validate_script_items
from app.services.representative_frames import extract_representative_frames_for_scenes
from app.services.scene_builder import build_scenes
from app.services.script_fallback import ensure_script_shape
from app.services.subtitle_mode_presets import resolve_subtitle_mode_preset, resolve_visual_mode
from app.services.subtitle_pipeline import build_subtitle_segments
from app.utils import utils


def run_subtitle_first_pipeline(
    video_path: str,
    subtitle_path: str = "",
    *,
    text_api_key: str = "",
    text_base_url: str = "",
    text_model: str = "",
    style: str = "short_drama",
    keyframe_dir: str = "",
    output_script_path: str = "",
    generation_mode: str = "balanced",
    visual_mode: str = "",
    scene_overrides: Optional[Dict[str, Any]] = None,
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
            keyframe_dir=keyframe_dir,
            output_script_path=output_script_path,
            generation_mode=generation_mode,
            visual_mode=visual_mode,
            scene_overrides=scene_overrides,
            progress=_progress,
        )
    except Exception as exc:
        logger.error(f"字幕优先管线执行失败: {exc}")
        return {
            "script_items": [],
            "script_path": "",
            "success": False,
            "error": str(exc),
        }


def _run(
    video_path: str,
    subtitle_path: str,
    text_api_key: str,
    text_base_url: str,
    text_model: str,
    style: str,
    keyframe_dir: str,
    output_script_path: str,
    generation_mode: str,
    visual_mode: str,
    scene_overrides: Optional[Dict[str, Any]],
    progress: Callable[[int, str], None],
) -> Dict[str, Any]:
    preset = resolve_subtitle_mode_preset(generation_mode, overrides=scene_overrides)
    effective_visual_mode = resolve_visual_mode(visual_mode, preset)

    # M2 + M3
    progress(10, "字幕解析与标准化...")
    sub_result = build_subtitle_segments(
        video_path=video_path,
        explicit_subtitle_path=subtitle_path,
    )
    segments = sub_result["segments"]
    if not segments:
        raise ValueError(
            f"无法获取有效字幕 (source={sub_result['source']}, error={sub_result.get('error', '')})"
        )
    logger.info(
        f"M2+M3 完成: {len(segments)} 段标准化字幕, source={sub_result.get('source')}, "        f"subtitle_path={sub_result.get('subtitle_path', '') or 'NONE'}"
    )

    # M4
    progress(20, "场景切分...")
    keyframe_files = _collect_keyframes(keyframe_dir)
    scenes = build_scenes(
        subtitle_segments=segments,
        video_path=video_path,
        keyframe_files=keyframe_files,
        mode=generation_mode,
        preset=preset,
    )
    if not scenes:
        raise ValueError("场景切分失败，未生成任何 scene")
    logger.info(
        f"M4 完成: {len(scenes)} 个场景 "
        f"(mode={generation_mode}, visual_mode={effective_visual_mode})"
    )

    # M5
    progress(30, "字幕-场景对齐...")
    aligned_scenes = align_subtitles_to_scenes(segments, scenes)
    logger.info(f"M5 完成: {len(aligned_scenes)} 个对齐场景")

    # M6
    progress(40, "代表帧选取...")
    frame_records: List[Dict[str, Any]] = []

    if effective_visual_mode != "off":
        frame_records = extract_representative_frames_for_scenes(
            video_path=video_path,
            scenes=aligned_scenes,
            visual_mode=effective_visual_mode,
            max_frames_dialogue=int(preset.get("visual_max_frames_dialogue", 1)),
            max_frames_visual_only=int(preset.get("visual_max_frames_visual_only", 3)),
            max_frames_long_scene=int(preset.get("visual_max_frames_long_scene", 3)),
        )

    # 兼容旧逻辑：如果没抽出任何帧，且用户提供了 keyframe_dir，仍可继续纯字幕模式
    if frame_records:
        logger.info(f"M6 完成: {len(frame_records)} 张代表帧")
    else:
        logger.info("M6 跳过或无结果: 使用纯字幕/弱视觉模式")

    # M7
    progress(50, "证据融合...")
    evidence = fuse_scene_evidence(
        scenes=aligned_scenes,
        frame_records=frame_records,
        visual_observations={},
    )
    _enrich_evidence_with_alignment(evidence, aligned_scenes)
    logger.info(f"M7 完成: {len(evidence)} 个证据包")

    # M8
    progress(55, "剧情理解...")
    evidence = add_local_understanding(evidence)
    global_summary = build_global_summary(
        evidence,
        api_key=text_api_key,
        base_url=text_base_url,
        model=text_model,
    )
    logger.info(
        f"M8 完成: arc={global_summary.get('arc')}, "
        f"key_segments={len(global_summary.get('key_segments', []))}"
    )

    # M9
    progress(60, "两阶段脚本生成...")
    script_items = generate_narration_from_scene_evidence(
        scene_evidence=evidence,
        api_key=text_api_key,
        base_url=text_base_url,
        model=text_model,
        style=style,
    )
    if not script_items:
        logger.warning("LLM 脚本生成为空，使用兜底方案")
        script_items = _build_fallback_script(evidence)
    logger.info(f"M9 完成: {len(script_items)} 个脚本片段")

    # M10
    progress(80, "时间线预算校正...")
    script_items = ensure_script_shape(script_items)

    # M11
    progress(85, "预检验证...")
    try:
        validate_script_items(script_items)
        logger.info("M11 预检通过")
    except PreflightError as e:
        logger.warning(f"M11 预检警告 (非致命): {e}")

    # Save
    progress(90, "保存脚本文件...")
    if not output_script_path:
        video_hash = utils.md5(video_path + str(os.path.getmtime(video_path)))
        output_script_path = os.path.join(
            utils.script_dir(),
            f"{video_hash}_subtitle_first.json",
        )

    os.makedirs(os.path.dirname(output_script_path), exist_ok=True)
    with open(output_script_path, "w", encoding="utf-8") as f:
        json.dump(script_items, f, ensure_ascii=False, indent=2)
    logger.success(f"脚本已保存: {output_script_path}")

    progress(100, "管线完成")
    return {
        "script_items": script_items,
        "script_path": output_script_path,
        "evidence": evidence,
        "global_summary": global_summary,
        "preset": preset,
        "generation_mode": generation_mode,
        "visual_mode": effective_visual_mode,
        "subtitle_path": sub_result.get("subtitle_path", ""),
        "subtitle_source": sub_result.get("source", "none"),
        "subtitle_segments": len(segments),
        "generated_saved_subtitle_path": sub_result.get("generated_saved_path", ""),
        "generated_temp_subtitle_path": sub_result.get("generated_temp_path", ""),
        "success": True,
        "error": "",
    }


def _collect_keyframes(keyframe_dir: str) -> List[str]:
    if not keyframe_dir or not os.path.isdir(keyframe_dir):
        return []
    extensions = {".jpg", ".jpeg", ".png", ".bmp", ".webp"}
    files = sorted(
        f for f in [
            os.path.join(keyframe_dir, name)
            for name in os.listdir(keyframe_dir)
        ]
        if os.path.isfile(f) and os.path.splitext(f)[1].lower() in extensions
    )
    return files


def _enrich_evidence_with_alignment(
    evidence: List[Dict],
    aligned_scenes: List[Dict],
) -> None:
    scene_map = {s["scene_id"]: s for s in aligned_scenes}
    for pkg in evidence:
        aligned = scene_map.get(pkg["scene_id"], {})
        aligned_text = aligned.get("aligned_subtitle_text", "")
        if aligned_text:
            pkg["subtitle_text"] = aligned_text
        pkg["visual_only"] = aligned.get("visual_only", False)
        pkg["evidence_mode"] = "visual_only" if pkg["visual_only"] else "subtitle_first"


def _build_fallback_script(evidence: List[Dict]) -> List[Dict]:
    items: List[Dict] = []
    for idx, pkg in enumerate(evidence, start=1):
        subtitle_text = (pkg.get("subtitle_text") or "").strip()
        narration = subtitle_text[:60] if subtitle_text else "这一段的关键信息已经出现。"
        items.append({
            "_id": idx,
            "timestamp": pkg.get("timestamp", ""),
            "picture": subtitle_text[:30] if subtitle_text else "画面出现新的信息点",
            "narration": narration,
            "OST": 2,
        })
    return items