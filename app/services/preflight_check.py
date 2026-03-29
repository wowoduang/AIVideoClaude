from typing import Dict, List


class PreflightError(ValueError):
    pass


REQUIRED_SCRIPT_KEYS = ["_id", "timestamp", "picture", "narration", "OST"]


def validate_script_items(script_list: List[Dict]):
    if not script_list:
        raise PreflightError("脚本数组不能为空")
    for idx, item in enumerate(script_list, start=1):
        for key in REQUIRED_SCRIPT_KEYS:
            if key not in item:
                raise PreflightError(f"第 {idx} 个片段缺少字段: {key}")
        if not str(item.get("narration", "")).strip():
            raise PreflightError(f"第 {idx} 个片段 narration 为空")


def validate_tts_results(script_list: List[Dict], tts_results: List[Dict]):
    required_ids = {item["_id"] for item in script_list if item.get("OST") in [0, 2]}
    result_ids = {item.get("_id") for item in (tts_results or []) if item.get("audio_file")}
    missing = sorted([rid for rid in required_ids if rid not in result_ids])
    if missing:
        raise PreflightError(
            "缺少 TTS 结果，无法继续统一裁剪。"
            f" 缺失片段ID: {missing}. 请检查语音合成是否成功，或将相关片段改为不依赖TTS的 OST 模式。"
        )
