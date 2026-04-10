"""
segment_refiner.py
------------------
精分段 + plot_function 打分 + 差异化抽帧策略。

流程：
1. 对粗分段做初理解（轻量规则，不调用LLM）
2. 判断是否需要进一步拆细
3. 给每段打分（重要性/歧义度/视觉依赖度）
4. 根据分数决定抽帧策略
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import List, Dict, Optional, Tuple

from loguru import logger

from app.services.boundary_fuser import FusedSegment


# ── 打分配置 ──────────────────────────────────────────────────

# 重要性触发词
HIGH_IMPORTANCE_PATTERNS = [
    r"(?:原来|竟然|没想到|突然|其实|真相)",         # 反转
    r"(?:终于|决战|关键|爆发|高潮|崩溃)",            # 高潮
    r"(?:死|杀|爱你|结婚|离婚|真的是你)",            # 强情感
    r"(?:finally|truth|actually|killed|loved)",
]
LOW_IMPORTANCE_PATTERNS = [
    r"^(?:嗯|啊|哦|好的|知道了|谢谢|再见)$",        # 口水词
    r"(?:字幕组|翻译|校对|出品)",                    # 制作字幕
]

# 歧义度触发词（字幕可能不可信）
AMBIGUITY_PATTERNS = [
    r"(?:没有|不是|绝对不|从来没|怎么可能)",          # 否认
    r"(?:开玩笑|随便说说|骗你的|不当真)",             # 轻描淡写
    r"(?:我很好|没事|不怕|无所谓)",                   # 强颜欢笑
    r"(?:just kidding|never|absolutely not|i'm fine)",
]

# 视觉依赖度：纯动作无对白的特征
VISUAL_ONLY_FEATURES = [
    "visual_only",   # 来自 plot_chunker 标记
    "flashback",     # 回忆闪回
    "montage",       # 蒙太奇
]

# 精分触发：粗段内部发现这些信号时考虑拆细
INTERNAL_SHIFT_PATTERNS = [
    r"(?:另一边|与此同时|meanwhile|切换到|转到)",
    r"(?:第二天|隔天|一周后|months later)",
]


# ── 输出结构 ──────────────────────────────────────────────────

@dataclass
class RefinedSegment:
    """精分后的段落"""
    segment_id: str
    start: float
    end: float
    subtitle_text: str
    subtitle_ids: List[str]
    boundary_source: List[str]
    boundary_confidence: float
    boundary_reasons: List[str]

    # 精分打分
    importance: int = 3          # 1-5
    ambiguity: int = 1           # 1-5
    visual_dependency: int = 2   # 1-5
    plot_function: str = "铺垫"  # 铺垫|冲突升级|反转|情感爆发|信息揭露|悬念制造|节奏缓冲|结局收束
    segment_type: str = "narration"  # narration|original|skip

    # 抽帧策略（由打分决定）
    frame_count: int = 1         # 应抽帧数
    frame_strategy: str = "center"  # center|spread|first_mid_last

    # 继承字段
    has_dialogue: bool = True
    visual_only: bool = False
    special_type: str = ""
    scene_ids: List[str] = field(default_factory=list)
    aligned_subtitle_text: str = ""
    timestamp: str = ""
    frame_paths: List[str] = field(default_factory=list)
    keyframe_candidates: List[float] = field(default_factory=list)


# ── 主精分器 ──────────────────────────────────────────────────

class SegmentRefiner:

    def __init__(self):
        self._high_imp_re = re.compile(
            "|".join(HIGH_IMPORTANCE_PATTERNS), re.IGNORECASE
        )
        self._low_imp_re = re.compile(
            "|".join(LOW_IMPORTANCE_PATTERNS), re.IGNORECASE
        )
        self._ambiguity_re = re.compile(
            "|".join(AMBIGUITY_PATTERNS), re.IGNORECASE
        )
        self._internal_shift_re = re.compile(
            "|".join(INTERNAL_SHIFT_PATTERNS), re.IGNORECASE
        )

    def refine(
        self,
        fused_segments: List[FusedSegment],
        narrative_warnings: List[Dict] = None,
    ) -> List[RefinedSegment]:
        """
        主入口：对融合后的段落做精分和打分。

        narrative_warnings: 来自第一轮 LLM 的雷区标记列表，
        用于提升对应时间段内段落的歧义评分。
        对应会话共识：narrative_warnings 在精分段里的应用。
        """
        # 建立雷区时间段快速查找索引
        self._warning_ranges = _build_warning_index(narrative_warnings or [])

        total = len(fused_segments)
        refined = []

        for i, seg in enumerate(fused_segments):
            position_ratio = i / max(total - 1, 1)

            # 初理解：判断是否需要拆细
            sub_segs = self._try_split(seg)

            for sub in sub_segs:
                scored = self._score(sub, position_ratio, total)
                refined.append(scored)

        # 重编ID
        for i, r in enumerate(refined):
            r.segment_id = f"seg_{i+1:03d}"

        logger.info("精分段完成: {} 个段落", len(refined))
        return refined

    # ── 内部拆细判断 ──────────────────────────────────────────

    def _try_split(self, seg: FusedSegment) -> List[FusedSegment]:
        """
        判断粗段内部是否发生了叙事状态变化，如果是则拆细。
        当前实现：纯规则判断，不调用LLM（保持低成本）。
        """
        text = seg.subtitle_text or ""
        duration = seg.end - seg.start

        # 已经够短的不拆
        if duration < 30:
            return [seg]

        # 内部发现场景切换信号
        if self._internal_shift_re.search(text):
            # 简单从中间拆开
            mid = (seg.start + seg.end) / 2
            left = self._clone_seg(seg, seg.start, mid, text[:len(text)//2])
            right = self._clone_seg(seg, mid, seg.end, text[len(text)//2:])
            left.boundary_reasons.append("内部场景切换信号拆分（前半）")
            right.boundary_reasons.append("内部场景切换信号拆分（后半）")
            logger.debug("段落 {} 因内部切换信号拆细", seg.segment_id)
            return [left, right]

        return [seg]

    def _clone_seg(self, src: FusedSegment, start: float, end: float, text: str) -> FusedSegment:
        return FusedSegment(
            segment_id=src.segment_id,
            start=round(start, 3),
            end=round(end, 3),
            subtitle_text=text.strip(),
            subtitle_ids=src.subtitle_ids,
            boundary_source=list(src.boundary_source),
            boundary_confidence=src.boundary_confidence,
            boundary_reasons=list(src.boundary_reasons),
            has_dialogue=src.has_dialogue,
            visual_only=src.visual_only,
            special_type=src.special_type,
            scene_ids=src.scene_ids,
            aligned_subtitle_text=text.strip(),
            timestamp="",
            keyframe_candidates=src.keyframe_candidates,
        )

    # ── 打分 ──────────────────────────────────────────────────

    def _score(self, seg: FusedSegment, position_ratio: float, total: int) -> RefinedSegment:
        text = seg.subtitle_text or ""

        # importance（1-5）
        importance = self._score_importance(text, position_ratio)

        # ambiguity（1-5）
        ambiguity = self._score_ambiguity(text, start=seg.start, end=seg.end)

        # visual_dependency（1-5）
        visual_dep = self._score_visual_dependency(seg)

        # plot_function
        plot_func = self._classify_plot_function(text, position_ratio, importance)

        # segment_type
        seg_type = self._decide_segment_type(seg, importance, plot_func)

        # 抽帧策略
        frame_count, frame_strategy = self._decide_frame_strategy(
            importance, ambiguity, visual_dep, seg
        )

        return RefinedSegment(
            segment_id=seg.segment_id,
            start=seg.start,
            end=seg.end,
            subtitle_text=seg.subtitle_text,
            subtitle_ids=seg.subtitle_ids,
            boundary_source=seg.boundary_source,
            boundary_confidence=seg.boundary_confidence,
            boundary_reasons=seg.boundary_reasons,
            importance=importance,
            ambiguity=ambiguity,
            visual_dependency=visual_dep,
            plot_function=plot_func,
            segment_type=seg_type,
            frame_count=frame_count,
            frame_strategy=frame_strategy,
            has_dialogue=seg.has_dialogue,
            visual_only=seg.visual_only,
            special_type=seg.special_type,
            scene_ids=seg.scene_ids,
            aligned_subtitle_text=seg.aligned_subtitle_text or seg.subtitle_text,
            timestamp=seg.timestamp,
            keyframe_candidates=seg.keyframe_candidates,
        )

    def _score_importance(self, text: str, position_ratio: float) -> int:
        if self._low_imp_re.search(text):
            return 1
        score = 3
        if self._high_imp_re.search(text):
            score = 5
        # 片尾通常重要
        if position_ratio > 0.85:
            score = max(score, 4)
        # 片头第一段
        if position_ratio < 0.05:
            score = max(score, 3)
        return max(1, min(5, score))

    def _score_ambiguity(self, text: str, start: float = 0.0, end: float = 0.0) -> int:
        """
        歧义度评分。
        如果段落时间范围内有 narrative_warnings 雷区标记，
        歧义度至少设为 3（对应文档要求）。
        """
        base = 4 if self._ambiguity_re.search(text) else 1
        # 检查是否命中雷区
        if hasattr(self, '_warning_ranges') and self._warning_ranges:
            for w_start, w_end, w_type in self._warning_ranges:
                # 时间段有重叠
                if not (end < w_start or start > w_end):
                    warning_boost = {
                        "lie": 4, "irony": 4, "omission": 3,
                        "flashback": 2, "voiceover": 2,
                    }.get(w_type, 3)
                    base = max(base, warning_boost)
                    break
        return max(1, min(5, base))

    def _score_visual_dependency(self, seg: FusedSegment) -> int:
        if seg.visual_only or seg.special_type in ("flashback", "montage"):
            return 5
        if not seg.has_dialogue:
            return 4
        duration = seg.end - seg.start
        if duration > 60 and not (seg.subtitle_text or "").strip():
            return 4
        return 2

    def _classify_plot_function(self, text: str, pos: float, importance: int) -> str:
        if importance == 1:
            return "节奏缓冲"
        if pos > 0.9:
            return "结局收束"
        if pos < 0.1:
            return "铺垫"
        # 关键词匹配
        if re.search(r"原来|竟然|其实|没想到|真相", text):
            return "反转"
        if re.search(r"爆发|崩溃|哭|怒|终于说出|告白", text):
            return "情感爆发"
        if re.search(r"冲突|争吵|对峙|威胁|质问", text):
            return "冲突升级"
        if re.search(r"发现|得知|知道了|告诉你|真的是", text):
            return "信息揭露"
        if re.search(r"结果会|接下来|到底|难道|会不会", text):
            return "悬念制造"
        if importance >= 4:
            return "冲突升级"
        return "铺垫"

    def _decide_segment_type(self, seg: FusedSegment, importance: int, plot_func: str) -> str:
        # 跳过条件：纯片头片尾字幕、无内容的纯转场
        if not (seg.subtitle_text or "").strip() and seg.end - seg.start < 5:
            return "skip"
        if importance == 1 and seg.end - seg.start < 8:
            return "skip"
        # 保留原声条件：情感爆发/高潮时刻/高重要性
        if plot_func in ("情感爆发",) and importance >= 4:
            return "original"
        if plot_func == "反转" and importance == 5:
            return "original"
        return "narration"

    def _decide_frame_strategy(
        self, importance: int, ambiguity: int, visual_dep: int, seg: FusedSegment
    ) -> Tuple[int, str]:
        """
        根据分数决定抽帧数量和策略。
        差异化抽帧：高分段多抽，低分段少抽。
        """
        # 跳过段不抽帧
        if seg.visual_only and not seg.has_dialogue:
            return 3, "spread"
        if ambiguity >= 4:
            # 高歧义：首中尾都抽，补充视觉证据
            return 3, "first_mid_last"
        if importance >= 4:
            # 高重要性：多抽
            return 3 if visual_dep >= 3 else 2, "spread"
        if visual_dep >= 4:
            # 高视觉依赖：多抽
            return 3, "spread"
        if importance <= 2:
            # 低重要性：少抽
            return 1, "center"
        return 2, "spread"


# ── 便捷入口 ──────────────────────────────────────────────────

def refine_segments(
    fused_segments: List[FusedSegment],
    narrative_warnings: List[Dict] = None,
) -> List[RefinedSegment]:
    """pipeline 调用的入口"""
    return SegmentRefiner().refine(fused_segments, narrative_warnings=narrative_warnings)


def refined_to_dict(seg: RefinedSegment) -> Dict:
    """转换为下游模块期望的 dict 格式"""
    return {
        "segment_id": seg.segment_id,
        "scene_id": seg.segment_id,
        "start": seg.start,
        "end": seg.end,
        "duration": round(seg.end - seg.start, 3),
        "subtitle_text": seg.subtitle_text,
        "subtitle_ids": seg.subtitle_ids,
        "aligned_subtitle_ids": seg.subtitle_ids,
        "aligned_subtitle_text": seg.aligned_subtitle_text or seg.subtitle_text,
        "boundary_source": seg.boundary_source,
        "boundary_confidence": seg.boundary_confidence,
        "boundary_reasons": seg.boundary_reasons,
        "importance": seg.importance,
        "ambiguity": seg.ambiguity,
        "visual_dependency": seg.visual_dependency,
        "plot_function": seg.plot_function,
        "segment_type": seg.segment_type,
        "frame_count": seg.frame_count,
        "frame_strategy": seg.frame_strategy,
        "has_dialogue": seg.has_dialogue,
        "visual_only": seg.visual_only,
        "special_type": seg.special_type,
        "scene_ids": seg.scene_ids,
        "timestamp": seg.timestamp,
        "frame_paths": seg.frame_paths,
        "keyframe_candidates": seg.keyframe_candidates,
        # 兼容 evidence_fuser 字段
        "picture": seg.subtitle_text[:30] if seg.subtitle_text else "画面推进",
        "plot_role": _plot_function_to_role(seg.plot_function),
        "attraction_level": "高" if seg.importance >= 4 else ("中" if seg.importance >= 3 else "低"),
    }


def _plot_function_to_role(pf: str) -> str:
    mapping = {
        "反转": "twist",
        "情感爆发": "climax",
        "冲突升级": "conflict",
        "结局收束": "resolution",
        "铺垫": "setup",
        "信息揭露": "twist",
        "悬念制造": "conflict",
        "节奏缓冲": "development",
    }
    return mapping.get(pf, "development")


# ─────────────────────────────────────────────────────────────
# narrative_warnings 索引构建
# ─────────────────────────────────────────────────────────────

def _build_warning_index(narrative_warnings: List[Dict]) -> List[tuple]:
    """
    把 narrative_warnings 列表转换为 (start_sec, end_sec, type) 元组列表，
    用于 O(n) 时间段重叠检查。

    narrative_warnings 格式：
    [{"time": "00:10:15", "type": "lie", "reason": "..."}]
    """
    index = []
    for w in (narrative_warnings or []):
        t = _parse_warning_time(w.get("time", ""))
        if t >= 0:
            # 雷区窗口：前后各30秒
            index.append((max(0.0, t - 30), t + 30, w.get("type", "unknown")))
    return index


def _parse_warning_time(t: str) -> float:
    """把 HH:MM:SS 或秒数字符串解析为秒"""
    t = str(t or "").strip()
    if ":" in t:
        parts = t.split(":")
        try:
            if len(parts) == 3:
                return int(parts[0]) * 3600 + int(parts[1]) * 60 + float(parts[2])
            if len(parts) == 2:
                return int(parts[0]) * 60 + float(parts[1])
        except Exception:
            pass
    try:
        return float(t)
    except Exception:
        return -1.0
