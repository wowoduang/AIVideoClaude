import os
import re
from typing import Dict, List, Optional

from loguru import logger


SRT_TIME_RE = re.compile(
    r"(?P<sh>\d{2}):(?P<sm>\d{2}):(?P<ss>\d{2}),(?P<sms>\d{3})\s*-->\s*"
    r"(?P<eh>\d{2}):(?P<em>\d{2}):(?P<es>\d{2}),(?P<ems>\d{3})"
)


def srt_time_to_seconds(value: str) -> float:
    h, m, rest = value.split(":")
    s, ms = rest.split(",")
    return int(h) * 3600 + int(m) * 60 + int(s) + int(ms) / 1000.0


def seconds_to_srt_time(seconds: float) -> str:
    seconds = max(0.0, float(seconds))
    h = int(seconds // 3600)
    seconds -= h * 3600
    m = int(seconds // 60)
    seconds -= m * 60
    s = int(seconds)
    ms = int(round((seconds - s) * 1000))
    if ms == 1000:
        s += 1
        ms = 0
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def parse_srt_file(filename: str) -> List[Dict]:
    if not filename or not os.path.isfile(filename):
        return []

    with open(filename, "r", encoding="utf-8") as f:
        text = f.read().replace("\r\n", "\n")

    blocks = re.split(r"\n\s*\n", text.strip())
    segments: List[Dict] = []
    for idx, block in enumerate(blocks, start=1):
        lines = [line.strip() for line in block.split("\n") if line.strip()]
        if len(lines) < 2:
            continue

        time_line = lines[1] if lines[0].isdigit() else lines[0]
        match = SRT_TIME_RE.search(time_line)
        if not match:
            continue

        start = srt_time_to_seconds(
            f"{match.group('sh')}:{match.group('sm')}:{match.group('ss')},{match.group('sms')}"
        )
        end = srt_time_to_seconds(
            f"{match.group('eh')}:{match.group('em')}:{match.group('es')},{match.group('ems')}"
        )
        content_start = 2 if lines[0].isdigit() else 1
        text_value = " ".join(lines[content_start:]).strip()
        segments.append({
            "seg_id": f"sub_{idx:04d}",
            "start": start,
            "end": max(end, start + 0.2),
            "text": text_value,
            "source": "srt",
        })
    return segments


def _clean_text(text: str) -> str:
    if not text:
        return ""
    text = re.sub(r"\s+", " ", text).strip()
    text = re.sub(r"^[，。！？、,.!?]+|[，。！？、,.!?]+$", "", text)
    return text.strip()


PUNCT_END = set("。！？!?；;…")


def normalize_segments(
    segments: List[Dict],
    max_chars: int = 42,
    max_duration: float = 8.0,
    min_duration: float = 0.35,
    merge_gap: float = 0.45,
) -> List[Dict]:
    cleaned: List[Dict] = []
    for item in segments or []:
        text = _clean_text(item.get("text", ""))
        if not text:
            continue
        start = float(item.get("start", 0) or 0)
        end = float(item.get("end", start + 0.5) or (start + 0.5))
        if end <= start:
            end = start + 0.5
        cleaned.append({
            "seg_id": item.get("seg_id") or f"sub_{len(cleaned)+1:04d}",
            "start": start,
            "end": end,
            "text": text,
            "source": item.get("source", "subtitle"),
        })

    if not cleaned:
        return []

    merged: List[Dict] = []
    current = cleaned[0].copy()
    for item in cleaned[1:]:
        gap = item["start"] - current["end"]
        current_len = len(current["text"])
        current_duration = current["end"] - current["start"]
        should_merge = (
            gap <= merge_gap
            and current_len < max_chars
            and current_duration < max_duration
            and (
                len(item["text"]) < max_chars // 2
                or current["text"][-1] not in PUNCT_END
            )
        )
        if should_merge:
            current["text"] = f"{current['text']} {item['text']}".strip()
            current["end"] = max(current["end"], item["end"])
        else:
            merged.append(current)
            current = item.copy()
    merged.append(current)

    normalized: List[Dict] = []
    for idx, item in enumerate(merged, start=1):
        duration = item["end"] - item["start"]
        if duration < min_duration:
            item["end"] = item["start"] + min_duration
        item["text"] = _clean_text(item["text"])
        item["seg_id"] = f"sub_{idx:04d}"
        normalized.append(item)
    logger.info(f"字幕标准化完成: {len(segments or [])} -> {len(normalized)} 段")
    return normalized


def dump_segments_to_srt(segments: List[Dict], output_file: str) -> Optional[str]:
    if not output_file:
        return None
    os.makedirs(os.path.dirname(output_file), exist_ok=True)
    with open(output_file, "w", encoding="utf-8") as f:
        for idx, seg in enumerate(segments or [], start=1):
            f.write(f"{idx}\n")
            f.write(f"{seconds_to_srt_time(seg['start'])} --> {seconds_to_srt_time(seg['end'])}\n")
            f.write(f"{seg.get('text','').strip()}\n\n")
    return output_file
