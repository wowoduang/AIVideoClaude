"""
pipeline_state.py
-----------------
流水线状态管理器，自动管理三轮LLM调用之间的上下文传递。
用户不感知，程序内部自动流转。
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from loguru import logger


@dataclass
class SegmentCard:
    """精分段理解卡，第二轮LLM输出"""
    segment_id: str
    start: float
    end: float
    label: str
    what_happened: str
    surface_dialogue_meaning: str
    real_narrative_state: str
    visual_correction: str
    plot_function: str          # 铺垫|冲突升级|反转|情感爆发|信息揭露|悬念制造|节奏缓冲|结局收束
    importance: int             # 1-5
    ambiguity: int              # 1-5
    visual_dependency: int      # 1-5
    segment_type: str           # narration|original|skip
    narration_candidate: str    # 解说文案初稿
    next_segment_handoff: str   # 传给下一段的50字摘要
    boundary_source: List[str] = field(default_factory=list)
    boundary_confidence: float = 0.8


@dataclass
class GlobalBible:
    """全局剧情底稿，第一轮LLM输出"""
    story_summary: str = ""
    main_characters: List[Dict] = field(default_factory=list)
    global_conflicts: List[str] = field(default_factory=list)
    timeline_outline: List[Dict] = field(default_factory=list)
    narrative_warnings: List[Dict] = field(default_factory=list)  # 雷区标记
    arc: str = "unknown"

    def get_warnings_in_range(self, start: float, end: float) -> List[Dict]:
        """取某时间段内的雷区标记"""
        result = []
        for w in self.narrative_warnings:
            try:
                t = _parse_time(w.get("time", "0"))
                if start - 30 <= t <= end + 30:
                    result.append(w)
            except Exception:
                pass
        return result

    def to_prompt_str(self) -> str:
        return json.dumps({
            "story_summary": self.story_summary,
            "main_characters": self.main_characters,
            "global_conflicts": self.global_conflicts,
            "arc": self.arc,
        }, ensure_ascii=False, indent=2)


class PipelineState:
    """
    整个处理流程的状态管理。
    程序自动读写，用户完全不感知。
    """

    def __init__(self, video_id: str, cache_dir: str = ""):
        self.video_id = video_id
        self.cache_dir = cache_dir or os.path.join("storage", "temp", "pipeline_state")
        os.makedirs(self.cache_dir, exist_ok=True)

        self.global_bible: Optional[GlobalBible] = None
        self.segment_cards: List[SegmentCard] = []
        self._last_handoff: str = ""  # 上一段的摘要，自动滚动

    # ── 第一轮：全局底稿 ──────────────────────────────────────

    def set_global_bible(self, bible: GlobalBible) -> None:
        self.global_bible = bible
        logger.info("全局剧情底稿已设置: warnings={}", len(bible.narrative_warnings))

    def get_global_bible(self) -> Optional[GlobalBible]:
        return self.global_bible

    # ── 第二轮：分段精理解 ────────────────────────────────────

    def build_segment_input(self, segment: Dict, frame_descriptions: List[str] = None) -> Dict:
        """
        自动拼装第二轮LLM的输入，程序调用，用户不感知。
        """
        start = float(segment.get("start", 0))
        end = float(segment.get("end", 0))
        warnings = []
        if self.global_bible:
            warnings = self.global_bible.get_warnings_in_range(start, end)

        return {
            "global_bible": self.global_bible.to_prompt_str() if self.global_bible else "{}",
            "prev_summary": self._last_handoff,
            "subtitles": segment.get("subtitle_text", "") or segment.get("aligned_subtitle_text", ""),
            "frame_descriptions": frame_descriptions or [],
            "narrative_warnings": warnings,
            "segment_id": segment.get("segment_id", ""),
            "start": start,
            "end": end,
            "label": segment.get("label", ""),
        }

    def record_segment_card(self, card: SegmentCard) -> None:
        """每段处理完自动更新状态，handoff自动滚动"""
        self.segment_cards.append(card)
        self._last_handoff = card.next_segment_handoff
        logger.debug("segment_card recorded: {} plot_function={} importance={}",
                     card.segment_id, card.plot_function, card.importance)

    def get_all_cards(self) -> List[SegmentCard]:
        return self.segment_cards

    # ── 第三轮：整合 ──────────────────────────────────────────

    def build_integration_input(self, target_duration: int, style_examples: str = "") -> Dict:
        cards_json = []
        for c in self.segment_cards:
            cards_json.append({
                "segment_id": c.segment_id,
                "start": c.start,
                "end": c.end,
                "label": c.label,
                "what_happened": c.what_happened,
                "real_narrative_state": c.real_narrative_state,
                "plot_function": c.plot_function,
                "importance": c.importance,
                "ambiguity": c.ambiguity,
                "segment_type": c.segment_type,
                "narration_candidate": c.narration_candidate,
            })
        return {
            "global_bible": self.global_bible.to_prompt_str() if self.global_bible else "{}",
            "segment_cards": json.dumps(cards_json, ensure_ascii=False),
            "target_duration": target_duration,
            "style_examples": style_examples,
        }

    # ── 持久化（可选，用于断点续跑）─────────────────────────

    def save(self) -> str:
        path = os.path.join(self.cache_dir, f"{self.video_id}_state.json")
        payload = {
            "video_id": self.video_id,
            "global_bible": self.global_bible.__dict__ if self.global_bible else None,
            "segment_cards": [c.__dict__ for c in self.segment_cards],
            "last_handoff": self._last_handoff,
        }
        with open(path, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)
        logger.info("pipeline state saved: {}", path)
        return path

    def load(self) -> bool:
        path = os.path.join(self.cache_dir, f"{self.video_id}_state.json")
        if not os.path.exists(path):
            return False
        try:
            with open(path, encoding="utf-8") as f:
                payload = json.load(f)
            if payload.get("global_bible"):
                self.global_bible = GlobalBible(**payload["global_bible"])
            self.segment_cards = [SegmentCard(**c) for c in payload.get("segment_cards", [])]
            self._last_handoff = payload.get("last_handoff", "")
            logger.info("pipeline state loaded: cards={}", len(self.segment_cards))
            return True
        except Exception as e:
            logger.warning("pipeline state load failed: {}", e)
            return False


def _parse_time(t: str) -> float:
    """把 HH:MM:SS 或纯秒数字符串解析为秒"""
    t = str(t).strip()
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
        return 0.0
