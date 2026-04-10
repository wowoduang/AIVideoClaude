import streamlit as st
import os
import shutil
from loguru import logger

from app.utils.utils import storage_dir
from app.config import config


def clear_directory(dir_path, tr):
    """清理指定目录"""
    if os.path.exists(dir_path):
        try:
            for item in os.listdir(dir_path):
                item_path = os.path.join(dir_path, item)
                try:
                    if os.path.isfile(item_path):
                        os.unlink(item_path)
                    elif os.path.isdir(item_path):
                        shutil.rmtree(item_path)
                except Exception as e:
                    logger.error(f"Failed to delete {item_path}: {e}")
            st.success(tr("Directory cleared"))
            logger.info(f"Cleared directory: {dir_path}")
        except Exception as e:
            st.error(f"{tr('Failed to clear directory')}: {str(e)}")
            logger.error(f"Failed to clear directory {dir_path}: {e}")
    else:
        st.warning(tr("Directory does not exist"))

def render_asr_settings(tr):
    """渲染 ASR 字幕生成设置面板"""
    with st.expander(tr("ASR 字幕引擎设置"), expanded=False):
        st.caption("配置自动字幕生成引擎。修改后重启应用生效（或直接修改 config.toml）")

        whisper_cfg = config.whisper if hasattr(config, "whisper") else {}

        col1, col2 = st.columns(2)
        with col1:
            backend_options = {
                "faster-whisper（推荐，时间轴精准）": "faster-whisper",
                "SenseVoice（情感识别强，有漂移风险）": "sensevoice",
                "FunASR Paraformer（中文优化）": "funasr",
            }
            current_backend = whisper_cfg.get("backend", "faster-whisper")
            current_label = next(
                (k for k, v in backend_options.items() if v == current_backend),
                list(backend_options.keys())[0]
            )
            selected_backend_label = st.selectbox(
                "ASR 引擎",
                options=list(backend_options.keys()),
                index=list(backend_options.keys()).index(current_label),
                key="asr_backend_select",
            )
            selected_backend = backend_options[selected_backend_label]

        with col2:
            if selected_backend == "faster-whisper":
                model_options = [
                    "faster-whisper-large-v3",
                    "faster-whisper-large-v2",
                    "faster-whisper-medium",
                    "faster-whisper-small",
                ]
            else:
                model_options = ["SenseVoiceSmall", "iic/SenseVoiceSmall"]

            current_model = whisper_cfg.get("model_size", model_options[0])
            if current_model not in model_options:
                model_options.insert(0, current_model)
            selected_model = st.selectbox(
                "模型大小",
                options=model_options,
                index=model_options.index(current_model) if current_model in model_options else 0,
                key="asr_model_select",
            )

        col3, col4 = st.columns(2)
        with col3:
            lang_options = {"中文 (zh)": "zh", "英文 (en)": "en", "自动检测": ""}
            current_lang = whisper_cfg.get("language", "zh")
            current_lang_label = next((k for k, v in lang_options.items() if v == current_lang), "中文 (zh)")
            selected_lang_label = st.selectbox(
                "识别语言",
                options=list(lang_options.keys()),
                index=list(lang_options.keys()).index(current_lang_label),
                key="asr_language_select",
            )
            selected_lang = lang_options[selected_lang_label]

        with col4:
            device_options = ["cpu", "cuda"]
            current_device = whisper_cfg.get("device", "cpu")
            selected_device = st.selectbox(
                "运行设备",
                options=device_options,
                index=device_options.index(current_device) if current_device in device_options else 0,
                key="asr_device_select",
            )

        initial_prompt = st.text_input(
            "初始提示词（帮助 Whisper 理解内容类型）",
            value=whisper_cfg.get("initial_prompt", "以下是普通话的人声内容，请准确转写字幕。"),
            key="asr_initial_prompt",
        )

        if st.button("保存 ASR 配置", key="save_asr_config"):
            try:
                import toml
                config_path = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(__file__))), "config.toml")
                with open(config_path, "r", encoding="utf-8") as f:
                    cfg = toml.load(f)
                cfg.setdefault("whisper", {})
                cfg["whisper"]["backend"] = selected_backend
                cfg["whisper"]["model_size"] = selected_model
                cfg["whisper"]["language"] = selected_lang
                cfg["whisper"]["device"] = selected_device
                cfg["whisper"]["initial_prompt"] = initial_prompt
                with open(config_path, "w", encoding="utf-8") as f:
                    toml.dump(cfg, f)
                st.success("✅ ASR 配置已保存，重启应用后生效")
                st.info(f"当前设置：{selected_backend} | {selected_model} | 语言={selected_lang or '自动'} | 设备={selected_device}")
            except Exception as e:
                st.error(f"保存失败: {e}")

        # 当前配置状态展示
        st.caption(
            f"**当前运行配置**：引擎={whisper_cfg.get('backend','faster-whisper')} | "
            f"模型={whisper_cfg.get('model_size','large-v3')} | "
            f"语言={whisper_cfg.get('language','zh')} | "
            f"设备={whisper_cfg.get('device','cpu')}"
        )

        if selected_backend == "faster-whisper":
            st.info(
                "💡 **faster-whisper 使用说明**\n\n"
                "1. 下载模型到 `app/models/faster-whisper-large-v3/` 目录\n"
                "2. 时间轴精准，适合影视解说\n"
                "3. CPU 模式下 large-v3 约需 8GB 内存，medium 约需 4GB\n"
                "4. 如显存不足，选择 small 或 medium 模型"
            )
        else:
            st.info(
                "💡 **SenseVoice 使用说明**\n\n"
                "1. 需要安装 funasr: `pip install funasr`\n"
                "2. 支持情感标签识别，时间轴可能有漂移\n"
                "3. 本版本已内置漂移自动修正\n"
                "4. 适合需要情感信息的短视频场景"
            )


def render_system_panel(tr):
    """渲染系统设置面板"""
    render_asr_settings(tr)

    with st.expander(tr("System settings"), expanded=False):
        col1, col2, col3 = st.columns(3)
                
        with col1:
            if st.button(tr("Clear frames"), use_container_width=True):
                clear_directory(os.path.join(storage_dir(), "temp/keyframes"), tr)
                
        with col2:
            if st.button(tr("Clear clip videos"), use_container_width=True):
                clear_directory(os.path.join(storage_dir(), "temp/clip_video"), tr)
                
        with col3:
            if st.button(tr("Clear tasks"), use_container_width=True):
                clear_directory(os.path.join(storage_dir(), "tasks"), tr)
