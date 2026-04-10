"""
plot_understanding.py
---------------------
三轮 LLM 调用的核心实现。（覆盖原有纯规则实现）

第一轮：全局粗理解 → GlobalBible
第二轮：分段精理解 → SegmentCard（每段独立调用）
第三轮：整合生成解说文案 → script_items

兼容原有接口：classify_segment_role / add_local_understanding / build_global_summary
新增接口：build_global_understanding / run_segment_analysis / run_narration_integration
"""
from __future__ import annotations

import json
import re
from typing import Dict, List, Optional

from loguru import logger

from app.services.pipeline_state import GlobalBible, PipelineState, SegmentCard
from app.services.prompts.film_narration.global_understanding import build_global_understanding_prompt
from app.services.prompts.film_narration.segment_analysis import build_segment_analysis_prompt
from app.services.prompts.film_narration.narration_integration import build_narration_integration_prompt


# ── PromptManager 集成（可选加速路径）────────────────────────

def _get_prompt_via_manager(category: str, name: str, params: dict) -> Optional[dict]:
    """
    优先通过 PromptManager 获取已注册的 prompt 对象并渲染。
    失败时返回 None，由便捷函数兜底，保证向后兼容。
    """
    try:
        from app.services.prompts.manager import PromptManager
        prompt_obj = PromptManager.get_prompt_object(category, name)
        system = prompt_obj.get_system_prompt() or ""
        user = prompt_obj.render(params)
        return {"system": system, "user": user}
    except Exception as e:
        logger.debug("PromptManager 获取失败，使用便捷函数兜底: {}", e)
        return None


# ── LLM 调用封装 ──────────────────────────────────────────────

def _call_llm(system: str, user: str, api_key: str, base_url: str, model: str,
              temperature: float = 0.3) -> str:
    """
    统一 LLM 调用入口。
    优先使用 LiteLLM provider（已注册时），兜底使用直接 HTTP。
    """
    from app.services.llm_caller import call_llm_sync
    return call_llm_sync(
        system=system, user=user,
        api_key=api_key, base_url=base_url, model=model,
        temperature=temperature,
    )


def _parse_json_response(text: str) -> Optional[Dict]:
    if not text:
        return None
    clean = re.sub(r"```(?:json)?", "", text).strip().rstrip("`").strip()
    try:
        return json.loads(clean)
    except Exception:
        start = clean.find("{")
        end = clean.rfind("}")
        if start != -1 and end > start:
            try:
                return json.loads(clean[start:end + 1])
            except Exception:
                pass
    logger.warning("JSON 解析失败，原始内容: {}...", text[:200])
    return None


# ── 第一轮：全局粗理解 ────────────────────────────────────────

def build_global_understanding(
    subtitle_segments: List[Dict],
    api_key: str = "",
    base_url: str = "",
    model: str = "",
) -> GlobalBible:
    subtitle_content = _segments_to_text(subtitle_segments)
    if not subtitle_content:
        return GlobalBible()

    prompts = (
        _get_prompt_via_manager("film_narration", "global_understanding",
                                {"subtitle_content": subtitle_content})
        or build_global_understanding_prompt(subtitle_content)
    )
    logger.info("第一轮LLM：全局理解，字幕长度={}", len(subtitle_content))

    raw = _call_llm(
        system=prompts["system"], user=prompts["user"],
        api_key=api_key, base_url=base_url, model=model, temperature=0.2,
    )

    data = _parse_json_response(raw)
    if not data:
        logger.warning("第一轮输出解析失败，使用规则兜底")
        return _fallback_global_bible(subtitle_segments)

    bible = GlobalBible(
        story_summary=data.get("story_summary", ""),
        main_characters=data.get("main_characters", []),
        global_conflicts=data.get("global_conflicts", []),
        timeline_outline=data.get("timeline_outline", []),
        narrative_warnings=data.get("narrative_warnings", []),
        arc=data.get("arc", "drama"),
    )
    logger.info("全局理解完成: warnings={}", len(bible.narrative_warnings))
    return bible


# ── 第二轮：分段精理解 ────────────────────────────────────────

def run_segment_analysis(
    state: PipelineState,
    segment: Dict,
    frame_descriptions: List[str] = None,
    api_key: str = "",
    base_url: str = "",
    model: str = "",
) -> SegmentCard:
    inp = state.build_segment_input(segment, frame_descriptions or [])
    manager_params = {
        "global_bible": inp["global_bible"],
        "prev_summary": inp["prev_summary"],
        "subtitles": inp["subtitles"],
        "start_time": _fmt(inp["start"]),
        "end_time": _fmt(inp["end"]),
        "frame_descriptions": "\n".join(inp["frame_descriptions"]) if inp["frame_descriptions"] else "（无代表帧）",
        "narrative_warnings": "\n".join(
            f"[{w.get('time','')}] {w.get('type','')}: {w.get('reason','')}"
            for w in inp["narrative_warnings"]
        ) or "（此段无雷区提醒）",
    }
    prompts = (
        _get_prompt_via_manager("film_narration", "segment_analysis", manager_params)
        or build_segment_analysis_prompt(
            global_bible=inp["global_bible"],
            prev_summary=inp["prev_summary"],
            subtitles=inp["subtitles"],
            start=inp["start"],
            end=inp["end"],
            frame_descriptions=inp["frame_descriptions"],
            narrative_warnings=inp["narrative_warnings"],
        )
    )

    raw = _call_llm(
        system=prompts["system"], user=prompts["user"],
        api_key=api_key, base_url=base_url, model=model, temperature=0.3,
    )

    data = _parse_json_response(raw)
    if not data:
        card = _fallback_segment_card(segment)
    else:
        card = SegmentCard(
            segment_id=inp["segment_id"],
            start=inp["start"],
            end=inp["end"],
            label=inp.get("label", ""),
            what_happened=data.get("what_happened", ""),
            surface_dialogue_meaning=data.get("surface_dialogue_meaning", ""),
            real_narrative_state=data.get("real_narrative_state", ""),
            visual_correction=data.get("visual_correction") or "",
            plot_function=data.get("plot_function", "铺垫"),
            importance=int(data.get("importance", 3)),
            ambiguity=int(data.get("ambiguity", 1)),
            visual_dependency=int(data.get("visual_dependency", 2)),
            segment_type=data.get("segment_type", "narration"),
            narration_candidate=data.get("narration_candidate", ""),
            next_segment_handoff=data.get("next_segment_handoff", ""),
            boundary_source=segment.get("boundary_source", []),
            boundary_confidence=float(segment.get("boundary_confidence", 0.7)),
        )

    state.record_segment_card(card)
    return card


def run_all_segment_analysis(
    state: PipelineState,
    segments: List[Dict],
    frame_records: List[Dict] = None,
    api_key: str = "",
    base_url: str = "",
    model: str = "",
) -> List[SegmentCard]:
    frame_map = _build_frame_map(frame_records or [])
    cards = []
    total = len(segments)

    for i, seg in enumerate(segments):
        seg_id = seg.get("segment_id", "")
        if seg.get("segment_type") == "skip":
            # skip段：低成本规则处理，不调LLM（对应共识"低成本初理解"）
            card = _fallback_segment_card(seg)
            card.segment_type = "skip"
            card.plot_function = "节奏缓冲"
            card.importance = 1
            card.next_segment_handoff = ""
            state.record_segment_card(card)
            cards.append(card)
            continue

        # 无声纯画面段：规则处理，不调LLM
        if seg.get("visual_only") and not (seg.get("subtitle_text") or "").strip():
            card = _fallback_segment_card(seg)
            card.segment_type = "original"
            card.visual_dependency = 5
            card.importance = max(1, int(seg.get("importance", 2)))
            card.plot_function = "节奏缓冲"
            card.what_happened = f"画面推进（无对白，{seg.get('duration', 0):.0f}秒）"
            card.next_segment_handoff = "无声段，画面继续推进"
            state.record_segment_card(card)
            cards.append(card)
            continue

        frame_descs = frame_map.get(seg_id, [])
        logger.info("第二轮精理解 [{}/{}]: {}", i + 1, total, seg_id)
        card = run_segment_analysis(
            state=state, segment=seg, frame_descriptions=frame_descs,
            api_key=api_key, base_url=base_url, model=model,
        )
        cards.append(card)

    logger.info("第二轮精理解完成: {} 段", len(cards))
    return cards


# ── 第三轮：整合生成解说 ──────────────────────────────────────

def run_narration_integration(
    state: PipelineState,
    target_duration: int = 600,
    style_examples: str = "",
    api_key: str = "",
    base_url: str = "",
    model: str = "",
) -> List[Dict]:
    inp = state.build_integration_input(target_duration, style_examples)
    manager_params = {
        "global_bible": inp["global_bible"],
        "segment_cards": inp["segment_cards"],
        "target_duration": str(target_duration),
        "style_examples": style_examples or "",
    }
    prompts = (
        _get_prompt_via_manager("film_narration", "narration_integration", manager_params)
        or build_narration_integration_prompt(
            global_bible=inp["global_bible"],
            segment_cards=inp["segment_cards"],
            target_duration=target_duration,
            style_examples=style_examples,
        )
    )

    logger.info("第三轮LLM：整合生成解说文案")
    raw = _call_llm(
        system=prompts["system"], user=prompts["user"],
        api_key=api_key, base_url=base_url, model=model, temperature=0.7,
    )

    data = _parse_json_response(raw)
    if not data or "items" not in data:
        logger.warning("第三轮整合解析失败，使用候选文案兜底")
        return _fallback_script_items(state)

    script_items = []
    for item in data.get("items", []):
        script_items.append({
            "_id": item.get("_id", 0),
            "segment_id": item.get("segment_id", ""),
            "timestamp": item.get("timestamp", ""),
            "picture": item.get("picture", ""),
            "narration": item.get("narration", ""),
            "OST": int(item.get("OST", 0)),
            "plot_function": item.get("plot_function", ""),
            "importance": int(item.get("importance", 3)),
        })

    logger.info("第三轮整合完成: {} 个解说片段", len(script_items))
    return script_items


# ── 兼容原有接口 ───────────────────────────────────────────────

def classify_segment_role(text: str, position_ratio: float) -> str:
    text = (text or "").strip()
    role_keywords = {
        "twist": ["原来", "竟然", "没想到", "突然", "其实"],
        "climax": ["终于", "决战", "关键", "爆发", "当场"],
        "conflict": ["争吵", "质问", "威胁", "冲突", "对峙"],
        "resolution": ["和解", "离开", "结束", "平静", "释然"],
        "setup": ["开始", "第一次", "介绍", "来到", "得知"],
    }
    for role in ("twist", "climax", "conflict", "resolution", "setup"):
        if any(kw in text for kw in role_keywords[role]):
            return role
    if position_ratio < 0.15:
        return "setup"
    if position_ratio > 0.85:
        return "resolution"
    return "development"


def add_local_understanding(evidence_list: List[Dict]) -> List[Dict]:
    total = len(evidence_list or [])
    for idx, pkg in enumerate(evidence_list or []):
        position_ratio = idx / max(total - 1, 1)
        text = (pkg.get("subtitle_text") or pkg.get("main_text_evidence") or "").strip()
        role = classify_segment_role(text, position_ratio)
        if not pkg.get("plot_role"):
            pkg["plot_role"] = role
        if not pkg.get("attraction_level"):
            pkg["attraction_level"] = "高" if role in {"climax", "twist"} else (
                "中" if role in {"conflict", "development"} else "低"
            )
        if not pkg.get("local_understanding"):
            pkg["local_understanding"] = {
                "characters": [],
                "core_event": text[:20] if text else "画面推进",
                "key_dialogue": [],
                "conflict_or_twist": text[:24] if role in {"conflict", "twist"} else None,
                "emotion": pkg.get("emotion_hint") or "平静",
            }
    return evidence_list


def build_global_summary(
    evidence_list: List[Dict],
    api_key: str = "",
    base_url: str = "",
    model: str = "",
) -> Dict:
    items = evidence_list or []
    if not items:
        return {"main_storyline": "", "character_relations": [], "unresolved_tensions": [],
                "entity_map": {}, "arc": "unknown", "key_segments": []}

    key_segments = [pkg.get("segment_id") for pkg in items if pkg.get("attraction_level") == "高"]
    main_events = [(pkg.get("local_understanding") or {}).get("core_event", "") for pkg in items[:6]]
    storyline = "；".join(e for e in main_events if e)[:80]

    return {
        "main_storyline": storyline or "视频围绕人物关系与事件推进展开。",
        "character_relations": [],
        "unresolved_tensions": [],
        "entity_map": {},
        "arc": items[-1].get("plot_role", "development") if items else "unknown",
        "key_segments": [x for x in key_segments if x],
    }


# ── 兜底函数 ──────────────────────────────────────────────────

def _fallback_global_bible(subtitle_segments: List[Dict]) -> GlobalBible:
    texts = [(s.get("text") or "").strip() for s in subtitle_segments if s.get("text")]
    summary = "；".join(texts[:5])[:80] if texts else "暂无剧情摘要"
    return GlobalBible(story_summary=summary)


def _fallback_segment_card(segment: Dict) -> SegmentCard:
    text = (segment.get("subtitle_text") or segment.get("aligned_subtitle_text") or "").strip()
    return SegmentCard(
        segment_id=segment.get("segment_id", ""),
        start=float(segment.get("start", 0)),
        end=float(segment.get("end", 0)),
        label=segment.get("label", ""),
        what_happened=text[:60] if text else "画面推进",
        surface_dialogue_meaning=text[:60] if text else "",
        real_narrative_state=text[:60] if text else "画面推进",
        visual_correction="",
        plot_function=segment.get("plot_function", "铺垫"),
        importance=int(segment.get("importance", 3)),
        ambiguity=1, visual_dependency=2,
        segment_type=segment.get("segment_type", "narration"),
        narration_candidate=text[:80] if text else "",
        next_segment_handoff=text[:50] if text else "继续推进",
        boundary_source=segment.get("boundary_source", []),
        boundary_confidence=float(segment.get("boundary_confidence", 0.7)),
    )


def _fallback_script_items(state: PipelineState) -> List[Dict]:
    items = []
    for i, card in enumerate(state.get_all_cards(), start=1):
        if card.segment_type == "skip":
            continue
        items.append({
            "_id": i, "segment_id": card.segment_id,
            "timestamp": f"{_fmt(card.start)},000-{_fmt(card.end)},000",
            "picture": card.what_happened[:30] if card.what_happened else "画面推进",
            "narration": "播放原片" + str(i) if card.segment_type == "original"
                         else card.narration_candidate or card.what_happened[:60],
            "OST": 1 if card.segment_type == "original" else 0,
            "plot_function": card.plot_function, "importance": card.importance,
        })
    return items


# ── 工具函数 ──────────────────────────────────────────────────

def _segments_to_text(segments: List[Dict], max_chars: int = 60000) -> str:
    lines = []
    for s in segments:
        start = s.get("start", 0)
        text = (s.get("text") or "").strip()
        if text:
            lines.append(f"[{_fmt(start)}] {text}")
    content = "\n".join(lines)
    if len(content) > max_chars:
        content = content[:max_chars] + "\n...(字幕已截断)"
    return content


def _build_frame_map(frame_records: List[Dict]) -> Dict[str, List[str]]:
    result = {}
    for rec in frame_records:
        sid = rec.get("segment_id") or rec.get("scene_id", "")
        desc = rec.get("description") or rec.get("frame_path", "")
        if sid and desc:
            result.setdefault(sid, []).append(desc)
    return result


def _fmt(seconds: float) -> str:
    m, s = divmod(int(seconds), 60)
    h, m = divmod(m, 60)
    return f"{h:02d}:{m:02d}:{s:02d}"


# ─────────────────────────────────────────────────────────────
# 全局回修（第三轮后的一致性检查）
# 对应会话共识第10步：全局回修
# ─────────────────────────────────────────────────────────────

def run_global_revision(
    state: "PipelineState",
    api_key: str = "",
    base_url: str = "",
    model: str = "",
) -> list:
    """
    全局回修：对所有 segment_cards 做一致性检查，
    解决前后段理解矛盾、切段过碎、叙事不连贯等问题。

    对应文档第10步：全局回修
    返回修订后的 script_items，可替换第三轮输出。
    """
    cards = state.get_all_cards()
    if not cards:
        return []

    # 检测明显问题
    issues = _detect_consistency_issues(cards)
    if not issues:
        logger.info("全局回修：未发现一致性问题，跳过")
        return []

    logger.info("全局回修：发现 {} 个一致性问题", len(issues))

    # 构建回修 prompt
    bible_str = state.global_bible.to_prompt_str() if state.global_bible else "{}"
    cards_summary = _build_cards_summary(cards)
    issues_str = "\n".join(f"- {iss}" for iss in issues)

    system = "你是专业的影视解说审校编辑，负责检查解说脚本的叙事一致性并给出修订建议。"
    user = f"""## 全局剧情底稿
{bible_str}

## 当前各段理解摘要
{cards_summary}

## 发现的一致性问题
{issues_str}

## 任务
针对以上问题，输出修订建议。严格按 JSON 格式：

{{
  "revisions": [
    {{
      "segment_id": "seg_001",
      "issue": "问题描述",
      "revised_narration": "修订后的解说文案",
      "revised_segment_type": "narration|original|skip"
    }}
  ],
  "merge_suggestions": [
    {{"merge_ids": ["seg_003", "seg_004"], "reason": "这两段叙事状态相同，建议合并"}}
  ]
}}

只输出 JSON，不输出其他文字。"""

    raw = _call_llm(system=system, user=user,
                    api_key=api_key, base_url=base_url, model=model,
                    temperature=0.2)
    data = _parse_json_response(raw)
    if not data:
        logger.warning("全局回修解析失败，跳过")
        return []

    logger.info("全局回修完成：{} 条修订建议", len(data.get("revisions", [])))
    return data.get("revisions", [])


def _detect_consistency_issues(cards: list) -> list:
    """规则检测：发现明显的一致性问题"""
    issues = []
    total = len(cards)

    for i, card in enumerate(cards):
        # 问题1：高歧义段未标 original 或 skip
        if card.ambiguity >= 4 and card.segment_type == "narration":
            issues.append(
                f"{card.segment_id}: 歧义度={card.ambiguity}，但类型为 narration，"
                f"建议改为 original 或加保守表达"
            )

        # 问题2：连续多个"节奏缓冲"段（超过3个连续）
        if i >= 2:
            prev2 = cards[i-2].plot_function
            prev1 = cards[i-1].plot_function
            curr = card.plot_function
            if prev2 == prev1 == curr == "节奏缓冲":
                issues.append(
                    f"{card.segment_id}: 连续3段都是节奏缓冲，"
                    f"建议合并或跳过中间段"
                )

        # 问题3：结局前没有高潮
        if i == total - 1 and card.plot_function == "结局收束":
            climax_count = sum(1 for c in cards if c.plot_function in ("情感爆发", "反转", "冲突升级"))
            if climax_count == 0:
                issues.append("全片没有情感爆发/反转段落，解说可能过于平淡")

        # 问题4：重要段落解说文案太短
        if card.importance >= 4 and card.segment_type == "narration":
            if len(card.narration_candidate) < 20:
                issues.append(
                    f"{card.segment_id}: importance={card.importance} 但解说文案仅 "
                    f"{len(card.narration_candidate)} 字，建议扩写"
                )

    return issues[:10]  # 最多返回10个问题


def _build_cards_summary(cards: list) -> str:
    lines = []
    for c in cards:
        lines.append(
            f"[{c.segment_id}] {c.plot_function} | importance={c.importance} | "
            f"ambiguity={c.ambiguity} | type={c.segment_type} | "
            f"{(c.what_happened or '')[:40]}"
        )
    return "\n".join(lines)


def apply_revisions_to_script(
    script_items: list,
    revisions: list,
) -> list:
    """
    把全局回修的建议应用到最终 script_items。
    """
    if not revisions:
        return script_items

    revision_map = {r["segment_id"]: r for r in revisions if r.get("segment_id")}
    updated = []
    for item in script_items:
        seg_id = item.get("segment_id", "")
        if seg_id in revision_map:
            rev = revision_map[seg_id]
            new_item = dict(item)
            if rev.get("revised_narration"):
                new_item["narration"] = rev["revised_narration"]
                logger.debug("回修 {}: {}", seg_id, rev.get("issue", ""))
            if rev.get("revised_segment_type"):
                new_item["OST"] = 1 if rev["revised_segment_type"] == "original" else (
                    2 if rev["revised_segment_type"] == "skip" else 0
                )
            updated.append(new_item)
        else:
            updated.append(item)

    logger.info("全局回修应用完成：修改了 {} 个片段", len(revision_map))
    return updated
