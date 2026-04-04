#!/usr/bin/env python
# -*- coding: UTF-8 -*-

"""
narration_integration.py
------------------------
第三轮 Prompt：整合生成最终解说文案。
继承 ParameterizedPrompt，纳入 PromptManager 注册体系。

参数：global_bible / segment_cards / target_duration / style_examples
输出：最终解说结构 JSON（items 列表，含 OST 标记）
"""
from __future__ import annotations

from ..base import ParameterizedPrompt, PromptMetadata, ModelType, OutputFormat

SYSTEM_PROMPT = """你是专业的影视解说创作者，擅长把电影剧情分析转化为吸引人的口播解说文案。
你的解说必须基于已有分析，不虚构，不改变事实顺序，但可以用生动的语言和节奏感打动观众。"""

DEFAULT_STYLE = """- 节奏明快，悬念感强
- 口语化，像朋友聊天，避免书面语
- 情绪到位，不平铺直叙
- 参考风格："身为一个普通女孩，她怎么也没想到，这杯咖啡会彻底改变她的命运"
- 大量使用转折词："就在这时""没想到""而这一切..."
"""


class NarrationIntegrationPrompt(ParameterizedPrompt):
    """第三轮：整合所有 segment_card 生成最终解说文案"""

    def __init__(self):
        metadata = PromptMetadata(
            name="narration_integration",
            category="film_narration",
            version="v1.0",
            description="第三轮LLM调用：整合全局底稿和所有分段理解卡，生成最终口播解说文案，支持OST原声标记",
            model_type=ModelType.TEXT,
            output_format=OutputFormat.JSON,
            tags=["影视解说", "文案生成", "口播解说", "OST", "解说整合", "黄金开场"],
            parameters=["global_bible", "segment_cards", "target_duration", "style_examples"],
        )
        super().__init__(metadata, required_parameters=["segment_cards"])
        self._system_prompt = SYSTEM_PROMPT

    def get_template(self) -> str:
        return """## 全局剧情底稿
${global_bible}

## 各段精理解结果
${segment_cards}

## 创作要求
目标解说总时长：约 ${target_duration} 秒
风格参考：
${style_examples}

## 解说创作核心技巧

### 黄金开场（前15秒必须有钩子）
从以下类型选一种：
- 悬念式："你绝对想不到这部片最后..."
- 反转式："所有人以为...但真相是..."
- 情感共鸣式：触及观众普遍情感的切入
- 直接定性式："身为一个XXX，他知道自己..."

### 节奏控制
- importance≥4 的段落：重点展开，情绪拉满
- plot_function="节奏缓冲" 的段落：一句话带过或直接跳过
- plot_function="反转" 的段落：放大悬念，用"没想到""原来""竟然"
- plot_function="情感爆发" 的段落：优先考虑保留原声（OST=1）
- segment_type="original" 的段落：只写画面描述，标记保留原声

### 解说语言技巧
- 每句控制在15-25字，短句推进节奏，长句渲染氛围
- 使用"上帝视角"分析角色动机和内心
- 适时插入吐槽点评，增加趣味性
- 在关键高潮前留白，让画面说话

## 输出格式
严格按以下 JSON 格式输出，不添加任何说明文字：

{
  "narration_outline": "整体解说思路（50字以内）",
  "items": [
    {
      "_id": 1,
      "segment_id": "seg_001",
      "timestamp": "00:00:01,000-00:00:08,000",
      "picture": "画面内容描述（客观）",
      "narration": "解说文案（OST段写'播放原片+序号'）",
      "OST": 0,
      "plot_function": "铺垫",
      "importance": 2
    }
  ]
}

## 关键约束
1. OST=1 表示保留原声，OST=0 表示配解说
2. segment_type="original" 的段必须设 OST=1，narration 写"播放原片+序号"
3. segment_type="skip" 的段直接跳过，不出现在 items 里
4. 时间戳格式严格：HH:MM:SS,mmm-HH:MM:SS,mmm
5. 不虚构画面，picture 字段只描述该时间段实际发生的内容
6. ambiguity≥4 的段落，解说用保守表达（"似乎""好像""看起来"）
7. 解说文案每段80-150字，控制节奏密度
8. 只输出 JSON，不输出任何其他文字"""


# ── 便捷函数 ──────────────────────────────────────────────────

def build_narration_integration_prompt(
    global_bible: str,
    segment_cards: str,
    target_duration: int = 600,
    style_examples: str = "",
) -> dict:
    """返回可直接传给 LLM 的 messages 结构（兼容旧调用方式）"""
    prompt = NarrationIntegrationPrompt()
    return {
        "system": prompt.get_system_prompt(),
        "user": prompt.render({
            "global_bible": global_bible or "（全局底稿未就绪）",
            "segment_cards": segment_cards,
            "target_duration": str(target_duration),
            "style_examples": style_examples or DEFAULT_STYLE,
        }),
    }
