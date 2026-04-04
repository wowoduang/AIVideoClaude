"""
global_understanding.py
-----------------------
第一轮 Prompt：全局粗理解。

任务：只做理解，不写解说词。
输出：global_story_bible（含 narrative_warnings 雷区标记）
"""
from __future__ import annotations

SYSTEM_PROMPT = """你是专业的电影剧情分析师，擅长从字幕中提炼叙事结构、人物关系和剧情真相。
你的分析必须基于证据，不脑补，不猜测，不确定时明确说"无法确定"。"""

USER_PROMPT_TEMPLATE = """## 任务
分析以下电影字幕，输出全局剧情底稿。
只做理解和分析，不写任何解说文案。

## 字幕内容
{subtitle_content}

## 输出要求
严格按以下 JSON 格式输出，不添加任何说明文字：

{{
  "story_summary": "一句话概括整部片的核心剧情",
  "main_characters": [
    {{"name": "角色名", "role": "主角/配角/反派", "motivation": "核心动机"}}
  ],
  "global_conflicts": ["核心冲突1", "核心冲突2"],
  "timeline_outline": [
    {{"time": "00:00:00", "event": "关键事件描述"}}
  ],
  "narrative_warnings": [
    {{
      "time": "HH:MM:SS",
      "type": "lie|flashback|voiceover|irony|omission",
      "reason": "为什么这里字幕字面含义不可信",
      "real_state": "推测的真实叙事状态（不确定时写无法确定）"
    }}
  ],
  "arc": "tragedy|comedy|thriller|romance|action|drama"
}}

## 重要约束
1. narrative_warnings 必须标注所有你认为字幕字面含义与真实叙事状态可能不符的位置
   - 角色说谎、试探、反讽、隐瞒时必须标注
   - 回忆/闪回导致时间层变化时必须标注  
   - 旁白不代表当下剧情状态时必须标注
2. 不确定时输出"无法确定"，禁止脑补
3. time 字段格式严格为 HH:MM:SS
4. 只输出 JSON，不输出任何其他文字"""


def build_global_understanding_prompt(subtitle_content: str) -> dict:
    """返回可直接传给 LLM 的 messages 列表"""
    return {
        "system": SYSTEM_PROMPT,
        "user": USER_PROMPT_TEMPLATE.format(subtitle_content=subtitle_content),
    }
