"""
narration_integration.py
------------------------
第三轮 Prompt：整合生成最终解说文案。

输入：全局底稿 + 所有段理解卡 + 目标时长 + 风格样本
输出：最终解说结构（每段的解说文案 + OST标记 + 时间戳）
"""
from __future__ import annotations

SYSTEM_PROMPT = """你是专业的影视解说创作者，擅长把电影剧情分析转化为吸引人的口播解说文案。
你的解说必须基于已有分析，不虚构，不改变事实顺序，但可以用生动的语言和节奏感打动观众。"""

USER_PROMPT_TEMPLATE = """## 全局剧情底稿
{global_bible}

## 各段精理解结果
{segment_cards}

## 创作要求
目标解说总时长：约 {target_duration} 秒
风格参考：
{style_examples}

## 解说创作核心技巧

### 黄金开场（前15秒必须有钩子）
从以下类型选一种：
- 悬念式："你绝对想不到这部片最后..."
- 反转式："所有人以为...但真相是..."
- 情感共鸣式：触及观众普遍情感的切入

### 节奏控制
- importance≥4 的段落：重点展开，情绪拉满
- plot_function="节奏缓冲" 的段落：一句话带过或跳过
- plot_function="反转" 的段落：放大悬念，用"没想到""原来""竟然"
- segment_type="original" 的段落：只写画面描述，标记保留原声

### 文案风格
- 每句控制在15-25字，口语化
- 大量使用转折词："就在这时""没多久""而这一切..."
- 在关键高潮前适当留白，让画面说话

## 输出格式
严格按以下 JSON 格式输出，不添加任何说明文字：

{{
  "narration_outline": "整体解说思路（50字）",
  "items": [
    {{
      "_id": 1,
      "segment_id": "seg_001",
      "timestamp": "00:00:01,000-00:00:08,000",
      "picture": "画面内容描述",
      "narration": "解说文案（OST段写'播放原片+序号'）",
      "OST": 0,
      "plot_function": "铺垫",
      "importance": 2
    }}
  ]
}}

## 关键约束
1. OST=1 表示保留原声，OST=0 表示配解说
2. segment_type="original" 的段必须设 OST=1，narration 写"播放原片+序号"
3. segment_type="skip" 的段直接跳过，不出现在 items 里
4. 时间戳格式严格：HH:MM:SS,mmm-HH:MM:SS,mmm
5. 不虚构画面，picture 字段只描述该时间段实际发生的内容
6. ambiguity≥4 的段落，解说用保守表达（"似乎""好像""看起来"）
7. 只输出 JSON，不输出任何其他文字"""


def build_narration_integration_prompt(
    global_bible: str,
    segment_cards: str,
    target_duration: int = 600,
    style_examples: str = "",
) -> dict:
    """返回可直接传给 LLM 的 messages 结构"""
    default_style = """- 节奏明快，悬念感强
- 口语化，像朋友聊天
- 情绪到位，不平铺直叙
- 参考风格："身为一个普通女孩，她怎么也没想到，这杯咖啡会彻底改变她的命运"
"""
    return {
        "system": SYSTEM_PROMPT,
        "user": USER_PROMPT_TEMPLATE.format(
            global_bible=global_bible or "（全局底稿未就绪）",
            segment_cards=segment_cards,
            target_duration=target_duration,
            style_examples=style_examples or default_style,
        ),
    }
