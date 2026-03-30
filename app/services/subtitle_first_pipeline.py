"""End-to-end subtitle-first pipeline orchestrator.

Connects all modules in the subtitle-first narration video workflow:

  Video + Subtitle file
    -> M2  Subtitle acquisition (SRT / ASS / VTT or ASR)
    -> M3  Subtitle normalization
    -> M4  Scene segmentation
    -> M5  Subtitle-scene alignment
    -> M6  Representative frame extraction
    -> M7  Evidence package construction
    -> M8  Plot understanding (local + global)
    -> M9  Two-stage script generation (facts -> polish)
    -> M10 Timeline budget enforcement
    -> M11 Preflight validation
    -> Save final script JSON

The output JSON file is directly consumable by the existing video
generation pipeline (task.py -> start_subclip_unified).
"""

import json
import os
from typing import Any, Callable, Dict, List, Optional

from loguru import logger

from app.services.align_subtitle_scene import align_subtitles_to_scenes
from app.services.evidence_fuser import fuse_scene_evidence
from app.services.frame_selector import select_representative_frames
from app.services.generate_narration_script import generate_narration_from_scene_evidence
from app.services.plot_understanding import add_local_understanding, build_global_summary
from app.services.preflight_check import PreflightError, validate_script_items
from app.services.scene_builder import build_scenes_from_subtitles
from app.services.script_fallback import ensure_script_shape
from app.services.subtitle_pipeline import build_subtitle_segments
from app.utils import utils


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

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
    progress_callback: Optional[Callable[[int, str], None]] = None,
) -> Dict[str, Any]:
    """Run the complete subtitle-first pipeline.

    Parameters
    ----------
    video_path : str
        Path to the source video file.
    subtitle_path : str
        Path to an external subtitle file (SRT/ASS/VTT).  If empty, the
        pipeline will attempt ASR-based subtitle generation.
    text_api_key : str
        API key for the text LLM used in script generation.
    text_base_url : str
        Base URL for the text LLM API.
    text_model : str
        Model identifier for the text LLM.
    style : str
        Narration style – ``"documentary"`` or ``"short_drama"``.
    keyframe_dir : str
        Directory containing pre-extracted keyframe images.  If empty,
        frame-based evidence will be skipped (subtitle-only mode).
    output_script_path : str
        Where to write the final script JSON.  Defaults to
        ``resource/scripts/<video_hash>_subtitle_first.json``.
    progress_callback : callable, optional
        ``(percent: int, message: str) -> None`` called at each stage.

    Returns
    -------
    dict with keys:
        ``script_items`` – the final script list,
        ``script_path``  – path to the saved JSON file,
        ``success``      – bool,
        ``error``        – error message (empty on success).
    """

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


# ---------------------------------------------------------------------------
# Internal implementation
# ---------------------------------------------------------------------------

def _run(
    video_path: str,
    subtitle_path: str,
    text_api_key: str,
    text_base_url: str,
    text_model: str,
    style: str,
    keyframe_dir: str,
    output_script_path: str,
    progress: Callable[[int, str], None],
) -> Dict[str, Any]:

    # ── M2 + M3: Subtitle acquisition & normalization ──────────────
    progress(10, "字幕解析与标准化...")
    sub_result = build_subtitle_segments(
        video_path=video_path,
        explicit_subtitle_path=subtitle_path,
    )
    segments = sub_result["segments"]
    if not segments:
        raise ValueError(
            f"无法获取有效字幕 (source={sub_result['source']}, "
            f"error={sub_result.get('error', '')})"
        )
    logger.info(f"M2+M3 完成: {len(segments)} 段标准化字幕")

    # ── M4: Scene segmentation ─────────────────────────────────────
    progress(20, "场景切分...")
    scenes = build_scenes_from_subtitles(segments)
    if not scenes:
        raise ValueError("场景切分失败，未生成任何 scene")
    logger.info(f"M4 完成: {len(scenes)} 个场景")

    # ── M5: Subtitle-scene alignment ──────────────────────────────
    progress(30, "字幕-场景对齐...")
    aligned_scenes = align_subtitles_to_scenes(segments, scenes)
    logger.info(f"M5 完成: {len(aligned_scenes)} 个对齐场景")

    # ── M6: Representative frame extraction ────────────────────────
    progress(40, "代表帧选取...")
    keyframe_files = _collect_keyframes(keyframe_dir)
    frame_records: List[Dict] = []
    if keyframe_files:
        frame_records = select_representative_frames(aligned_scenes, keyframe_files)
        logger.info(f"M6 完成: {len(frame_records)} 张代表帧")
    else:
        logger.info("M6 跳过: 未提供关键帧目录，使用纯字幕模式")

    # ── M7: Evidence package construction ──────────────────────────
    progress(50, "证据融合...")
    evidence = fuse_scene_evidence(
        scenes=aligned_scenes,
        frame_records=frame_records,
        visual_observations={},  # No vision LLM analysis in this pipeline
    )
    # Enrich evidence with aligned subtitle text
    _enrich_evidence_with_alignment(evidence, aligned_scenes)
    logger.info(f"M7 完成: {len(evidence)} 个证据包")

    # ── M8: Plot understanding ────────────────────────────────────
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

    # ── M9: Two-stage script generation ────────────────────────────
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

    # ── M10: Timeline budget (already applied inside ensure_script_shape) ──
    progress(80, "时间线预算校正...")
    script_items = ensure_script_shape(script_items)

    # ── M11: Preflight validation ──────────────────────────────────
    progress(85, "预检验证...")
    try:
        validate_script_items(script_items)
        logger.info("M11 预检通过")
    except PreflightError as e:
        logger.warning(f"M11 预检警告 (非致命): {e}")

    # ── Save final script JSON ─────────────────────────────────────
    progress(90, "保存脚本文件...")
    if not output_script_path:
        video_hash = utils.md5(video_path + str(os.path.getmtime(video_path)))
        output_script_path = os.path.join(
            utils.script_dir(), f"{video_hash}_subtitle_first.json"
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
        "success": True,
        "error": "",
    }


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _collect_keyframes(keyframe_dir: str) -> List[str]:
    """Collect keyframe image files from a directory."""
    if not keyframe_dir or not os.path.isdir(keyframe_dir):
        return []
    extensions = {".jpg", ".jpeg", ".png", ".bmp", ".webp"}
    files = sorted(
        f
        for f in [
            os.path.join(keyframe_dir, name)
            for name in os.listdir(keyframe_dir)
        ]
        if os.path.isfile(f) and os.path.splitext(f)[1].lower() in extensions
    )
    return files


def _enrich_evidence_with_alignment(
    evidence: List[Dict], aligned_scenes: List[Dict]
) -> None:
    """Copy aligned subtitle text into evidence packages (in-place)."""
    scene_map = {s["scene_id"]: s for s in aligned_scenes}
    for pkg in evidence:
        aligned = scene_map.get(pkg["scene_id"], {})
        # Prefer aligned text over the basic subtitle_text
        aligned_text = aligned.get("aligned_subtitle_text", "")
        if aligned_text:
            pkg["subtitle_text"] = aligned_text
        pkg["visual_only"] = aligned.get("visual_only", False)
        pkg["evidence_mode"] = "visual_only" if pkg["visual_only"] else "subtitle_first"


def _build_fallback_script(evidence: List[Dict]) -> List[Dict]:
    """Build a minimal script from evidence when LLM generation fails."""
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
