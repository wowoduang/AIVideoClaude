import json
import os
import time
import traceback
from datetime import datetime

import streamlit as st
from loguru import logger

from app.config import config
from app.services.upload_validation import ensure_existing_file, InputValidationError
from app.services.evidence_fuser import fuse_scene_evidence
from app.services.generate_narration_script import generate_narration_from_scene_evidence
from app.services.scene_builder import build_scenes_from_subtitles
from app.services.script_fallback import ensure_script_shape
from app.services.subtitle_pipeline import build_subtitle_segments
from app.utils import utils


def generate_script_short(tr, params, custom_clips=5):
    """
    生成短视频脚本 - 使用共享底座 + 短剧风格策略
    
    重构说明：
    - 复用纪录片管道的字幕标准化、场景切分、证据融合模块
    - 跳过关键帧提取和视觉分析（短剧模式不需要）
    - 使用 style="short_drama" 的 Prompt 模板生成解说文案
    
    Args:
        tr: 翻译函数
        params: 视频参数对象
        custom_clips: 自定义片段数量，默认为5（暂未使用，保留兼容）
    """
    progress_bar = st.progress(0)
    status_text = st.empty()

    def update_progress(progress: float, message: str = ""):
        progress_bar.progress(progress)
        if message:
            status_text.text(f"{progress}% - {message}")
        else:
            status_text.text(f"进度: {progress}%")

    try:
        with st.spinner("正在生成脚本..."):
            # ========== 严格验证：必须上传视频和字幕 ==========
            # 1. 验证视频文件
            video_path = getattr(params, "video_origin_path", None)
            if not video_path or not str(video_path).strip():
                st.error("请先选择视频文件")
                st.stop()

            try:
                ensure_existing_file(
                    str(video_path),
                    label="视频",
                    allowed_exts=(".mp4", ".mov", ".avi", ".flv", ".mkv"),
                )
            except InputValidationError as e:
                st.error(str(e))
                st.stop()

            # 2. 验证字幕文件（必须上传）
            subtitle_path = st.session_state.get("subtitle_path")
            if not subtitle_path or not str(subtitle_path).strip():
                st.error("请先上传字幕文件")
                st.stop()

            try:
                subtitle_path = ensure_existing_file(
                    str(subtitle_path),
                    label="字幕",
                    allowed_exts=(".srt",),
                )
            except InputValidationError as e:
                st.error(str(e))
                st.stop()

            logger.info(f"[短剧模式] 使用用户上传的字幕文件: {subtitle_path}")
            update_progress(10, "正在加载字幕...")

            # ========== Step 1: 字幕标准化（共享模块）==========
            subtitle_result = build_subtitle_segments(
                video_path=video_path,
                explicit_subtitle_path=subtitle_path,
            )
            subtitle_segments = subtitle_result.get("segments", [])
            
            if not subtitle_segments:
                error = subtitle_result.get("error", "字幕解析失败")
                st.error(f"字幕处理失败: {error}")
                st.stop()
            
            logger.info(f"[短剧模式] 字幕标准化完成，共 {len(subtitle_segments)} 段")
            st.success(f"✅ 字幕加载成功，共 {len(subtitle_segments)} 段")
            update_progress(30, "正在构建场景...")

            # ========== Step 2: 场景切分（共享模块）==========
            scenes = build_scenes_from_subtitles(subtitle_segments)
            if not scenes:
                st.error("场景切分失败，无法生成脚本")
                st.stop()
            
            logger.info(f"[短剧模式] 场景切分完成，共 {len(scenes)} 个场景")
            update_progress(50, "正在构建证据包...")

            # ========== Step 3: 证据融合（共享模块，无视觉分析）==========
            # 短剧模式不进行关键帧提取和视觉分析，frame_records 和 visual_observations 传空
            frame_records = []  # 短剧模式不需要代表帧
            visual_observations = {}  # 短剧模式不需要视觉分析
            
            scene_evidence = fuse_scene_evidence(scenes, frame_records, visual_observations)
            
            # 标记证据模式和
            for scene in scene_evidence:
                scene['evidence_mode'] = 'subtitle_only'
                scene['visual_budget_meta'] = {"estimated_tokens": 0, "estimated_cost_cny": 0.0, "capped": 0, "original": 0}
            
            logger.info(f"[短剧模式] 证据融合完成（仅字幕证据），共 {len(scene_evidence)} 个场景证据")
            
            # 保存分析结果（调试用）
            analysis_json_path = _save_analysis(scene_evidence)
            logger.info(f"[短剧模式] 分析结果已保存到: {analysis_json_path}")
            
            update_progress(70, "正在生成解说文案...")

            # ========== Step 4: 生成解说文案（短剧风格）==========
            text_provider = config.app.get('text_llm_provider', 'gemini').lower()
            text_api_key = config.app.get(f'text_{text_provider}_api_key')
            text_model = config.app.get(f'text_{text_provider}_model_name')
            text_base_url = config.app.get(f'text_{text_provider}_base_url')
            
            narration_items = generate_narration_from_scene_evidence(
                scene_evidence=scene_evidence,
                api_key=text_api_key,
                base_url=text_base_url,
                model=text_model,
                style="short_drama",  # 使用短剧风格
            )
            
            # 格式化和时长预算
            narration_items = ensure_script_shape(narration_items)
            
            if not narration_items:
                st.error("未生成有效脚本片段")
                st.stop()
            
            logger.info(f"[短剧模式] 脚本生成完成，共 {len(narration_items)} 个片段")
            logger.info(f"脚本内容: {json.dumps(narration_items, ensure_ascii=False, indent=4)}")
            
            st.session_state['video_clip_json'] = narration_items
            update_progress(90, "脚本生成完成")

        time.sleep(0.1)
        progress_bar.progress(100)
        status_text.text("脚本生成完成！")
        st.success("✅ 短剧脚本生成成功！")

    except Exception as err:
        progress_bar.progress(100)
        st.error(f"生成过程中发生错误: {str(err)}")
        logger.exception(f"[短剧模式] 生成脚本时发生错误\n{traceback.format_exc()}")


def _save_analysis(scene_evidence):
    """保存分析结果到文件（调试用）"""
    analysis_dir = os.path.join(utils.storage_dir(), "temp", "analysis")
    os.makedirs(analysis_dir, exist_ok=True)
    now = datetime.now().strftime("%Y%m%d_%H%M%S")
    path = os.path.join(analysis_dir, f"short_drama_evidence_{now}.json")
    payload = {"scene_evidence": scene_evidence, "mode": "short_drama_subtitle_only"}
    with open(path, 'w', encoding='utf-8') as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
    return path


# ============ Legacy 模式切换（可选）============
def generate_script_short_legacy(tr, params, custom_clips=5):
    """
    生成短视频脚本 - Legacy 模式（使用原有 SDP 管道）
    
    保留此函数以便在需要时切换回原有实现。
    """
    progress_bar = st.progress(0)
    status_text = st.empty()

    def update_progress(progress: float, message: str = ""):
        progress_bar.progress(progress)
        if message:
            status_text.text(f"{progress}% - {message}")
        else:
            status_text.text(f"进度: {progress}%")

    try:
        with st.spinner("正在生成脚本..."):
            # ========== 严格验证：必须上传视频和字幕（与短剧解说保持一致）==========
            # 1. 验证视频文件
            video_path = getattr(params, "video_origin_path", None)
            if not video_path or not str(video_path).strip():
                st.error("请先选择视频文件")
                st.stop()

            try:
                ensure_existing_file(
                    str(video_path),
                    label="视频",
                    allowed_exts=(".mp4", ".mov", ".avi", ".flv", ".mkv"),
                )
            except InputValidationError as e:
                st.error(str(e))
                st.stop()

            # 2. 验证字幕文件（移除推断逻辑，必须上传）
            subtitle_path = st.session_state.get("subtitle_path")
            if not subtitle_path or not str(subtitle_path).strip():
                st.error("请先上传字幕文件")
                st.stop()

            try:
                subtitle_path = ensure_existing_file(
                    str(subtitle_path),
                    label="字幕",
                    allowed_exts=(".srt",),
                )
            except InputValidationError as e:
                st.error(str(e))
                st.stop()

            logger.info(f"使用用户上传的字幕文件: {subtitle_path}")

            # ========== 获取 LLM 配置 ==========
            text_provider = config.app.get('text_llm_provider', 'gemini').lower()
            text_api_key = config.app.get(f'text_{text_provider}_api_key')
            text_model = config.app.get(f'text_{text_provider}_model_name')
            text_base_url = config.app.get(f'text_{text_provider}_base_url')

            update_progress(20, "开始准备生成脚本")

            # ========== 调用后端生成脚本 ==========
            from app.services.SDP.generate_script_short import generate_script_result

            output_path = os.path.join(utils.script_dir(), "merged_subtitle.json")

            subtitle_content = st.session_state.get("subtitle_content")
            subtitle_kwargs = (
                {"subtitle_content": str(subtitle_content)}
                if subtitle_content is not None and str(subtitle_content).strip()
                else {"subtitle_file_path": subtitle_path}
            )

            result = generate_script_result(
                api_key=text_api_key,
                model_name=text_model,
                output_path=output_path,
                base_url=text_base_url,
                custom_clips=custom_clips,
                provider=text_provider,
                **subtitle_kwargs,
            )

            if result.get("status") != "success":
                st.error(result.get("message", "生成脚本失败，请检查日志"))
                st.stop()

            script = result.get("script")
            logger.info(f"脚本生成完成 {json.dumps(script, ensure_ascii=False, indent=4)}")

            if isinstance(script, list):
                st.session_state['video_clip_json'] = script
            elif isinstance(script, str):
                st.session_state['video_clip_json'] = json.loads(script)

            update_progress(80, "脚本生成完成")

        time.sleep(0.1)
        progress_bar.progress(100)
        status_text.text("脚本生成完成！")
        st.success("视频脚本生成成功！")

    except Exception as err:
        progress_bar.progress(100)
        st.error(f"生成过程中发生错误: {str(err)}")
        logger.exception(f"生成脚本时发生错误\n{traceback.format_exc()}")
