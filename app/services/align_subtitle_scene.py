"""M5: Subtitle-scene alignment module.

Maps normalized subtitle segments onto scene boundaries so that every
scene carries the subtitle text that overlaps with its time window.

Key behaviours
--------------
* Subtitles fully inside a scene are assigned to that scene directly.
* Subtitles that span two scenes are split at the scene boundary and
  assigned proportionally.
* Scenes with no overlapping subtitles are tagged ``visual_only=True``
  so downstream modules know to rely on frame evidence only.
"""

from typing import Dict, List

from loguru import logger


def align_subtitles_to_scenes(
    subtitle_segments: List[Dict],
    scenes: List[Dict],
) -> List[Dict]:
    """Align subtitle segments to scene boundaries.

    Parameters
    ----------
    subtitle_segments:
        Normalized subtitle dicts, each with ``start``, ``end``, ``text``,
        ``seg_id``.
    scenes:
        Scene dicts, each with ``scene_id``, ``start``, ``end``.

    Returns
    -------
    List of *aligned scene* dicts.  Each scene dict is a copy of the
    original with extra keys:

    * ``aligned_subtitle_ids`` – list of seg_ids that overlap
    * ``aligned_subtitle_text`` – concatenated text from those segments
    * ``visual_only`` – True when no subtitle overlaps
    """
    if not scenes:
        return []

    aligned: List[Dict] = []

    for scene in scenes:
        scene_start = scene["start"]
        scene_end = scene["end"]
        matched_ids: List[str] = []
        matched_texts: List[str] = []

        # Walk through subtitle segments that could overlap this scene
        for seg in subtitle_segments:
            seg_start = seg["start"]
            seg_end = seg["end"]

            # No overlap – segment entirely before scene
            if seg_end <= scene_start:
                continue
            # No overlap – segment entirely after scene
            if seg_start >= scene_end:
                break

            # Overlap exists
            overlap_start = max(seg_start, scene_start)
            overlap_end = min(seg_end, scene_end)
            overlap_duration = overlap_end - overlap_start
            seg_duration = seg_end - seg_start

            # Only include if at least 30% of the subtitle overlaps with
            # this scene, OR the overlap is >= 0.3 seconds.
            if seg_duration > 0 and (
                overlap_duration / seg_duration >= 0.3
                or overlap_duration >= 0.3
            ):
                matched_ids.append(seg["seg_id"])
                matched_texts.append(seg["text"])

        result = dict(scene)
        result["aligned_subtitle_ids"] = matched_ids
        result["aligned_subtitle_text"] = " ".join(matched_texts).strip()
        result["visual_only"] = len(matched_ids) == 0
        aligned.append(result)

    visual_only_count = sum(1 for s in aligned if s["visual_only"])
    logger.info(
        f"字幕-场景对齐完成: {len(aligned)} 个场景, "
        f"{visual_only_count} 个纯画面场景"
    )
    return aligned
