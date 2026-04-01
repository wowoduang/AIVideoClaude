from __future__ import annotations

import glob
import hashlib
import json
import os
import time
import traceback

import streamlit as st
from loguru import logger

from app.config import config
from app.models.schema import VideoAspect, VideoClipParams
from app.services.subtitle_text import decode_subtitle_bytes
from app.services.timeline_allocator import fit_check
from app.utils import check_script, utils
from webui.components.subtitle_first_mode_panel import render_subtitle_first_mode_panel
from webui.tools.generate_short_summary import generate_script_short_sunmmary


MODE_FILE = "file_selection"
MODE_SUBTITLE_FIRST = "summary"

OST_OPTIONS = [0, 1, 2]
OST_LABELS = {
    0: "TTS配音",
    1: "保留原声",
    2: "TTS+原声混合",
}


def _get_uploaded_file_signature(uploaded_file):
    if uploaded_file is None:
        return "", b""

    raw_bytes = uploaded_file.getvalue()
    digest = hashlib.md5(raw_bytes).hexdigest()
    return f"{uploaded_file.name}:{len(raw_bytes)}:{digest}", raw_bytes


def _build_unique_upload_path(save_dir: str, original_name: str) -> str:
    os.makedirs(save_dir, exist_ok=True)

    safe_filename = os.path.basename(original_name)
    file_name, file_extension = os.path.splitext(safe_filename)
    save_path = os.path.join(save_dir, safe_filename)

    if os.path.exists(save_path):
        timestamp = time.strftime("%Y%m%d%H%M%S")
        save_path = os.path.join(save_dir, f"{file_name}_{timestamp}{file_extension}")

    return save_path


def _clear_upload_cache(*keys: str):
    for key in keys:
        st.session_state.pop(key, None)


def get_script_params() -> dict:
    """供 webui.render_generate_button() 合并参数使用，必须返回 mapping。"""
    return {
        "video_clip_json": st.session_state.get("video_clip_json", []),
        "video_clip_json_path": st.session_state.get("video_clip_json_path", ""),
        "video_origin_path": st.session_state.get("video_origin_path", ""),
        "video_aspect": st.session_state.get("video_aspect", VideoAspect.portrait.value),
        "video_language": st.session_state.get("video_language", "zh-CN"),
        "voice_name": st.session_state.get("voice_name", "zh-CN-YunjianNeural"),
        "voice_volume": float(st.session_state.get("voice_volume", 1.0)),
        "voice_rate": float(st.session_state.get("voice_rate", 1.0)),
        "voice_pitch": float(st.session_state.get("voice_pitch", 1.0)),
        "tts_engine": st.session_state.get("tts_engine", ""),
        "bgm_name": st.session_state.get("bgm_name", "random"),
        "bgm_type": st.session_state.get("bgm_type", "random"),
        "bgm_file": st.session_state.get("bgm_file", ""),
        "subtitle_enabled": bool(st.session_state.get("subtitle_enabled", True)),
        "font_name": st.session_state.get("font_name", "SimHei"),
        "font_size": int(st.session_state.get("font_size", 36)),
        "text_fore_color": st.session_state.get("text_fore_color", "white"),
        "text_back_color": st.session_state.get("text_back_color"),
        "stroke_color": st.session_state.get("stroke_color", "black"),
        "stroke_width": float(st.session_state.get("stroke_width", 1.5)),
        "subtitle_position": st.session_state.get("subtitle_position", "bottom"),
        "custom_position": float(st.session_state.get("custom_position", 70.0)),
        "n_threads": int(st.session_state.get("n_threads", 16)),
        "tts_volume": float(st.session_state.get("tts_volume", 1.0)),
        "original_volume": float(st.session_state.get("original_volume", 1.2)),
        "bgm_volume": float(st.session_state.get("bgm_volume", 0.3)),
    }


def render_script_panel(tr):
    with st.container(border=True):
        st.write(tr("Video Script Configuration"))
        params = VideoClipParams()

        _normalize_legacy_mode()
        render_script_file(tr, params)
        render_video_file(tr, params)

        script_path = st.session_state.get("video_clip_json_path", "")
        if script_path == MODE_SUBTITLE_FIRST:
            render_subtitle_first_generate_panel(tr)

        render_script_buttons(tr, params)


def _normalize_legacy_mode():
    current = st.session_state.get("video_clip_json_path", "")
    if current in ("auto", "short"):
        st.session_state["video_clip_json_path"] = MODE_SUBTITLE_FIRST


def render_script_file(tr, params):
    if st.session_state.get("_switch_to_file_mode"):
        st.session_state["script_mode_selection"] = tr("Select/Upload Script")
        del st.session_state["_switch_to_file_mode"]

    mode_options = {
        tr("Select/Upload Script"): MODE_FILE,
        tr("字幕优先生成"): MODE_SUBTITLE_FIRST,
    }

    current_path = st.session_state.get("video_clip_json_path", "")
    mode_keys = list(mode_options.keys())

    if current_path == MODE_SUBTITLE_FIRST:
        default_index = mode_keys.index(tr("字幕优先生成"))
    else:
        default_index = mode_keys.index(tr("Select/Upload Script"))

    default_mode_label = mode_keys[default_index]

    def update_script_mode():
        selected_label = st.session_state.script_mode_selection
        if selected_label:
            new_mode = mode_options[selected_label]
            st.session_state["video_clip_json_path"] = new_mode
            params.video_clip_json_path = new_mode
        else:
            st.session_state["script_mode_selection"] = default_mode_label

    selected_mode_label = st.segmented_control(
        tr("脚本来源"),
        options=mode_keys,
        default=default_mode_label,
        key="script_mode_selection",
        on_change=update_script_mode,
    )

    if not selected_mode_label:
        selected_mode_label = default_mode_label

    selected_mode = mode_options[selected_mode_label]

    if selected_mode == MODE_FILE:
        script_list = [(tr("None"), ""), (tr("Upload Script"), "upload_script")]
        script_dir = utils.script_dir()
        files = glob.glob(os.path.join(script_dir, "*.json"))
        file_list = [
            {"name": os.path.basename(file), "file": file, "ctime": os.path.getctime(file)}
            for file in files
        ]
        file_list.sort(key=lambda x: x["ctime"], reverse=True)

        for file in file_list:
            display_name = file["file"].replace(config.root_dir, "")
            script_list.append((display_name, file["file"]))

        saved_script_path = current_path if current_path not in (MODE_SUBTITLE_FIRST, MODE_FILE) else ""
        selected_index = 0
        for i, (_, path) in enumerate(script_list):
            if path == saved_script_path:
                selected_index = i
                break

        selected_script_index = st.selectbox(
            tr("Script Files"),
            index=selected_index,
            options=range(len(script_list)),
            format_func=lambda x: script_list[x][0],
            key="script_file_selection",
        )

        script_path = script_list[selected_script_index][1]
        if script_path:
            st.session_state["video_clip_json_path"] = script_path
            params.video_clip_json_path = script_path
        elif saved_script_path:
            st.session_state["video_clip_json_path"] = saved_script_path
            params.video_clip_json_path = saved_script_path

        if script_path == "upload_script":
            uploaded_file = st.file_uploader(
                tr("Upload Script File"),
                type=["json"],
                accept_multiple_files=False,
                key="upload_script_file",
            )
            if uploaded_file is not None:
                try:
                    upload_sig, raw_bytes = _get_uploaded_file_signature(uploaded_file)
                    cached_sig = st.session_state.get("upload_script_file_sig", "")
                    cached_path = st.session_state.get("upload_script_saved_path", "")

                    if upload_sig == cached_sig and cached_path and os.path.exists(cached_path):
                        st.session_state["video_clip_json_path"] = cached_path
                        params.video_clip_json_path = cached_path
                    else:
                        script_content = raw_bytes.decode("utf-8")
                        json_data = json.loads(script_content)
                        script_file_path = _build_unique_upload_path(script_dir, uploaded_file.name)

                        with open(script_file_path, "w", encoding="utf-8") as f:
                            json.dump(json_data, f, ensure_ascii=False, indent=2)

                        st.session_state["upload_script_file_sig"] = upload_sig
                        st.session_state["upload_script_saved_path"] = script_file_path
                        st.session_state["video_clip_json_path"] = script_file_path
                        params.video_clip_json_path = script_file_path
                        st.success(tr("Script Uploaded Successfully"))
                        time.sleep(1)
                        st.rerun()
                except json.JSONDecodeError:
                    st.error(tr("Invalid JSON format"))
                except Exception as e:
                    st.error(f"{tr('Upload failed')}: {str(e)}")
                    logger.error(traceback.format_exc())
            else:
                _clear_upload_cache("upload_script_file_sig", "upload_script_saved_path")
    else:
        st.session_state["video_clip_json_path"] = MODE_SUBTITLE_FIRST
        params.video_clip_json_path = MODE_SUBTITLE_FIRST


def render_video_file(tr, params):
    source_options = {
        tr("选择已有视频"): "existing_video",
        tr("上传新视频"): "upload_video",
    }

    current_video_path = st.session_state.get("video_origin_path", "")
    default_video_source = (
        tr("选择已有视频") if current_video_path not in ("upload_local", "") else tr("上传新视频")
    )

    selected_video_source = st.segmented_control(
        tr("视频来源"),
        options=list(source_options.keys()),
        default=default_video_source,
        key="video_source_selection",
    )

    if not selected_video_source:
        selected_video_source = default_video_source

    source_mode = source_options[selected_video_source]

    if source_mode == "existing_video":
        video_list = [(tr("None"), "")]
        for suffix in ["*.mp4", "*.mov", "*.avi", "*.mkv", "*.flv"]:
            for file in glob.glob(os.path.join(utils.video_dir(), suffix)):
                display_name = file.replace(config.root_dir, "")
                video_list.append((display_name, file))

        saved_video_path = current_video_path if current_video_path not in ("", "upload_local") else ""
        selected_index = 0
        for i, (_, path) in enumerate(video_list):
            if path == saved_video_path:
                selected_index = i
                break

        selected_video_index = st.selectbox(
            tr("已有视频文件"),
            index=selected_index,
            options=range(len(video_list)),
            format_func=lambda x: video_list[x][0],
            key="existing_video_selection",
        )

        video_path = video_list[selected_video_index][1]
        st.session_state["video_origin_path"] = video_path
        params.video_origin_path = video_path

    else:
        uploaded_file = st.file_uploader(
            tr("上传视频文件"),
            type=["mp4", "mov", "avi", "flv", "mkv"],
            accept_multiple_files=False,
            key="upload_video_file",
        )
        if uploaded_file is not None:
            upload_sig, raw_bytes = _get_uploaded_file_signature(uploaded_file)
            cached_sig = st.session_state.get("upload_video_file_sig", "")
            cached_path = st.session_state.get("upload_video_saved_path", "")

            if upload_sig == cached_sig and cached_path and os.path.exists(cached_path):
                st.session_state["video_origin_path"] = cached_path
                params.video_origin_path = cached_path
            else:
                video_file_path = _build_unique_upload_path(utils.video_dir(), uploaded_file.name)
                with open(video_file_path, "wb") as f:
                    f.write(raw_bytes)

                st.session_state["upload_video_file_sig"] = upload_sig
                st.session_state["upload_video_saved_path"] = video_file_path
                st.session_state["video_origin_path"] = video_file_path
                params.video_origin_path = video_file_path
                st.success(tr("File Uploaded Successfully"))
                time.sleep(1)
                st.rerun()
        else:
            cached_path = st.session_state.get("upload_video_saved_path", "")
            if cached_path and os.path.exists(cached_path):
                st.session_state["video_origin_path"] = cached_path
                params.video_origin_path = cached_path
            else:
                _clear_upload_cache("upload_video_file_sig", "upload_video_saved_path")
                st.session_state["video_origin_path"] = ""
                params.video_origin_path = ""


def render_subtitle_first_generate_panel(tr):
    if "subtitle_file_processed" not in st.session_state:
        st.session_state["subtitle_file_processed"] = False
    if "subtitle_source_mode" not in st.session_state:
        st.session_state["subtitle_source_mode"] = "existing_subtitle"

    st.caption("subtitle-ui-version: v_auto_subtitle_3buttons_ostfix")

    subtitle_source_options = {
        tr("选择已有字幕"): "existing_subtitle",
        tr("上传新字幕"): "upload_subtitle",
        tr("自动生成字幕"): "auto_subtitle",
    }

    current_subtitle_path = st.session_state.get("subtitle_path", "")
    default_subtitle_source = st.session_state.get("subtitle_source_mode", "existing_subtitle")
    if default_subtitle_source not in subtitle_source_options.values():
        default_subtitle_source = "existing_subtitle"

    default_label = None
    for label, value in subtitle_source_options.items():
        if value == default_subtitle_source:
            default_label = label
            break
    if default_label is None:
        default_label = tr("选择已有字幕")

    selected_subtitle_source = st.segmented_control(
        tr("字幕来源"),
        options=list(subtitle_source_options.keys()),
        default=default_label,
        key="subtitle_source_selection",
    )

    if not selected_subtitle_source:
        selected_subtitle_source = default_label

    subtitle_source_mode = subtitle_source_options[selected_subtitle_source]
    st.session_state["subtitle_source_mode"] = subtitle_source_mode

    if subtitle_source_mode == "existing_subtitle":
        subtitle_list = [(tr("None"), "")]
        subtitle_dir = utils.subtitle_dir() if hasattr(utils, "subtitle_dir") else utils.temp_dir()
        subtitle_files = []
        for suffix in ["*.srt", "*.ass", "*.ssa", "*.vtt"]:
            for file in glob.glob(os.path.join(subtitle_dir, suffix)):
                subtitle_files.append(file)

        subtitle_files = sorted(set(subtitle_files), key=lambda x: os.path.getctime(x), reverse=True)
        for file in subtitle_files:
            display_name = file.replace(config.root_dir, "")
            subtitle_list.append((display_name, file))

        fallback_recent_generated = st.session_state.get("last_generated_subtitle_path", "")
        saved_subtitle_path = current_subtitle_path if current_subtitle_path and os.path.exists(current_subtitle_path) else ""
        if not saved_subtitle_path and fallback_recent_generated and os.path.exists(fallback_recent_generated):
            saved_subtitle_path = fallback_recent_generated
        selected_index = 0
        for i, (_, path) in enumerate(subtitle_list):
            if path == saved_subtitle_path:
                selected_index = i
                break

        selected_subtitle_index = st.selectbox(
            tr("已有字幕文件"),
            index=selected_index,
            options=range(len(subtitle_list)),
            format_func=lambda x: subtitle_list[x][0],
            key="existing_subtitle_selection",
        )

        subtitle_path = subtitle_list[selected_subtitle_index][1]
        if subtitle_path:
            st.session_state["subtitle_path"] = subtitle_path
            st.session_state["subtitle_file_processed"] = True
            try:
                with open(subtitle_path, "rb") as f:
                    decoded = decode_subtitle_bytes(f.read())
                st.session_state["subtitle_content"] = decoded.text
                st.info(f"{tr('已选择字幕文件')}: {os.path.basename(subtitle_path)}")
            except Exception as e:
                st.error(f"{tr('读取字幕失败')}: {str(e)}")
        else:
            st.session_state["subtitle_path"] = ""
            st.session_state["subtitle_content"] = None
            st.session_state["subtitle_file_processed"] = False

    elif subtitle_source_mode == "upload_subtitle":
        subtitle_file = st.file_uploader(
            tr("上传字幕文件"),
            type=["srt", "ass", "ssa", "vtt"],
            accept_multiple_files=False,
            key="subtitle_file_uploader",
        )

        if subtitle_file is not None:
            try:
                upload_sig, raw_bytes = _get_uploaded_file_signature(subtitle_file)
                cached_sig = st.session_state.get("subtitle_upload_file_sig", "")
                cached_path = st.session_state.get("subtitle_upload_file_path", "")

                if upload_sig == cached_sig and cached_path and os.path.exists(cached_path):
                    st.session_state["subtitle_path"] = cached_path
                    st.session_state["subtitle_file_processed"] = True
                    if not st.session_state.get("subtitle_content"):
                        with open(cached_path, "r", encoding="utf-8") as f:
                            st.session_state["subtitle_content"] = f.read()
                else:
                    decoded = decode_subtitle_bytes(raw_bytes)
                    subtitle_content = decoded.text
                    detected_encoding = decoded.encoding

                    if not subtitle_content:
                        st.error(tr("无法读取字幕文件，请检查文件编码（支持 UTF-8、UTF-16、GBK、GB2312）"))
                        st.stop()

                    subtitle_dir = utils.subtitle_dir() if hasattr(utils, "subtitle_dir") else utils.temp_dir()
                    subtitle_file_path = _build_unique_upload_path(subtitle_dir, subtitle_file.name)

                    with open(subtitle_file_path, "w", encoding="utf-8") as f:
                        f.write(subtitle_content)

                    st.session_state["subtitle_upload_file_sig"] = upload_sig
                    st.session_state["subtitle_upload_file_path"] = subtitle_file_path
                    st.session_state["subtitle_path"] = subtitle_file_path
                    st.session_state["subtitle_content"] = subtitle_content
                    st.session_state["subtitle_file_processed"] = True

                    st.success(
                        f"{tr('已获得字幕，进入字幕优先模式')} "
                        f"(编码: {detected_encoding.upper()}, 大小: {len(subtitle_content)} 字符)"
                    )
            except Exception as e:
                st.error(f"{tr('Upload failed')}: {str(e)}")
                logger.error(traceback.format_exc())
        else:
            cached_path = st.session_state.get("subtitle_upload_file_path", "")
            if cached_path and os.path.exists(cached_path):
                st.session_state["subtitle_path"] = cached_path
                st.session_state["subtitle_file_processed"] = True
            else:
                st.session_state["subtitle_file_processed"] = False
                _clear_upload_cache("subtitle_upload_file_sig", "subtitle_upload_file_path")

    else:
        st.session_state["subtitle_path"] = ""
        st.session_state["subtitle_content"] = None
        st.session_state["subtitle_file_processed"] = False
        st.info(tr("将根据视频自动生成字幕，然后再生成解说脚本。"))
        recent_generated = st.session_state.get("last_generated_subtitle_path", "")
        if recent_generated and os.path.exists(recent_generated):
            st.caption(f"最近自动生成字幕: {os.path.basename(recent_generated)}")

    if st.session_state.get("subtitle_path") or subtitle_source_mode == "auto_subtitle":
        render_subtitle_first_mode_panel(tr)

    st.text_input(
        tr("短剧名称"),
        value=st.session_state.get("short_name", ""),
        key="short_name",
    )

    st.slider(
        "temperature",
        0.0,
        2.0,
        float(st.session_state.get("temperature", 0.7)),
        key="temperature",
    )


def render_script_buttons(tr, params):
    script_path = st.session_state.get("video_clip_json_path", "")

    if script_path == MODE_SUBTITLE_FIRST:
        if st.session_state.get("subtitle_source_mode") == "auto_subtitle":
            button_name = tr("自动生成字幕并生成解说脚本")
        else:
            button_name = tr("生成字幕优先解说脚本")
    elif isinstance(script_path, str) and script_path.endswith("json"):
        button_name = tr("Load Video Script")
    else:
        button_name = tr("Please Select Script File")

    if st.button(button_name, key="script_action", disabled=not script_path):
        if script_path == MODE_SUBTITLE_FIRST:
            subtitle_mode = st.session_state.get("subtitle_source_mode", "existing_subtitle")
            subtitle_path = st.session_state.get("subtitle_path", "")
            if subtitle_mode != "auto_subtitle" and (not subtitle_path or not os.path.exists(subtitle_path)):
                st.error(tr("字幕文件不存在"))
                return

            video_theme = st.session_state.get("short_name") or st.session_state.get("video_theme", "")
            temperature = st.session_state.get("temperature", 0.7)
            generate_script_short_sunmmary(params, subtitle_path, video_theme, temperature)
        else:
            load_script(tr, script_path)

    _render_evidence_preview(tr)
    video_clip_json_details = _render_script_editor(tr)

    if st.button(tr("Save Script"), key="save_script", use_container_width=True):
        save_script_with_validation(tr, video_clip_json_details)


def _render_evidence_preview(tr):
    evidence = st.session_state.get("subtitle_first_evidence", [])
    global_summary = st.session_state.get("subtitle_first_global_summary", {})

    if not evidence:
        return

    with st.expander(tr("Evidence Preview"), expanded=False):
        total_chars = sum(len(pkg.get("subtitle_text", "")) for pkg in evidence)
        estimated_tokens = int(total_chars * 1.5)

        st.caption(
            f"Scenes: {len(evidence)} | Subtitle chars: {total_chars} | Est. tokens: {estimated_tokens}"
        )

        if global_summary:
            st.write("**Global Summary**")
            st.json(global_summary)

        max_items = min(len(evidence), 8)
        for i, pkg in enumerate(evidence[:max_items], start=1):
            title = f"#{i} {pkg.get('scene_id', '')} {pkg.get('timestamp', '')}"
            with st.container(border=True):
                st.write(title)
                st.write(pkg.get("subtitle_text", ""))
                if pkg.get("visual_only"):
                    st.caption("visual_only")
                if pkg.get("frame_paths"):
                    st.caption(f"frames: {len(pkg.get('frame_paths', []))}")


def _render_script_editor(tr):
    video_clip_json = st.session_state.get("video_clip_json", [])
    if not video_clip_json:
        return []

    st.write(tr("Video Script"))

    edited_items = []
    for index, item in enumerate(video_clip_json, start=1):
        timestamp = item.get("timestamp", "")
        picture = item.get("picture", "")
        narration = item.get("narration", "")
        raw_ost = int(item.get("OST", 2) or 2)
        if raw_ost not in OST_OPTIONS:
            raw_ost = 2

        with st.container(border=True):
            top_cols = st.columns([1, 3, 2])
            with top_cols[0]:
                st.write(f"#{index}")
            with top_cols[1]:
                st.caption(timestamp)
            with top_cols[2]:
                ost_value = st.selectbox(
                    "OST",
                    options=OST_OPTIONS,
                    index=OST_OPTIONS.index(raw_ost),
                    format_func=lambda x: OST_LABELS.get(x, str(x)),
                    key=f"ost_{index}",
                )

            picture_value = st.text_input(
                tr("Picture Description"),
                value=picture,
                key=f"picture_{index}",
            )
            narration_value = st.text_area(
                tr("Narration"),
                value=narration,
                height=120,
                key=f"narration_{index}",
            )

            duration = _estimate_duration_from_timestamp(timestamp)
            if duration > 0 and int(ost_value) in [0, 2]:
                fit = fit_check(narration_value, duration)
                if not fit["fits"]:
                    st.warning(
                        f"字数可能超时: budget={fit['budget']}, actual={fit['actual']}, overflow={fit['overflow']}"
                    )

            edited_item = dict(item)
            edited_item["picture"] = picture_value
            edited_item["narration"] = narration_value
            edited_item["OST"] = int(ost_value)
            edited_items.append(edited_item)

    st.session_state["video_clip_json"] = edited_items
    return edited_items


def _estimate_duration_from_timestamp(timestamp: str) -> float:
    if not timestamp or "-" not in timestamp:
        return 0.0

    try:
        start_text, end_text = timestamp.split("-", 1)
        return _parse_ts(end_text) - _parse_ts(start_text)
    except Exception:
        return 0.0


def _parse_ts(value: str) -> float:
    value = value.strip().replace(",", ".")
    parts = value.split(":")
    if len(parts) != 3:
        return 0.0
    h, m, s = parts
    return int(h) * 3600 + int(m) * 60 + float(s)


def load_script(tr, script_path: str):
    try:
        with open(script_path, "r", encoding="utf-8") as f:
            data = json.load(f)

        if isinstance(data, dict) and "items" in data:
            data = data["items"]

        if not isinstance(data, list):
            st.error(tr("Invalid script format"))
            return

        st.session_state["video_clip_json"] = data
        st.success(tr("Script loaded successfully"))
    except Exception as e:
        st.error(f"{tr('Load script failed')}: {str(e)}")
        logger.error(traceback.format_exc())


def save_script_with_validation(tr, video_clip_json_details):
    try:
        items = video_clip_json_details or st.session_state.get("video_clip_json", [])
        if not items:
            st.warning(tr("No script content to save"))
            return

        try:
            check_script.check_format(items)
        except Exception as e:
            st.warning(f"{tr('Script format warning')}: {str(e)}")

        output_dir = utils.script_dir()
        os.makedirs(output_dir, exist_ok=True)

        timestamp = time.strftime("%Y%m%d%H%M%S")
        file_name = f"script_{timestamp}.json"
        save_path = os.path.join(output_dir, file_name)

        with open(save_path, "w", encoding="utf-8") as f:
            json.dump(items, f, ensure_ascii=False, indent=2)

        st.session_state["video_clip_json_path"] = save_path
        st.session_state["_switch_to_file_mode"] = True
        st.success(f"{tr('Script saved successfully')}: {save_path}")
    except Exception as e:
        st.error(f"{tr('Save script failed')}: {str(e)}")
        logger.error(traceback.format_exc())
