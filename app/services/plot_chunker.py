"""
plot_chunker.py
---------------
字幕语义粗分段器。

输入：normalize_segments() 输出的字幕段列表
输出：粗剧情段列表，每段带 boundary_source / boundary_confidence / boundary_reasons

设计原则：
- 字幕语义连续性主导（权重0.55）
- 不依赖视频，纯文本判断
- 边界必须可解释（来源+置信度+理由）
- 边界吸附交给 boundary_fuser.py 处理
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import List, Dict, Tuple

from loguru import logger


# ── 配置 ──────────────────────────────────────────────────────

# 语义连续性各维度权重（合计1.0）
CONTINUITY_WEIGHTS = {
    "topic_similarity": 0.35,
    "character_overlap": 0.20,
    "emotion_continuity": 0.15,
    "temporal_continuity": 0.15,
    "action_continuity": 0.15,
}

# 切段阈值
STRONG_CUT_THRESHOLD = 0.75   # 低于此值强制切段
CANDIDATE_CUT_THRESHOLD = 0.55  # 低于此值候选切段

# 段落长度约束
MIN_SEGMENT_DURATION = 8.0    # 最短段落秒数
MAX_SEGMENT_DURATION = 180.0  # 最长段落秒数（3分钟）
MIN_SUBTITLE_COUNT = 2        # 最少字幕条数

# 特殊结构关键词
FLASHBACK_KEYWORDS = ["那年", "那时候", "回忆", "当初", "过去", "years ago",
                       "flashback", "想起", "记得那", "多年前", "从前"]
VOICEOVER_KEYWORDS = ["旁白", "narrator", "voice over", "画外音", "os:", "vo:"]
TIME_JUMP_PATTERNS = [
    r"(?:一|两|三|\d+)(?:天|周|月|年)(?:后|前|过去)",
    r"(?:next|later|after)\s+(?:day|week|month|year)",
    r"(?:meanwhile|meanwhile,|同时|与此同时)",
]
TOPIC_SHIFT_MARKERS = [
    "话说", "且说", "再说", "另一边", "meanwhile", "however",
    "但是", "然而", "不过", "转眼", "突然", "此时", "就在这时",
]
NEW_EVENT_MARKERS = [
    "第一", "首先", "开始", "终于", "随后", "紧接着", "于是",
    "finally", "suddenly", "then", "after that",
]


# ── 输出结构 ──────────────────────────────────────────────────

@dataclass
class PlotChunk:
    """一个粗剧情段"""
    segment_id: str
    start: float
    end: float
    subtitle_ids: List[str]
    subtitle_text: str          # 合并后的字幕文本
    boundary_source: List[str]  # subtitle_semantic | time_jump | forced_split
    boundary_confidence: float
    boundary_reasons: List[str]
    # 内容特征
    has_dialogue: bool = True
    visual_only: bool = False
    special_type: str = ""      # flashback | voiceover | montage | ""
    # 后续填充
    aligned_subtitle_text: str = ""
    scene_id: str = ""
    timestamp: str = ""


# ── 主分段器 ──────────────────────────────────────────────────

class PlotChunker:

    def __init__(self):
        self._time_jump_re = re.compile(
            "|".join(TIME_JUMP_PATTERNS), re.IGNORECASE
        )

    def build_chunks(self, subtitle_segments: List[Dict]) -> List[PlotChunk]:
        """
        主入口：把字幕段列表切成粗剧情块。
        """
        if not subtitle_segments:
            return []

        # 预处理：过滤空字幕
        segs = [s for s in subtitle_segments if (s.get("text") or "").strip()]
        if not segs:
            return []

        total_dur = float(segs[-1].get("end", 0)) - float(segs[0].get("start", 0))
        logger.info("粗分段开始: {} 条字幕，总时长 {:.0f}s", len(segs), total_dur)

        # 找候选切点
        cut_points = self._find_cut_points(segs)
        # 按切点分组
        chunks = self._group_into_chunks(segs, cut_points)
        # 合并过短段落
        chunks = self._merge_short_chunks(chunks)
        # 强制拆分过长段落
        chunks = self._split_long_chunks(chunks, segs)
        # 标记特殊结构
        chunks = self._label_special_types(chunks)
        # 重新编号
        for i, c in enumerate(chunks):
            c.segment_id = f"seg_{i+1:03d}"
            c.aligned_subtitle_text = c.subtitle_text
            c.timestamp = f"{_fmt(c.start)}-{_fmt(c.end)}"

        logger.info("粗分段完成: {} 个剧情块", len(chunks))
        return chunks

    # ── 切点检测 ───────────────────────────────────────────────

    def _find_cut_points(self, segs: List[Dict]) -> List[Tuple[int, float, List[str]]]:
        """
        返回 (subtitle_index, confidence, reasons) 列表，表示在该字幕之前切段。
        """
        cut_points = []
        total = len(segs)

        for i in range(1, total):
            prev = segs[i - 1]
            cur = segs[i]
            reasons = []
            scores = {}

            prev_text = (prev.get("text") or "").strip()
            cur_text = (cur.get("text") or "").strip()

            # 1. 话题相似度
            topic_sim = self._topic_similarity(prev_text, cur_text)
            scores["topic_similarity"] = topic_sim
            if topic_sim < 0.15:
                reasons.append("话题明显转移")

            # 2. 人物重叠
            char_overlap = self._character_overlap(prev_text, cur_text)
            scores["character_overlap"] = char_overlap
            if char_overlap < 0.1:
                reasons.append("人物焦点变化")

            # 3. 情绪连续性
            emotion_cont = self._emotion_continuity(prev_text, cur_text)
            scores["emotion_continuity"] = emotion_cont

            # 4. 时间连续性（间隔）
            gap = float(cur.get("start", 0)) - float(prev.get("end", 0))
            temporal_cont = max(0.0, 1.0 - gap / 5.0)
            scores["temporal_continuity"] = temporal_cont
            if gap > 3.0:
                reasons.append(f"字幕间隔较大({gap:.1f}s)")

            # 5. 动作连续性
            action_cont = self._action_continuity(prev_text, cur_text)
            scores["action_continuity"] = action_cont

            # 加权综合得分
            continuity = sum(
                scores[k] * CONTINUITY_WEIGHTS[k]
                for k in CONTINUITY_WEIGHTS
            )

            # 额外信号：强制切段触发器
            if self._has_time_jump(cur_text):
                continuity = min(continuity, 0.3)
                reasons.append("时间跳跃标志词")
            if self._has_topic_shift_marker(cur_text):
                continuity = min(continuity, 0.45)
                reasons.append("话题转折词")

            if continuity < STRONG_CUT_THRESHOLD:
                cut_points.append((i, 1.0 - continuity, reasons))

        return cut_points

    # ── 分组 ───────────────────────────────────────────────────

    def _group_into_chunks(
        self, segs: List[Dict], cut_points: List[Tuple[int, float, List[str]]]
    ) -> List[PlotChunk]:
        cut_set = {cp[0]: cp for cp in cut_points}
        chunks = []
        bucket_start = 0

        for i in range(len(segs) + 1):
            if i == len(segs) or i in cut_set:
                if i > bucket_start:
                    bucket = segs[bucket_start:i]
                    cp = cut_set.get(i)
                    confidence = cp[1] if cp else 0.8
                    reasons = cp[2] if cp else ["段落开始"]
                    chunk = self._make_chunk(bucket, confidence, reasons)
                    chunks.append(chunk)
                bucket_start = i

        return chunks

    def _make_chunk(self, segs: List[Dict], confidence: float, reasons: List[str]) -> PlotChunk:
        text = " ".join((s.get("text") or "").strip() for s in segs if s.get("text"))
        ids = [str(s.get("seg_id", s.get("id", ""))) for s in segs]
        return PlotChunk(
            segment_id="",
            start=round(float(segs[0].get("start", 0)), 3),
            end=round(float(segs[-1].get("end", 0)), 3),
            subtitle_ids=ids,
            subtitle_text=text,
            boundary_source=["subtitle_semantic"],
            boundary_confidence=round(confidence, 3),
            boundary_reasons=reasons,
            has_dialogue=bool(text.strip()),
        )

    # ── 合并/拆分 ──────────────────────────────────────────────

    def _merge_short_chunks(self, chunks: List[PlotChunk]) -> List[PlotChunk]:
        """把连续的过短块合并"""
        if not chunks:
            return []
        merged = [chunks[0]]
        for c in chunks[1:]:
            prev = merged[-1]
            if prev.end - prev.start < MIN_SEGMENT_DURATION:
                # 合并
                prev.end = c.end
                prev.subtitle_ids.extend(c.subtitle_ids)
                prev.subtitle_text = (prev.subtitle_text + " " + c.subtitle_text).strip()
                prev.boundary_confidence = min(prev.boundary_confidence, c.boundary_confidence)
                prev.boundary_reasons.extend(c.boundary_reasons)
            else:
                merged.append(c)
        return merged

    def _split_long_chunks(self, chunks: List[PlotChunk], segs: List[Dict]) -> List[PlotChunk]:
        """把超过 MAX_SEGMENT_DURATION 的块强制从中间拆开"""
        seg_map = {str(s.get("seg_id", s.get("id", ""))): s for s in segs}
        result = []
        for chunk in chunks:
            if chunk.end - chunk.start <= MAX_SEGMENT_DURATION:
                result.append(chunk)
                continue
            # 按时间均分
            n = int((chunk.end - chunk.start) // MAX_SEGMENT_DURATION) + 1
            step = (chunk.end - chunk.start) / n
            for i in range(n):
                start = chunk.start + i * step
                end = min(chunk.start + (i + 1) * step, chunk.end)
                # 找属于这个时间段的字幕id
                ids = [sid for sid in chunk.subtitle_ids
                       if sid in seg_map and
                       start <= float(seg_map[sid].get("start", 0)) < end]
                text = " ".join(
                    (seg_map[sid].get("text") or "").strip()
                    for sid in ids if sid in seg_map
                )
                result.append(PlotChunk(
                    segment_id="",
                    start=round(start, 3),
                    end=round(end, 3),
                    subtitle_ids=ids,
                    subtitle_text=text,
                    boundary_source=["forced_split"],
                    boundary_confidence=0.6,
                    boundary_reasons=["段落超过最大时长强制拆分"],
                ))
        return result

    # ── 特殊结构标注 ───────────────────────────────────────────

    def _label_special_types(self, chunks: List[PlotChunk]) -> List[PlotChunk]:
        for chunk in chunks:
            text = chunk.subtitle_text.lower()
            if any(kw in text for kw in FLASHBACK_KEYWORDS):
                chunk.special_type = "flashback"
                chunk.boundary_reasons.append("检测到回忆/闪回关键词")
            elif any(kw in text for kw in VOICEOVER_KEYWORDS):
                chunk.special_type = "voiceover"
                chunk.boundary_reasons.append("检测到旁白标记")
        return chunks

    # ── 语义计算工具 ───────────────────────────────────────────

    def _topic_similarity(self, a: str, b: str) -> float:
        a_tokens = set(self._tokenize(a))
        b_tokens = set(self._tokenize(b))
        if not a_tokens or not b_tokens:
            return 0.5
        inter = len(a_tokens & b_tokens)
        union = len(a_tokens | b_tokens)
        return inter / max(union, 1)

    def _character_overlap(self, a: str, b: str) -> float:
        a_names = set(self._extract_names(a))
        b_names = set(self._extract_names(b))
        if not a_names and not b_names:
            return 0.7  # 无法判断，保守认为连续
        if not a_names or not b_names:
            return 0.3
        inter = len(a_names & b_names)
        union = len(a_names | b_names)
        return inter / max(union, 1)

    def _emotion_continuity(self, a: str, b: str) -> float:
        emotion_groups = [
            {"生气", "愤怒", "怒", "骂"},
            {"哭", "难过", "伤心", "泪"},
            {"笑", "开心", "高兴"},
            {"紧张", "危险", "快"},
            {"害怕", "恐惧"},
        ]
        ea = self._detect_emotion_group(a, emotion_groups)
        eb = self._detect_emotion_group(b, emotion_groups)
        if ea is None and eb is None:
            return 0.8
        return 1.0 if ea == eb else 0.2

    def _action_continuity(self, a: str, b: str) -> float:
        for marker in NEW_EVENT_MARKERS:
            if marker in b:
                return 0.3
        return 0.7

    def _has_time_jump(self, text: str) -> bool:
        return bool(self._time_jump_re.search(text))

    def _has_topic_shift_marker(self, text: str) -> bool:
        return any(m in text for m in TOPIC_SHIFT_MARKERS)

    def _tokenize(self, text: str) -> List[str]:
        text = re.sub(r"[\uff0c\u3002\uff01\uff1f\u3001\uff1b\uff1a\s]", " ", text or "")
        tokens = text.split()
        # 保留2字以上的词
        return [t for t in tokens if len(t) >= 2]

    def _extract_names(self, text: str) -> List[str]:
        # 简单提取：大写英文名 + 引号内中文名
        en = re.findall(r"\b[A-Z][a-z]{1,15}\b", text or "")
        zh = re.findall(r"[\u300a\u300b\u300c\u300d\u3010\u3011]([^\u300b\u300d\u3011]{1,8})", text or "")
        return en + zh

    def _detect_emotion_group(self, text: str, groups: List[set]):
        for i, group in enumerate(groups):
            if any(w in text for w in group):
                return i
        return None


# ── 便捷入口 ──────────────────────────────────────────────────

def build_plot_chunks_from_subtitles(subtitle_segments: List[Dict]) -> List[PlotChunk]:
    """pipeline 调用的入口函数，与 plot_first_pipeline.py 接口兼容"""
    chunker = PlotChunker()
    chunks = chunker.build_chunks(subtitle_segments)
    # 转换为 pipeline 期望的 dict 格式（兼容 evidence_fuser 等下游）
    return [_chunk_to_dict(c) for c in chunks]


def _chunk_to_dict(c: PlotChunk) -> Dict:
    return {
        "segment_id": c.segment_id,
        "scene_id": c.segment_id,
        "start": c.start,
        "end": c.end,
        "duration": round(c.end - c.start, 3),
        "subtitle_ids": c.subtitle_ids,
        "aligned_subtitle_ids": c.subtitle_ids,
        "subtitle_text": c.subtitle_text,
        "aligned_subtitle_text": c.subtitle_text,
        "boundary_source": c.boundary_source,
        "boundary_confidence": c.boundary_confidence,
        "boundary_reasons": c.boundary_reasons,
        "has_dialogue": c.has_dialogue,
        "visual_only": c.visual_only,
        "special_type": c.special_type,
        "timestamp": c.timestamp,
        "frame_paths": [],
        "keyframe_candidates": [],
    }


def _fmt(seconds: float) -> str:
    m, s = divmod(int(seconds), 60)
    h, m = divmod(m, 60)
    return f"{h:02d}:{m:02d}:{s:02d}"
