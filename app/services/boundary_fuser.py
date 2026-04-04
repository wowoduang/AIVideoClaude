"""
boundary_fuser.py
-----------------
字幕语义边界 + 视频场景边界融合决策。

核心逻辑：
1. LLM/字幕给出大概时间点（有误差）
2. 在吸附窗口内找最近的视频真实切割点
3. 融合打分，决定最终边界位置
4. 输出带置信度的最终剧情段列表
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import List, Dict, Optional, Tuple

from loguru import logger

from app.services.scene_detector import DetectedScene


# ── 融合配置 ──────────────────────────────────────────────────

@dataclass
class FuserConfig:
    # 边界融合权重
    weight_subtitle:  float = 0.55
    weight_video:     float = 0.25
    weight_character: float = 0.10
    weight_silence:   float = 0.05
    weight_scene_ctx: float = 0.05

    # 决策阈值
    strong_boundary:    float = 0.75
    candidate_boundary: float = 0.55

    # 时间戳吸附窗口（秒）
    snap_radius: float = 8.0

    # 场景与段落重叠比例阈值
    overlap_threshold: float = 0.3


# ── 输出结构 ──────────────────────────────────────────────────

@dataclass
class FusedSegment:
    """融合后的最终剧情段"""
    segment_id: str
    start: float                    # 吸附到视频边界后的精确时间
    end: float
    duration: float
    subtitle_text: str
    subtitle_ids: List[str]
    boundary_source: List[str]      # subtitle_semantic | video_candidate | subtitle+video
    boundary_confidence: float
    boundary_reasons: List[str]
    # 内容属性
    has_dialogue: bool = True
    visual_only: bool = False
    special_type: str = ""
    # 场景列表（归组后）
    scene_ids: List[str] = field(default_factory=list)
    aligned_subtitle_text: str = ""
    timestamp: str = ""
    frame_paths: List[str] = field(default_factory=list)
    keyframe_candidates: List[float] = field(default_factory=list)


# ── 主融合器 ──────────────────────────────────────────────────

class BoundaryFuser:

    def __init__(self, config: Optional[FuserConfig] = None):
        self.config = config or FuserConfig()

    def fuse(
        self,
        plot_chunks: List[Dict],
        scenes: List[DetectedScene],
    ) -> List[FusedSegment]:
        """
        主入口：把粗分段 + 场景列表融合成最终剧情段。

        plot_chunks: plot_chunker.py 输出的粗剧情块
        scenes:      scene_detector.py 输出的场景列表
        """
        if not plot_chunks:
            return []

        if not scenes:
            # 没有场景检测结果，直接用字幕分段
            logger.warning("无场景检测结果，使用纯字幕分段")
            return self._chunks_to_fused(plot_chunks)

        # 建立场景边界索引
        boundaries = self._build_boundary_index(scenes)

        fused = []
        for i, chunk in enumerate(plot_chunks):
            hint_start = float(chunk.get("start", 0))
            hint_end = float(chunk.get("end", 0))

            # 时间戳吸附
            snapped_start = self._snap(hint_start, boundaries, prefer="before")
            snapped_end = self._snap(hint_end, boundaries, prefer="after")

            # 归组场景
            scene_group = self._find_scenes_in_range(snapped_start, snapped_end, scenes)

            # 计算融合置信度
            sub_confidence = float(chunk.get("boundary_confidence", 0.7))
            video_boost = self._video_boost(hint_start, boundaries)
            final_confidence = (
                sub_confidence * self.config.weight_subtitle +
                video_boost * self.config.weight_video +
                0.5 * (self.config.weight_character + self.config.weight_silence + self.config.weight_scene_ctx)
            )

            # 决定边界来源标签
            source = list(chunk.get("boundary_source", ["subtitle_semantic"]))
            if video_boost > 0.6:
                if "subtitle_semantic" in source:
                    source = ["subtitle+video"]
                else:
                    source.append("video_candidate")

            reasons = list(chunk.get("boundary_reasons", []))
            if video_boost > 0.6:
                reasons.append("视频边界印证")
            elif video_boost < 0.2:
                reasons.append("视频边界未印证（字幕主导）")

            actual_start = scene_group[0].start if scene_group else snapped_start
            actual_end = scene_group[-1].end if scene_group else snapped_end

            seg = FusedSegment(
                segment_id=chunk.get("segment_id", f"seg_{i+1:03d}"),
                start=round(actual_start, 3),
                end=round(actual_end, 3),
                duration=round(actual_end - actual_start, 3),
                subtitle_text=chunk.get("subtitle_text", ""),
                subtitle_ids=chunk.get("subtitle_ids", []),
                boundary_source=source,
                boundary_confidence=round(min(final_confidence, 1.0), 3),
                boundary_reasons=reasons,
                has_dialogue=chunk.get("has_dialogue", True),
                visual_only=chunk.get("visual_only", False),
                special_type=chunk.get("special_type", ""),
                scene_ids=[s.scene_id for s in scene_group],
                aligned_subtitle_text=chunk.get("aligned_subtitle_text", chunk.get("subtitle_text", "")),
            )
            seg.timestamp = f"{_fmt(seg.start)}-{_fmt(seg.end)}"
            fused.append(seg)

        # 解决相邻段的场景归属冲突
        fused = self._resolve_scene_conflicts(fused, scenes)

        logger.info("边界融合完成: {} 个最终剧情段", len(fused))
        return fused

    # ── 时间戳吸附 ────────────────────────────────────────────

    def _build_boundary_index(self, scenes: List[DetectedScene]) -> List[float]:
        """收集所有真实场景边界时间点"""
        boundaries = set()
        for s in scenes:
            boundaries.add(s.start)
            boundaries.add(s.end)
        return sorted(boundaries)

    def _snap(self, t: float, boundaries: List[float], prefer: str = "nearest") -> float:
        """
        把时间点吸附到最近的真实场景边界。
        prefer: before（不超过t）| after（不早于t）| nearest
        """
        if not boundaries:
            return t

        radius = self.config.snap_radius
        candidates = [(abs(b - t), b) for b in boundaries if abs(b - t) <= radius]

        if not candidates:
            # 超出吸附半径，用最近点兜底
            return min(boundaries, key=lambda b: abs(b - t))

        candidates.sort()
        if prefer == "nearest":
            return candidates[0][1]
        elif prefer == "before":
            before = [(d, b) for d, b in candidates if b <= t]
            return before[0][1] if before else candidates[0][1]
        else:  # after
            after = [(d, b) for d, b in candidates if b >= t]
            return after[0][1] if after else candidates[0][1]

    def _video_boost(self, t: float, boundaries: List[float]) -> float:
        """计算视频边界对该时间点的支持强度（0-1）"""
        if not boundaries:
            return 0.0
        dists = [abs(b - t) for b in boundaries]
        min_dist = min(dists)
        if min_dist <= 1.0:
            return 1.0
        if min_dist <= 3.0:
            return 0.8
        if min_dist <= self.config.snap_radius:
            return 0.5
        return 0.1

    # ── 场景归组 ──────────────────────────────────────────────

    def _find_scenes_in_range(
        self, start: float, end: float, scenes: List[DetectedScene]
    ) -> List[DetectedScene]:
        """找时间范围内重叠超过阈值的场景"""
        result = []
        for scene in scenes:
            overlap = max(0, min(scene.end, end) - max(scene.start, start))
            ratio = overlap / max(scene.duration, 0.001)
            if ratio >= self.config.overlap_threshold:
                result.append(scene)
        return result

    # ── 场景归属冲突解决 ──────────────────────────────────────

    def _resolve_scene_conflicts(
        self, fused: List[FusedSegment], scenes: List[DetectedScene]
    ) -> List[FusedSegment]:
        """
        相邻段可能争抢同一个场景。
        规则：置信度高的段落优先；相同置信度则时间先的优先。
        """
        scene_owner: Dict[str, int] = {}  # scene_id → fused index

        for fi, seg in enumerate(fused):
            for sid in seg.scene_ids:
                if sid in scene_owner:
                    existing_fi = scene_owner[sid]
                    if fused[fi].boundary_confidence > fused[existing_fi].boundary_confidence:
                        scene_owner[sid] = fi
                else:
                    scene_owner[sid] = fi

        # 重建每段的 scene_ids
        for fi, seg in enumerate(fused):
            seg.scene_ids = [sid for sid in seg.scene_ids if scene_owner.get(sid) == fi]

        # 更新实际时间范围
        scene_map = {s.scene_id: s for s in scenes}
        for seg in fused:
            owned = [scene_map[sid] for sid in seg.scene_ids if sid in scene_map]
            if owned:
                seg.start = round(owned[0].start, 3)
                seg.end = round(owned[-1].end, 3)
                seg.duration = round(seg.end - seg.start, 3)
                seg.timestamp = f"{_fmt(seg.start)}-{_fmt(seg.end)}"
                seg.keyframe_candidates = [
                    round((s.start + s.end) / 2, 3) for s in owned
                ]

        return fused

    def _chunks_to_fused(self, chunks: List[Dict]) -> List[FusedSegment]:
        """无场景数据时直接把粗分段转换为 FusedSegment"""
        result = []
        for i, c in enumerate(chunks):
            start = float(c.get("start", 0))
            end = float(c.get("end", 0))
            seg = FusedSegment(
                segment_id=c.get("segment_id", f"seg_{i+1:03d}"),
                start=round(start, 3),
                end=round(end, 3),
                duration=round(end - start, 3),
                subtitle_text=c.get("subtitle_text", ""),
                subtitle_ids=c.get("subtitle_ids", []),
                boundary_source=c.get("boundary_source", ["subtitle_semantic"]),
                boundary_confidence=float(c.get("boundary_confidence", 0.7)),
                boundary_reasons=c.get("boundary_reasons", []),
                has_dialogue=c.get("has_dialogue", True),
                visual_only=c.get("visual_only", False),
                special_type=c.get("special_type", ""),
                aligned_subtitle_text=c.get("aligned_subtitle_text", ""),
            )
            seg.timestamp = f"{_fmt(seg.start)}-{_fmt(seg.end)}"
            result.append(seg)
        return result


# ── 便捷入口 ──────────────────────────────────────────────────

def fuse_boundaries(
    plot_chunks: List[Dict],
    scenes: List[DetectedScene],
    config: Optional[FuserConfig] = None,
) -> List[FusedSegment]:
    """pipeline 调用的入口函数"""
    return BoundaryFuser(config).fuse(plot_chunks, scenes)


def fused_to_dict(seg: FusedSegment) -> Dict:
    """转换为下游模块（evidence_fuser等）期望的 dict 格式"""
    return {
        "segment_id": seg.segment_id,
        "scene_id": seg.segment_id,
        "start": seg.start,
        "end": seg.end,
        "duration": seg.duration,
        "subtitle_text": seg.subtitle_text,
        "subtitle_ids": seg.subtitle_ids,
        "aligned_subtitle_ids": seg.subtitle_ids,
        "aligned_subtitle_text": seg.aligned_subtitle_text or seg.subtitle_text,
        "boundary_source": seg.boundary_source,
        "boundary_confidence": seg.boundary_confidence,
        "boundary_reasons": seg.boundary_reasons,
        "has_dialogue": seg.has_dialogue,
        "visual_only": seg.visual_only,
        "special_type": seg.special_type,
        "scene_ids": seg.scene_ids,
        "timestamp": seg.timestamp,
        "frame_paths": seg.frame_paths,
        "keyframe_candidates": seg.keyframe_candidates,
    }


def _fmt(seconds: float) -> str:
    m, s = divmod(int(seconds), 60)
    h, m = divmod(m, 60)
    return f"{h:02d}:{m:02d}:{s:02d}"
