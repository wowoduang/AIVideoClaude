from __future__ import annotations

import streamlit as st


MODE_LABELS = {
    "fast": "快速",
    "balanced": "标准",
    "quality": "高质量",
}

VISUAL_LABELS = {
    "off": "关闭",
    "auto": "自动",
    "boost": "强化",
}

STYLE_LABELS = {
    "general": "通用",
    "short_drama": "短剧",
    "documentary": "纪实",
}

AUDIO_STRATEGY_LABELS = {
    "keep": "保留",
    "duck": "降低",
    "mute": "关闭",
}


def _safe_index(options, current, default):
    if current in options:
        return options.index(current)
    return options.index(default)


def render_subtitle_first_mode_panel(tr):
    with st.container(border=True):
        st.markdown("### " + tr("字幕优先生成设置"))

        generation_options = ["fast", "balanced", "quality"]
        visual_options = ["off", "auto", "boost"]
        style_options = ["general", "short_drama", "documentary"]
        audio_options = ["keep", "duck", "mute"]

        st.selectbox(
            tr("生成模式"),
            options=generation_options,
            index=_safe_index(
                generation_options,
                st.session_state.get("generation_mode", "balanced"),
                "balanced",
            ),
            format_func=lambda x: MODE_LABELS.get(x, x),
            help=tr("快速模式更省成本，标准模式更均衡，高质量模式会做更细的事件切分与自动视觉补充"),
            key="generation_mode",
        )

        st.selectbox(
            tr("视觉补充"),
            options=visual_options,
            index=_safe_index(
                visual_options,
                st.session_state.get("visual_mode", "auto"),
                "auto",
            ),
            format_func=lambda x: VISUAL_LABELS.get(x, x),
            help=tr("关闭=纯字幕；自动=只在必要段补帧；强化=更多段启用视觉补充"),
            key="visual_mode",
        )

        st.selectbox(
            tr("解说风格"),
            options=style_options,
            index=_safe_index(
                style_options,
                st.session_state.get("narration_style", "short_drama"),
                "short_drama",
            ),
            format_func=lambda x: STYLE_LABELS.get(x, x),
            help=tr("影响脚本的语气和表达方式，不影响事实内容"),
            key="narration_style",
        )

        st.selectbox(
            tr("原声策略"),
            options=audio_options,
            index=_safe_index(
                audio_options,
                st.session_state.get("audio_strategy", "duck"),
                "duck",
            ),
            format_func=lambda x: AUDIO_STRATEGY_LABELS.get(x, x),
            help=tr("保留=尽量保留原声；降低=压低原声；关闭=只保留解说和背景音"),
            key="audio_strategy",
        )

        st.caption(
            tr("说明：已移除旧的帧间隔、批处理大小等旧控件，字幕优先模式统一使用内部预设参数。")
        )
