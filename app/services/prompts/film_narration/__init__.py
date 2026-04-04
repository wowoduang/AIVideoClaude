#!/usr/bin/env python
# -*- coding: UTF-8 -*-

"""
film_narration/__init__.py
--------------------------
影视长片解说提示词模块。
注册三轮 LLM 调用的 prompt 到现有 PromptManager 体系。
"""

from .global_understanding import GlobalUnderstandingPrompt
from .segment_analysis import SegmentAnalysisPrompt
from .narration_integration import NarrationIntegrationPrompt
from ..manager import PromptManager


def register_prompts():
    """注册影视长片解说相关的提示词"""
    PromptManager.register_prompt(GlobalUnderstandingPrompt(), is_default=True)
    PromptManager.register_prompt(SegmentAnalysisPrompt(), is_default=True)
    PromptManager.register_prompt(NarrationIntegrationPrompt(), is_default=True)


__all__ = [
    "GlobalUnderstandingPrompt",
    "SegmentAnalysisPrompt",
    "NarrationIntegrationPrompt",
    "register_prompts",
]
