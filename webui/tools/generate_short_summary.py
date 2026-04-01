#!/usr/bin/env python
# -*- coding: UTF-8 -*-

"""
@Project: NarratoAI
@File   : 短剧解说脚本生成
@Author : 小林同学
@Date   : 2025/5/10 下午10:26
"""

from __future__ import annotations

import json
import os
import re
import time
import traceback

import streamlit as st
from loguru import logger

from app.config import config
from app.services.SDE.short_drama_explanation import (
    analyze_subtitle,
    generate_narration_script,
)
from app.services.subtitle_first_pipeline import run_subtitle_first_pipeline
from app.services.subtitle_text import read_subtitle_text

# 导入新的LLM服务模块 - 确保提供商被注册
import app.services.llm  # noqa: F401
from app.services.llm.migration_adapter import SubtitleAnalyzerAdapter


def parse_and_fix_json(json_string):
    """
    解析并修复 JSON 字符串
    Args:
        json_string: 待解析的 JSON 字符串
    Returns:
        dict: 解析后的字典，如果解析失败返回一个兜底结构或 None
    """
    if not json_string or not json_string.strip():
        logger.error("JSON字符串为空")
        return None

    json_string = json_string.strip()

    try:
        return json.loads(json_string)
    except json.JSONDecodeError as e:
        logger.warning(f"直接JSON解析失败: {e}")

    try:
        fixed_braces = json_string.replace("{{", "{").replace("}}", "}")
        logger.info("修复双大括号格式")
        return json.loads(fixed_braces)
    except json.JSONDecodeError:
        pass

    try:
        json_match = re.search(r"```json\s*(.*?)\s*```", json_string, re.DOTALL)
        if json_match:
            json_content = json_match.group(1).strip()
            logger.info("从代码块中提取JSON内容")
            return json.loads(json_content)
    except json.JSONDecodeError:
        pass

    try:
        start_idx = json_string.find("{")
        end_idx = json_string.rfind("}")
        if start_idx != -1 and end_idx != -1 and end_idx > start_idx:
            json_content = json_string[start_idx : end_idx + 1]
            logger.info("提取大括号包围的JSON内容")
            return json.loads(json_content)
    except json.JSONDecodeError:
        pass

    try:
        fixed_json = json_string
        fixed_json = fixed_json.replace("{{", "{").replace("}}", "}")

        start_idx = fixed_json.find("{")
        end_idx = fixed_json.rfind("}")
        if start_idx != -1 and end_idx != -1 and end_idx > start_idx:
            fixed_json = fixed_json[start_idx : end_idx + 1]

        fixed_json = re.sub(r"#.*", "", fixed_json)
        fixed_json = re.sub(r"//.*", "", fixed_json)
        fixed_json = re.sub(r",\s*}", "}", fixed_json)
        fixed_json = re.sub(r",\s*]", "]", fixed_json)
        fixed_json = re.sub(r"'([^']*)':", r'"\1":', fixed_json)
        fixed_json = re.sub(r"(\w+)(\s*):", r'"\1"\2:', fixed_json)
        fixed_json = re.sub(r'""([^"]*?)""', r'"\1"', fixed_json)

        logger.info("尝试综合修复JSON格式问题后解析")
        return json.loads(fixed_json)
    except json.JSONDecodeError as e:
        logger.debug(f"综合修复失败: {e}")

    logger.error(f"所有JSON解析方法都失败，原始内容: {json_string[:200]}...")

    try:
        return {
            "items": [
                {
                    "_id": 1,
                    "timestamp": "00:00:00,000-00:00:10,000",
                    "picture": "解析失败，使用默认内容",
                    "narration": (
                        json_string[:100] + "..." if len(json_string) > 100 else json_string
                    ),
                    "OST": 0,
                }
            ]
        }
    except Exception:
        return None


def generate_script_short_sunmmary(params, subtitle_path, video_theme, temperature):
    """
    生成短剧解说视频脚本

    支持三种字幕来源：
    1. 选择已有字幕
    2. 上传新字幕
    3. 自动生成字幕（此时 subtitle_path 可以为空）
    """
    progress_bar = st.progress(0)
    status_text = st.empty()

    def update_progress(progress: float, message: str = ""):
        progress_bar.progress(min(int(progress), 100))
        if message:
            status_text.text(f"{progress}% - {message}")
        else:
            status_text.text(f"进度: {progress}%")

    try:
        with st.spinner("正在生成脚本..."):
            if not params.video_origin_path:
                st.error("请先选择视频文件")
                return

            subtitle_mode = st.session_state.get("subtitle_source_mode", "existing_subtitle")
            allow_auto_subtitle = subtitle_mode == "auto_subtitle"

            if not allow_auto_subtitle and (not subtitle_path or not os.path.exists(subtitle_path)):
                st.error("字幕文件不存在")
                return

            text_provider = config.app.get("text_llm_provider", "gemini").lower()
            text_api_key = config.app.get(f"text_{text_provider}_api_key", "")
            text_model = config.app.get(f"text_{text_provider}_model_name", "")
            text_base_url = config.app.get(f"text_{text_provider}_base_url", "")

            generation_mode = st.session_state.get("generation_mode", "balanced")
            visual_mode = st.session_state.get("visual_mode", "auto")
            narration_style = st.session_state.get("narration_style", "short_drama")

            actual_subtitle_path = subtitle_path or ""
            logger.info(
                "尝试使用字幕优先管线生成解说脚本 "
                f"(mode={generation_mode}, visual_mode={visual_mode}, style={narration_style}, "
                f"subtitle_mode={subtitle_mode}, subtitle_path={'AUTO' if not actual_subtitle_path else actual_subtitle_path})"
            )

            pipeline_result = run_subtitle_first_pipeline(
                video_path=params.video_origin_path,
                subtitle_path=actual_subtitle_path,
                text_api_key=text_api_key,
                text_base_url=text_base_url,
                text_model=text_model,
                style=narration_style,
                generation_mode=generation_mode,
                visual_mode=visual_mode,
                progress_callback=update_progress,
            )

            if pipeline_result.get("success") and pipeline_result.get("script_items"):
                logger.success("字幕优先管线成功")

                st.session_state["video_clip_json"] = pipeline_result["script_items"]
                st.session_state["subtitle_first_evidence"] = pipeline_result.get("evidence", [])
                st.session_state["subtitle_first_global_summary"] = pipeline_result.get(
                    "global_summary", {}
                )
                st.session_state["video_clip_json_path"] = pipeline_result.get("script_path", st.session_state.get("video_clip_json_path", ""))

                actual_generated_subtitle = pipeline_result.get("generated_saved_subtitle_path") or pipeline_result.get("subtitle_path", "")
                if actual_generated_subtitle and os.path.exists(actual_generated_subtitle):
                    st.session_state["subtitle_path"] = actual_generated_subtitle
                    st.session_state["short_drama_subtitle_path"] = actual_generated_subtitle
                    st.session_state["short_drama_subtitle_source"] = pipeline_result.get("subtitle_source", "")
                    st.session_state["last_generated_subtitle_path"] = actual_generated_subtitle
                    try:
                        subtitle_obj = read_subtitle_text(actual_generated_subtitle)
                        st.session_state["subtitle_content"] = subtitle_obj.text if subtitle_obj else ""
                    except Exception:
                        pass

                update_progress(100, "脚本生成完成！")
                if actual_generated_subtitle and os.path.exists(actual_generated_subtitle):
                    st.success(f"视频脚本生成成功！字幕已保存: {os.path.basename(actual_generated_subtitle)}")
                else:
                    st.success("视频脚本生成成功！")
                return

            logger.warning(
                f"字幕优先管线未成功 (error={pipeline_result.get('error', '')}), 回退到旧的 LLM 分析方式"
            )

            if allow_auto_subtitle:
                st.error("自动生成字幕失败，请检查后端字幕流水线日志")
                return

            update_progress(30, "正在解析字幕 (legacy)...")

            subtitle_obj = read_subtitle_text(subtitle_path)
            subtitle_content = subtitle_obj.text if subtitle_obj else ""

            if not subtitle_content:
                st.error("字幕文件内容为空或无法读取")
                return

            analyzer = None
            try:
                logger.info("使用新的LLM服务架构进行字幕分析")
                analyzer = SubtitleAnalyzerAdapter(
                    text_api_key,
                    text_model,
                    text_base_url,
                    text_provider,
                )
                analysis_result = analyzer.analyze_subtitle(subtitle_content)
            except Exception as e:
                logger.warning(f"使用新LLM服务失败，回退到旧实现: {str(e)}")
                analysis_result = analyze_subtitle(
                    subtitle_file_path=subtitle_path,
                    api_key=text_api_key,
                    model=text_model,
                    base_url=text_base_url,
                    save_result=True,
                    temperature=temperature,
                    provider=text_provider,
                )

            if analysis_result["status"] != "success":
                logger.error(f"分析失败: {analysis_result['message']}")
                st.error("生成脚本失败，请检查日志")
                st.stop()

            logger.info("字幕分析成功！")
            update_progress(60, "正在生成文案...")

            try:
                if analyzer is None:
                    raise RuntimeError("analyzer 未初始化，转入旧实现")

                logger.info("使用新的LLM服务架构生成解说文案")
                narration_result = analyzer.generate_narration_script(
                    short_name=video_theme,
                    plot_analysis=analysis_result["analysis"],
                    subtitle_content=subtitle_content,
                    temperature=temperature,
                )
            except Exception as e:
                logger.warning(f"使用新LLM服务失败，回退到旧实现: {str(e)}")
                narration_result = generate_narration_script(
                    short_name=video_theme,
                    plot_analysis=analysis_result["analysis"],
                    subtitle_content=subtitle_content,
                    api_key=text_api_key,
                    model=text_model,
                    base_url=text_base_url,
                    save_result=True,
                    temperature=temperature,
                    provider=text_provider,
                )

            if narration_result["status"] != "success":
                logger.info(f"解说文案生成失败: {narration_result['message']}")
                st.error("生成脚本失败，请检查日志")
                st.stop()

            logger.info("解说文案生成成功！")

            narration_script = narration_result["narration_script"]
            narration_dict = parse_and_fix_json(narration_script)

            if narration_dict is None:
                st.error("生成的解说文案格式错误，无法解析为JSON")
                logger.error(f"JSON解析失败，原始内容: {narration_script}")
                st.stop()

            if "items" not in narration_dict:
                st.error("生成的解说文案缺少必要的'items'字段")
                logger.error(f"JSON结构错误，缺少items字段: {narration_dict}")
                st.stop()

            script = json.dumps(narration_dict["items"], ensure_ascii=False, indent=2)
            if script is None:
                st.error("生成脚本失败，请检查日志")
                st.stop()

            logger.success("剪辑脚本生成完成")

            if isinstance(script, list):
                st.session_state["video_clip_json"] = script
            elif isinstance(script, str):
                st.session_state["video_clip_json"] = json.loads(script)

            update_progress(90, "整理输出...")
            time.sleep(0.1)
            progress_bar.progress(100)
            status_text.text("脚本生成完成！")
            st.success("视频脚本生成成功！")

    except Exception as err:
        st.error(f"生成过程中发生错误: {str(err)}")
        logger.exception(f"生成脚本时发生错误\n{traceback.format_exc()}")
    finally:
        time.sleep(2)
        progress_bar.empty()
        status_text.empty()
