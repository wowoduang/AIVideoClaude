#!/usr/bin/env python
# -*- coding: UTF-8 -*-

import json
import os
import traceback
from typing import Dict, List

from openai import OpenAI
from loguru import logger

import app.services.llm  # noqa: F401
from app.services.llm.migration_adapter import generate_narration as generate_narration_new
from app.services.prompts import PromptManager
from app.services.script_fallback import ensure_script_shape

SYSTEM_ROLE = "你是一名专业的影视解说编辑，要求先忠于事实，再保证表达有吸引力。"


def parse_frame_analysis_to_markdown(json_file_path):
    if not os.path.exists(json_file_path):
        return f"错误: 文件 {json_file_path} 不存在"
    try:
        with open(json_file_path, "r", encoding="utf-8") as file:
            data = json.load(file)
        markdown = ""
        summaries = data.get("overall_activity_summaries", [])
        frame_observations = data.get("frame_observations", [])
        batch_frames = {}
        for frame in frame_observations:
            batch_index = frame.get("batch_index")
            batch_frames.setdefault(batch_index, []).append(frame)
        for i, summary in enumerate(summaries, 1):
            batch_index = summary.get("batch_index")
            time_range = summary.get("time_range", "")
            batch_summary = summary.get("summary", "")
            markdown += f"## 片段 {i}\n"
            markdown += f"- 时间范围：{time_range}\n"
            markdown += f"- 片段描述：{batch_summary}\n"
            markdown += "- 详细描述：\n"
            for frame in batch_frames.get(batch_index, []):
                markdown += f"  - {frame.get('timestamp', '')}: {frame.get('observation', '')}\n"
            markdown += "\n"
        return markdown
    except Exception:
        return f"处理JSON文件时出错: {traceback.format_exc()}"


def scene_evidence_to_markdown(scene_evidence: List[Dict]) -> str:
    lines = []
    for idx, scene in enumerate(scene_evidence or [], start=1):
        char_budget = scene.get("char_budget")
        lines.append(f"## Scene {idx}")
        lines.append(f"- scene_id: {scene.get('scene_id')}")
        lines.append(f"- timestamp: {scene.get('timestamp')}")
        if char_budget:
            lines.append(f"- 本段字数上限：约 {char_budget} 字")
        lines.append(f"- subtitle_text: {scene.get('subtitle_text', '')}")
        lines.append(f"- visual_summary: {scene.get('visual_summary', '')}")
        lines.append(f"- evidence_mode: {scene.get('evidence_mode', 'subtitle_first')}")
        frames = scene.get("frame_paths", [])
        if frames:
            lines.append(f"- frame_count: {len(frames)}")
        lines.append("")
    return "\n".join(lines)


FACT_PROMPT_TEMPLATE = """
请阅读下面的视频片段证据包，先做事实抽取，不要直接写花哨文案。

要求：
1. 每个 scene 输出一条 facts 记录。
2. 仅基于字幕和画面证据，不要脑补不存在的细节。
3. 如果有字幕，优先以字幕为准；画面只做补充。
4. 每段 fact 描述必须控制在对应的字数上限内（如有标注），宁可精炼也不要超出。
5. 输出 JSON，格式如下：
{{
  "items": [
    {{
      "scene_id": "scene_001",
      "timestamp": "00:00:00,000-00:00:03,000",
      "char_budget": 20,
      "picture": "用一句话概括这一段画面与动作",
      "fact": "用一句话概括这一段真实发生了什么"
    }}
  ]
}}

证据包：
__SCENE_MARKDOWN__
"""

POLISH_PROMPT_TEMPLATE = """
你将收到一组事实版视频解说片段，请把它们改写成适合短视频解说的文案。

要求：
1. 保留原时间戳，不新增事实。
2. narration 要简洁、准确、有一点吸引力，但不要浮夸。
3. 每段 narration 必须严格控制在对应的字数上限（char_budget）内，宁可精炼也不要超出。
4. picture 保留为画面概括。
5. 输出 JSON，格式如下：
{{
  "items": [
    {{
      "_id": 1,
      "timestamp": "00:00:00,000-00:00:03,000",
      "char_budget": 20,
      "picture": "...",
      "narration": "..."
    }}
  ]
}}

事实版数据：
__FACT_JSON__
"""


# ============ 短剧风格的 Prompt 模板 ============
SHORT_DRAMA_FACT_PROMPT_TEMPLATE = """
请阅读下面的视频片段证据包（基于字幕/对白），找出最吸引观众的爆点和转折。

要求：
1. 每个 scene 输出一条记录，重点关注：冲突、反转、笑点、高潮。
2. 仅基于字幕证据，不要脑补不存在的细节。
3. 标注每段的吸引力等级（高/中/低），高吸引力的段落要重点挖掘戏剧张力。
4. 每段描述必须控制在对应的字数上限内，宁可精炼也不要超出。
5. 输出 JSON，格式如下：
{{
  "items": [
    {{
      "scene_id": "scene_001",
      "timestamp": "00:00:00,000-00:00:03,000",
      "char_budget": 20,
      "attraction_level": "高/中/低",
      "picture": "用一句话概括这一段画面与动作",
      "fact": "用一句话概括这一段最吸引人的剧情点"
    }}
  ]
}}

证据包：
__SCENE_MARKDOWN__
"""

SHORT_DRAMA_POLISH_PROMPT_TEMPLATE = """
你将收到一组短剧爆点分析，请改写成适合短视频解说的文案。

要求：
1. 风格：悬念感、节奏快、口语化，像在给朋友讲一个精彩故事。
2. 高吸引力片段重点渲染，低吸引力片段精简概括。
3. 可以适当加入问句制造悬念，但不要过度夸张。
4. 每段 narration 严格控制在字数上限内，宁可精炼也不要超出。
5. picture 保留为画面概括。
6. 输出 JSON，格式如下：
{{
  "items": [
    {{
      "_id": 1,
      "timestamp": "00:00:00,000-00:00:03,000",
      "char_budget": 20,
      "attraction_level": "高/中/低",
      "picture": "...",
      "narration": "..."
    }}
  ]
}}

事实版数据：
__FACT_JSON__
"""


def generate_narration(markdown_content, api_key, base_url, model):
    try:
        logger.info("使用新的LLM服务架构生成解说文案")
        result = generate_narration_new(markdown_content, api_key, base_url, model)
        return result
    except Exception as e:
        logger.warning(f"使用新LLM服务失败，回退到旧实现: {str(e)}")
        return _generate_narration_legacy(markdown_content, api_key, base_url, model)


def generate_narration_from_scene_evidence(
    scene_evidence: List[Dict],
    api_key: str,
    base_url: str,
    model: str,
    style: str = "documentary",
) -> List[Dict]:
    """基于场景证据生成解说文案

    Args:
        scene_evidence: 场景证据列表
        api_key: API密钥
        base_url: API基础URL
        model: 模型名称
        style: 风格类型，可选 "documentary"（纪录片）或 "short_drama"（短剧）

    Returns:
        List[Dict]: 生成的脚本片段列表
    """
    if not scene_evidence:
        return []
    if not api_key or not model:
        logger.warning("文本模型配置不完整，回退到本地兜底脚本")
        return _fallback_from_scene_evidence(scene_evidence)

    # 根据风格选择 Prompt 模板
    if style == "short_drama":
        fact_prompt_template = SHORT_DRAMA_FACT_PROMPT_TEMPLATE
        polish_prompt_template = SHORT_DRAMA_POLISH_PROMPT_TEMPLATE
        logger.info(f"使用短剧风格生成解说文案，共 {len(scene_evidence)} 个场景")
    else:
        fact_prompt_template = FACT_PROMPT_TEMPLATE
        polish_prompt_template = POLISH_PROMPT_TEMPLATE
        logger.info(f"使用纪录片风格生成解说文案，共 {len(scene_evidence)} 个场景")

    try:
        client = OpenAI(api_key=api_key, base_url=base_url)
        scene_markdown = scene_evidence_to_markdown(scene_evidence)
        fact_prompt = fact_prompt_template.replace("__SCENE_MARKDOWN__", scene_markdown)
        facts_response = _chat_json(client=client, model=model, prompt=fact_prompt)
        fact_items = facts_response.get("items", []) if isinstance(facts_response, dict) else []
        if not fact_items:
            logger.warning("事实版生成为空，回退到本地兜底")
            return _fallback_from_scene_evidence(scene_evidence)

        fact_json = json.dumps(fact_items, ensure_ascii=False, indent=2)
        polish_prompt = polish_prompt_template.replace("__FACT_JSON__", fact_json)
        polish_response = _chat_json(client=client, model=model, prompt=polish_prompt)
        polished_items = polish_response.get("items", []) if isinstance(polish_response, dict) else []
        if not polished_items:
            polished_items = _facts_to_items(fact_items)
        for idx, item in enumerate(polished_items, start=1):
            item.setdefault("_id", idx)
            item.setdefault("OST", 2)
        return ensure_script_shape(polished_items)
    except Exception as e:
        logger.error(f"基于字幕证据生成解说失败 (style={style}): {e}")
        return _fallback_from_scene_evidence(scene_evidence)


def _generate_narration_legacy(markdown_content, api_key, base_url, model):
    try:
        prompt = PromptManager.get_prompt(category="documentary", name="narration_generation", parameters={"video_frame_description": markdown_content})
        client = OpenAI(api_key=api_key, base_url=base_url)
        if model not in ["deepseek-reasoner"]:
            response = client.chat.completions.create(
                model=model,
                messages=[
                    {"role": "system", "content": SYSTEM_ROLE},
                    {"role": "user", "content": prompt},
                ],
                temperature=0.9,
                response_format={"type": "json_object"},
            )
            if response.choices:
                logger.debug(f"消耗的tokens: {response.usage.total_tokens}")
                return response.choices[0].message.content
            return "生成解说文案失败: 未获取到有效响应"
        response = client.chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": SYSTEM_ROLE},
                {"role": "user", "content": prompt},
            ],
            temperature=0.9,
        )
        if response.choices:
            narration_script = response.choices[0].message.content
            logger.debug(f"文案消耗的tokens: {response.usage.total_tokens}")
            return narration_script.replace("```json", "").replace("```", "")
        return "生成解说文案失败: 未获取到有效响应"
    except Exception:
        return f"调用API生成解说文案时出错: {traceback.format_exc()}"


def _chat_json(client: OpenAI, model: str, prompt: str) -> Dict:
    response = client.chat.completions.create(
        model=model,
        messages=[
            {"role": "system", "content": SYSTEM_ROLE},
            {"role": "user", "content": prompt},
        ],
        temperature=0.7,
        response_format={"type": "json_object"},
    )
    content = response.choices[0].message.content if response.choices else "{}"
    logger.debug(f"LLM tokens: {getattr(response, 'usage', None)}")
    return _parse_json_text(content)


def _parse_json_text(content: str) -> Dict:
    text = (content or "").strip()
    if "```json" in text:
        text = text.split("```json", 1)[1].split("```", 1)[0].strip()
    elif "```" in text:
        text = text.split("```", 1)[1].split("```", 1)[0].strip()
    return json.loads(text)


def _fallback_from_scene_evidence(scene_evidence: List[Dict]) -> List[Dict]:
    items = []
    for idx, scene in enumerate(scene_evidence or [], start=1):
        subtitle_text = (scene.get("subtitle_text") or "").strip()
        visual_summary = (scene.get("visual_summary") or "").strip()
        picture = visual_summary or subtitle_text or "画面出现新的信息点"
        if subtitle_text:
            narration = subtitle_text[:50]
        else:
            narration = visual_summary[:40] or "这一段的关键信息已经出现。"
        items.append({
            "_id": idx,
            "timestamp": scene.get("timestamp"),
            "picture": picture,
            "narration": narration,
            "OST": 2,
        })
    return ensure_script_shape(items)


def _facts_to_items(fact_items: List[Dict]) -> List[Dict]:
    items = []
    for idx, item in enumerate(fact_items or [], start=1):
        narration = item.get("fact") or item.get("picture") or "这一段发生了新的变化。"
        items.append({
            "_id": idx,
            "timestamp": item.get("timestamp"),
            "picture": item.get("picture", ""),
            "narration": narration,
            "OST": 2,
        })
    return items
