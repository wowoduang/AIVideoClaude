import re
from typing import Dict, List

from app.services.timeline_allocator import apply_timeline_budget
from app.utils import utils


KEYWORD_MAP = [
    ("手机", "手机"),
    ("茶几", "茶几"),
    ("门", "门口"),
    ("哭", "对方情绪失控"),
    ("笑", "气氛突然变得滑稽"),
]


def build_picture_fallback(picture: str, is_last: bool = False) -> str:
    picture = (picture or "").strip()
    if not picture:
        return "这一幕的信息点很关键。" if is_last else "画面里发生了新的变化。"
    for kw, phrase in KEYWORD_MAP:
        if kw in picture:
            if is_last and kw in {"手机", "茶几"}:
                return "结果反转来了，手机其实一直放在家里的茶几上。"
            return f"画面显示，{phrase}成了这一段的关键信息。"
    cleaned = re.sub(r"\s+", " ", picture)
    cleaned = cleaned[:26].rstrip("，。！？,.!? ")
    if is_last:
        return f"最后的反转点出现了：{cleaned}。"
    return f"这一段里，{cleaned}。"


def fill_missing_narrations(script_items: List[Dict]) -> List[Dict]:
    fixed: List[Dict] = []
    total = len(script_items or [])
    for idx, item in enumerate(script_items or [], start=1):
        new_item = dict(item)
        narration = str(new_item.get("narration", "") or "").strip()
        if not narration:
            new_item["narration"] = build_picture_fallback(new_item.get("picture", ""), is_last=idx == total)
        fixed.append(new_item)
    return fixed


def ensure_script_shape(script_items: List[Dict]) -> List[Dict]:
    """
    统一 script_items 格式，兼容新流水线（plot_first_v2）和旧流水线输出。
    新流水线新增字段：segment_id / plot_function / importance
    旧流水线：OST 默认为 2（自动选择）
    """
    result: List[Dict] = []
    for idx, item in enumerate(script_items or [], start=1):
        new_item = dict(item)
        new_item.setdefault("_id", idx)
        new_item.setdefault("picture", "")
        # 新流水线 OST=0/1 已明确；旧流水线默认 2（自动选择）
        ost = new_item.get("OST")
        if ost is None:
            new_item["OST"] = 2
        else:
            new_item["OST"] = int(ost)
        # 新流水线字段：有则保留，无则置默认
        new_item.setdefault("segment_id", "")
        new_item.setdefault("plot_function", "")
        new_item.setdefault("importance", 3)
        # 时间戳：新流水线已生成，旧流水线从 start/end 计算
        if "timestamp" not in new_item or not new_item["timestamp"]:
            start = float(new_item.get("start", 0) or 0)
            end = float(new_item.get("end", start + 1) or (start + 1))
            new_item["timestamp"] = f"{utils.format_time(start)}-{utils.format_time(end)}"
        result.append(new_item)
    return apply_timeline_budget(fill_missing_narrations(result))
