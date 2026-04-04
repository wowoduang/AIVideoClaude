"""
segment_analysis.py
-------------------
第二轮 Prompt：分段精理解。

每段独立调用，自动传入：
- 全局底稿（来自第一轮）
- 前段摘要（pipeline_state 自动滚动）
- 当前段字幕
- 代表帧描述（可选）
- 雷区提醒（来自 narrative_warnings）
"""
from __future__ import annotations

SYSTEM_PROMPT = """你是专业的电影叙事分析师。
你的任务是对电影的单个片段做深度理解，区分"角色说了什么"和"剧情真实发生了什么"。
输出必须结构化，不写散文，不脑补，不确定时写"无法确定"。"""

USER_PROMPT_TEMPLATE = """## 全局剧情底稿
{global_bible}

## 前段内容摘要
{prev_summary}

## 当前片段字幕
时间：{start_time} → {end_time}
{subtitles}

## 画面描述（代表帧）
{frame_descriptions}

## 雷区提醒（此段附近已知的字幕不可信位置）
{narrative_warnings}

## 任务
对当前片段做精理解，严格按以下 JSON 格式输出：

{{
  "what_happened": "客观描述这段发生了什么（基于字幕+画面证据）",
  "surface_dialogue_meaning": "字幕字面含义是什么",
  "real_narrative_state": "真实叙事状态（若与字幕字面一致则重复，若有偏差必须说明原因）",
  "visual_correction": "画面是否修正了字幕含义，如何修正（无则写null）",
  "plot_function": "铺垫|冲突升级|反转|情感爆发|信息揭露|悬念制造|节奏缓冲|结局收束",
  "importance": 1,
  "ambiguity": 1,
  "visual_dependency": 1,
  "segment_type": "narration|original|skip",
  "narration_candidate": "解说文案初稿（skip类型留空，original类型写画面描述即可）",
  "next_segment_handoff": "给下一段的上下文摘要，50字以内，包含关键人物状态和未解悬念"
}}

## 字段说明
- importance: 1-5，5=高潮/关键反转，1=过渡/无关紧要
- ambiguity: 1-5，5=字幕极可能不可信，1=字幕可信
- visual_dependency: 1-5，5=必须看画面才能理解，1=纯靠字幕即可
- segment_type:
  * narration = 需要解说配音
  * original = 保留原声（情感爆发/关键台词/高潮时刻）
  * skip = 跳过（片头片尾/纯转场/无叙事价值）

## 重要约束
1. 先理解，后表达，不直接追求漂亮解说句
2. real_narrative_state 和 surface_dialogue_meaning 必须分开写
3. 遇到雷区提醒覆盖的时间段，ambiguity 至少设为 3
4. 只输出 JSON，不输出任何其他文字"""


def build_segment_analysis_prompt(
    global_bible: str,
    prev_summary: str,
    subtitles: str,
    start: float,
    end: float,
    frame_descriptions: list = None,
    narrative_warnings: list = None,
) -> dict:
    """返回可直接传给 LLM 的 messages 结构"""
    frame_desc_str = "\n".join(frame_descriptions) if frame_descriptions else "（无代表帧）"
    warnings_str = "\n".join(
        f"[{w.get('time','')}] {w.get('type','')}: {w.get('reason','')}"
        for w in (narrative_warnings or [])
    ) or "（此段无雷区提醒）"

    return {
        "system": SYSTEM_PROMPT,
        "user": USER_PROMPT_TEMPLATE.format(
            global_bible=global_bible or "（全局底稿未就绪）",
            prev_summary=prev_summary or "（这是第一段）",
            start_time=_fmt(start),
            end_time=_fmt(end),
            subtitles=subtitles or "（无对白，纯画面段落）",
            frame_descriptions=frame_desc_str,
            narrative_warnings=warnings_str,
        ),
    }


def _fmt(seconds: float) -> str:
    m, s = divmod(int(seconds), 60)
    h, m = divmod(m, 60)
    return f"{h:02d}:{m:02d}:{s:02d}"
