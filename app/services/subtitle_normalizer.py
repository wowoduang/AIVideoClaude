import os
import re
from typing import Dict, List, Optional, Tuple

from loguru import logger


# ── Oral filler words to strip (Chinese) ──────────────────────────
# Common meaningless fillers in spoken Chinese subtitles.
ORAL_FILLERS_ZH = [
    "嗯嗯", "嗯", "啊啊", "啊", "呃", "额", "哦", "噢",
    "那个", "就是说", "就是", "然后呢", "然后",
    "对对对", "对对", "是吧", "你知道吗", "怎么说呢",
]
# Build a regex pattern – match fillers at word boundaries.
# Longer fillers first so "嗯嗯" is tried before "嗯".
_ORAL_FILLER_RE = re.compile(
    "|".join(re.escape(w) for w in sorted(ORAL_FILLERS_ZH, key=len, reverse=True))
)

# ── Speaker label patterns ────────────────────────────────────────
# Matches patterns like "[A]:" or "【说话人1】:" or "Speaker1:" at the
# beginning of a subtitle line.
_SPEAKER_RE = re.compile(
    r"^\s*"
    r"(?:"
    r"\[(?P<s1>[^\]]+)\]"
    r"|【(?P<s2>[^】]+)】"
    r"|(?P<s3>[A-Za-z\u4e00-\u9fff]+\d*)"
    r")"
    r"\s*[:：]\s*"
)


SRT_TIME_RE = re.compile(
    r"(?P<sh>\d{2}):(?P<sm>\d{2}):(?P<ss>\d{2}),(?P<sms>\d{3})\s*-->\s*"
    r"(?P<eh>\d{2}):(?P<em>\d{2}):(?P<es>\d{2}),(?P<ems>\d{3})"
)

# ASS/SSA Dialogue line regex
# Format: Dialogue: Layer,Start,End,Style,Name,MarginL,MarginR,MarginV,Effect,Text
ASS_DIALOGUE_RE = re.compile(
    r"Dialogue:\s*\d+,"
    r"(?P<sh>\d+):(?P<sm>\d{2}):(?P<ss>\d{2})\.(?P<scs>\d{2}),"
    r"(?P<eh>\d+):(?P<em>\d{2}):(?P<es>\d{2})\.(?P<ecs>\d{2}),"
    r"[^,]*,[^,]*,[^,]*,[^,]*,[^,]*,[^,]*,"
    r"(?P<text>.+)"
)

# VTT timestamp regex (supports both . and , as ms separator)
VTT_TIME_RE = re.compile(
    r"(?P<sh>\d{2}):(?P<sm>\d{2}):(?P<ss>\d{2})[.,](?P<sms>\d{3})\s*-->\s*"
    r"(?P<eh>\d{2}):(?P<em>\d{2}):(?P<es>\d{2})[.,](?P<ems>\d{3})"
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


def parse_ass_file(filename: str) -> List[Dict]:
    """Parse ASS/SSA subtitle file into normalized segments.

    Handles standard ASS Dialogue lines. Override/drawing tags
    (e.g. {\\pos(...)}, {\\an8}) are stripped from the text.
    """
    if not filename or not os.path.isfile(filename):
        return []

    with open(filename, "r", encoding="utf-8") as f:
        text = f.read()

    segments: List[Dict] = []
    idx = 0
    for line in text.splitlines():
        match = ASS_DIALOGUE_RE.match(line.strip())
        if not match:
            continue

        start = (
            int(match.group("sh")) * 3600
            + int(match.group("sm")) * 60
            + int(match.group("ss"))
            + int(match.group("scs")) / 100.0
        )
        end = (
            int(match.group("eh")) * 3600
            + int(match.group("em")) * 60
            + int(match.group("es"))
            + int(match.group("ecs")) / 100.0
        )

        raw_text = match.group("text")
        # Strip ASS override tags like {\pos(320,50)}
        raw_text = re.sub(r"\{[^}]*\}", "", raw_text)
        # Replace \N and \n (ASS line breaks) with space
        raw_text = raw_text.replace("\\N", " ").replace("\\n", " ")
        raw_text = raw_text.strip()
        if not raw_text:
            continue

        idx += 1
        segments.append({
            "seg_id": f"sub_{idx:04d}",
            "start": start,
            "end": max(end, start + 0.2),
            "text": raw_text,
            "source": "ass",
        })
    logger.info(f"ASS字幕解析完成: {len(segments)} 段")
    return segments


def parse_vtt_file(filename: str) -> List[Dict]:
    """Parse WebVTT subtitle file into normalized segments.

    Handles standard WebVTT cues. HTML tags (e.g. <b>, <i>) and
    voice tags (e.g. <v Speaker>) are stripped.
    """
    if not filename or not os.path.isfile(filename):
        return []

    with open(filename, "r", encoding="utf-8") as f:
        text = f.read().replace("\r\n", "\n")

    # Remove WEBVTT header and any metadata blocks
    # Split by double newlines to get cue blocks
    blocks = re.split(r"\n\s*\n", text.strip())
    segments: List[Dict] = []
    idx = 0

    for block in blocks:
        lines = [line.strip() for line in block.split("\n") if line.strip()]
        if not lines:
            continue

        # Skip WEBVTT header block
        if lines[0].startswith("WEBVTT"):
            continue
        # Skip NOTE blocks
        if lines[0].startswith("NOTE"):
            continue
        # Skip STYLE blocks
        if lines[0].startswith("STYLE"):
            continue

        # Find the timestamp line
        time_line_idx = -1
        for i, line in enumerate(lines):
            if VTT_TIME_RE.search(line):
                time_line_idx = i
                break

        if time_line_idx < 0:
            continue

        match = VTT_TIME_RE.search(lines[time_line_idx])
        if not match:
            continue

        start = srt_time_to_seconds(
            f"{match.group('sh')}:{match.group('sm')}:{match.group('ss')},{match.group('sms')}"
        )
        end = srt_time_to_seconds(
            f"{match.group('eh')}:{match.group('em')}:{match.group('es')},{match.group('ems')}"
        )

        # Text is everything after the timestamp line
        content_lines = lines[time_line_idx + 1:]
        raw_text = " ".join(content_lines).strip()
        # Strip HTML tags (e.g. <b>, </b>, <i>, <v Speaker>)
        raw_text = re.sub(r"<[^>]+>", "", raw_text)
        raw_text = raw_text.strip()
        if not raw_text:
            continue

        idx += 1
        segments.append({
            "seg_id": f"sub_{idx:04d}",
            "start": start,
            "end": max(end, start + 0.2),
            "text": raw_text,
            "source": "vtt",
        })
    logger.info(f"VTT字幕解析完成: {len(segments)} 段")
    return segments


def parse_subtitle_file(filename: str) -> List[Dict]:
    """Auto-detect subtitle format and parse into normalized segments.

    Supports SRT, ASS/SSA, and WebVTT formats. Detection is based on
    file extension with content-based fallback.
    """
    if not filename or not os.path.isfile(filename):
        return []

    ext = os.path.splitext(filename)[1].lower()
    if ext in (".ass", ".ssa"):
        return parse_ass_file(filename)
    elif ext == ".vtt":
        return parse_vtt_file(filename)
    elif ext == ".srt":
        return parse_srt_file(filename)

    # Fallback: try to detect format from content
    with open(filename, "r", encoding="utf-8") as f:
        head = f.read(512)

    if "WEBVTT" in head:
        return parse_vtt_file(filename)
    if "[Script Info]" in head or "[V4+ Styles]" in head or "[V4 Styles]" in head:
        return parse_ass_file(filename)
    # Default to SRT
    return parse_srt_file(filename)


def _extract_speaker(text: str) -> Tuple[str, str]:
    """Extract speaker label from the beginning of subtitle text.

    Returns (speaker, remaining_text).  If no speaker label is found,
    returns ("", original_text).
    """
    if not text:
        return "", ""
    m = _SPEAKER_RE.match(text)
    if not m:
        return "", text
    speaker = (m.group("s1") or m.group("s2") or m.group("s3") or "").strip()
    remaining = text[m.end():].strip()
    return speaker, remaining


def _strip_oral_fillers(text: str) -> str:
    """Remove oral filler words from text (optional cleaning step)."""
    if not text:
        return ""
    cleaned = _ORAL_FILLER_RE.sub("", text)
    # Collapse multiple spaces introduced by removal
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    return cleaned


def _strip_whisper_tags(text: str) -> str:
    """
    清除 Whisper/SenseVoice 输出的特殊标签，例如：
      <|zh|><|ANGRY|><|BGM|><|withitn|><|EMO_UNKNOWN|>
    这些标签对字幕文字无意义，必须在进入流水线前清除。
    """
    if not text:
        return ""
    text = re.sub(r"<\|[^|>]+\|>", "", text)
    text = re.sub(r"<[^>]{0,20}>", "", text)
    return text.strip()


def _is_noise_only(text: str) -> bool:
    """
    判断文本是否只有噪音（无实质中文/英文内容）。
    有效内容：至少2个汉字，或至少2个3字母以上的英文单词。
    """
    if not text:
        return True
    if len(re.findall(r"[\u4e00-\u9fff]", text)) >= 2:
        return False
    if len(re.findall(r"\b[a-zA-Z]{3,}\b", text)) >= 2:
        return False
    return True


def _clean_text(text: str) -> str:
    if not text:
        return ""
    # 1. 清除 Whisper 特殊标签
    text = _strip_whisper_tags(text)
    # 2. 清除 HTML 标签（如 <i> <b>）
    text = re.sub(r"<[^>]+>", "", text)
    # 3. 合并空白
    text = re.sub(r"\s+", " ", text).strip()
    # 4. 清除首尾标点
    text = re.sub(r"^[，。！？、,.!?\s]+|[，。！？、,.!?\s]+$", "", text)
    return text.strip()


PUNCT_END = set("。！？!?；;…")


def normalize_segments(
    segments: List[Dict],
    max_chars: int = 42,
    max_duration: float = 8.0,
    min_duration: float = 0.35,
    merge_gap: float = 0.45,
    strip_fillers: bool = True,
    detect_speaker: bool = True,
) -> List[Dict]:
    """Normalize subtitle segments.

    Parameters
    ----------
    segments : list
        Raw parsed subtitle segments.
    max_chars : int
        Max characters per segment before forcing a split.
    max_duration : float
        Max duration (seconds) per segment.
    min_duration : float
        Minimum duration; shorter segments are extended.
    merge_gap : float
        Adjacent segments with gap <= this value may be merged.
    strip_fillers : bool
        If True, remove common oral filler words (e.g. 嗯, 那个).
    detect_speaker : bool
        If True, extract speaker labels from text (e.g. [A]: ...).
    """
    cleaned: List[Dict] = []
    for item in segments or []:
        raw_text = item.get("text", "")

        # Speaker extraction (optional)
        speaker = ""
        if detect_speaker:
            speaker, raw_text = _extract_speaker(raw_text)

        text = _clean_text(raw_text)

        # Oral filler removal (optional)
        if strip_fillers and text:
            text = _strip_oral_fillers(text)
            text = _clean_text(text)

        # 跳过：空文本 或 纯 Whisper 噪音标签段（无实质内容）
        if not text or _is_noise_only(text):
            continue

        start = float(item.get("start", 0) or 0)
        end = float(item.get("end", start + 0.5) or (start + 0.5))
        if end <= start:
            end = start + 0.5

        seg_dict: Dict = {
            "seg_id": item.get("seg_id") or f"sub_{len(cleaned)+1:04d}",
            "start": start,
            "end": end,
            "text": text,
            "source": item.get("source", "subtitle"),
        }
        if speaker:
            seg_dict["speaker"] = speaker
        # Propagate confidence from ASR results if present
        if "confidence" in item:
            seg_dict["confidence"] = float(item["confidence"])
        cleaned.append(seg_dict)

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


# ─────────────────────────────────────────────────────────────
# 字幕时间轴偏移校准
# 处理 Whisper 转录字幕与视频画面存在系统性偏移的问题
# ─────────────────────────────────────────────────────────────

def detect_subtitle_offset(
    segments: List[Dict],
    reference_dialogues: List[str] = None,
    max_offset_sec: float = 5.0,
) -> float:
    """
    检测字幕时间轴的系统性偏移量（秒）。

    方法：找到字幕中首个有实质对白的段落，
    与参考对白（如果提供）做时间比对。
    如果没有参考对白，返回 0.0（无法自动校准）。

    Parameters
    ----------
    segments : 已清洗的字幕段列表
    reference_dialogues : 已知对白的精确时间点列表，格式 [(time_sec, text), ...]
    max_offset_sec : 最大允许偏移量，超过此值认为校准失败

    Returns
    -------
    offset : 偏移秒数，正数表示字幕偏早，负数表示字幕偏晚
    """
    if not segments or not reference_dialogues:
        return 0.0

    # 找字幕中第一个有实质内容的段落
    first_sub = None
    for seg in segments:
        text = (seg.get("text") or "").strip()
        if len(text) >= 4:  # 至少4个字才算实质对白
            first_sub = seg
            break

    if not first_sub:
        return 0.0

    sub_time = float(first_sub.get("start", 0))
    sub_text = first_sub.get("text", "")

    # 在参考对白里找最接近的匹配
    best_offset = 0.0
    best_match_score = 0

    for ref_time, ref_text in reference_dialogues:
        # 简单文字重叠率
        sub_chars = set(sub_text)
        ref_chars = set(ref_text)
        if not sub_chars or not ref_chars:
            continue
        overlap = len(sub_chars & ref_chars) / max(len(sub_chars | ref_chars), 1)
        if overlap > best_match_score:
            best_match_score = overlap
            best_offset = sub_time - ref_time

    if abs(best_offset) > max_offset_sec:
        logger.warning(f"检测到字幕偏移量 {best_offset:.2f}s 超过最大值 {max_offset_sec}s，跳过校准")
        return 0.0

    logger.info(f"检测到字幕偏移量: {best_offset:.2f}s（字幕{'偏早' if best_offset > 0 else '偏晚'}）")
    return best_offset


def apply_subtitle_offset(segments: List[Dict], offset_sec: float) -> List[Dict]:
    """
    对所有字幕段应用时间偏移校准。

    Parameters
    ----------
    segments : 字幕段列表
    offset_sec : detect_subtitle_offset() 返回的偏移量

    Returns
    -------
    校准后的字幕段列表（新对象，不修改原始数据）
    """
    if not offset_sec or abs(offset_sec) < 0.01:
        return segments

    corrected = []
    for seg in segments:
        new_seg = dict(seg)
        new_start = max(0.0, float(seg.get("start", 0)) - offset_sec)
        new_end = max(new_start + 0.1, float(seg.get("end", 0)) - offset_sec)
        new_seg["start"] = round(new_start, 3)
        new_seg["end"] = round(new_end, 3)
        corrected.append(new_seg)

    logger.info(f"字幕偏移校准完成: 偏移 {offset_sec:+.2f}s，共 {len(corrected)} 段")
    return corrected


def auto_calibrate_segments(
    segments: List[Dict],
    reference_dialogues: List[str] = None,
) -> List[Dict]:
    """
    一键自动校准：检测偏移 + 应用校准。
    如果没有参考对白，直接返回原始数据。
    """
    offset = detect_subtitle_offset(segments, reference_dialogues)
    if abs(offset) < 0.05:
        return segments
    return apply_subtitle_offset(segments, offset)


# ─────────────────────────────────────────────────────────────
# 字幕时间空洞处理与修复
# 处理 Whisper 丢弃无声段导致的大段时间空洞
# ─────────────────────────────────────────────────────────────

def analyze_subtitle_gaps(segments: List[Dict], gap_threshold: float = 5.0) -> List[Dict]:
    """
    分析字幕时间轴中的空洞。

    Returns
    -------
    gaps : List[Dict]
        每个空洞的 start/end/duration/type 信息。
        type: "silence"（无声）| "missing"（可能有对白但未识别）
    """
    gaps = []
    if not segments:
        return gaps

    # 片头空洞（视频开始到第一条字幕）
    if segments[0]["start"] > gap_threshold:
        gaps.append({
            "start": 0.0,
            "end": segments[0]["start"],
            "duration": segments[0]["start"],
            "type": "head_silence",
            "before_text": "",
            "after_text": segments[0]["text"],
        })

    # 中间空洞
    for i in range(1, len(segments)):
        gap = segments[i]["start"] - segments[i - 1]["end"]
        if gap > gap_threshold:
            gaps.append({
                "start": segments[i - 1]["end"],
                "end": segments[i]["start"],
                "duration": round(gap, 3),
                # 超过30秒的空洞很可能是场景切换（无声段）
                # 5-30秒的空洞可能是没识别到的对白
                "type": "scene_break" if gap > 30 else "possible_missing",
                "before_text": segments[i - 1]["text"],
                "after_text": segments[i]["text"],
            })

    return gaps


def insert_placeholder_segments(
    segments: List[Dict],
    gap_threshold: float = 10.0,
    placeholder_text: str = "",
) -> List[Dict]:
    """
    在大时间空洞中插入占位段，保持时间轴连续性。
    这样 plot_chunker 可以感知到"这里有段时间是无声/未识别的"。

    占位段的 text 为空，will be filtered by _is_noise_only，
    但其 start/end 会保留，供场景检测和剧情分段使用。

    Parameters
    ----------
    gap_threshold : 超过多少秒才插入占位段（默认10秒）
    """
    if not segments:
        return segments

    result = []
    prev_end = 0.0

    for seg in segments:
        gap = seg["start"] - prev_end
        if gap > gap_threshold:
            # 插入一个"无声段"占位符
            placeholder = {
                "seg_id": f"gap_{prev_end:.0f}",
                "start": round(prev_end, 3),
                "end": round(seg["start"], 3),
                "text": "",  # 空文本，标记为无内容段
                "source": "gap_placeholder",
                "is_gap": True,
                "gap_duration": round(gap, 3),
            }
            result.append(placeholder)
        result.append(seg)
        prev_end = seg["end"]

    return result


def build_full_timeline(
    segments: List[Dict],
    video_duration: float = 0.0,
    gap_threshold: float = 10.0,
) -> List[Dict]:
    """
    构建完整的时间轴，包含字幕段和无声占位段。
    这是 plot_chunker 理想的输入格式。

    Parameters
    ----------
    segments : 已清洗的字幕段
    video_duration : 视频总时长（秒），用于在末尾补全时间轴
    gap_threshold : 超过多少秒的空洞才插入占位段
    """
    with_placeholders = insert_placeholder_segments(segments, gap_threshold)

    # 补全末尾
    if video_duration > 0 and with_placeholders:
        last_end = with_placeholders[-1]["end"]
        if video_duration - last_end > gap_threshold:
            with_placeholders.append({
                "seg_id": f"gap_{last_end:.0f}",
                "start": round(last_end, 3),
                "end": round(video_duration, 3),
                "text": "",
                "source": "gap_placeholder",
                "is_gap": True,
                "gap_duration": round(video_duration - last_end, 3),
            })

    return with_placeholders


def repair_subtitle_timing(
    segments: List[Dict],
    min_gap_between: float = 0.05,
) -> List[Dict]:
    """
    修复字幕时间轴的微小问题：
    1. 相邻字幕时间重叠 → 截断前一条的结束时间
    2. 相邻字幕间隔过小（< min_gap_between）→ 微调
    3. 同一条字幕 end <= start → 强制设置最小时长

    这是针对"字幕时间和文字对不上"的直接修复。
    """
    if not segments:
        return segments

    repaired = [dict(segments[0])]
    for seg in segments[1:]:
        prev = repaired[-1]
        new_seg = dict(seg)

        # 修复重叠
        if new_seg["start"] < prev["end"]:
            # 如果重叠时间很短（< 0.5s），截断上一条
            if prev["end"] - new_seg["start"] < 0.5:
                prev["end"] = new_seg["start"] - 0.02
            # 如果重叠很严重，说明时间轴本身有问题，保持原样并记录
            else:
                logger.warning(
                    f"字幕时间严重重叠: [{prev['start']:.2f}-{prev['end']:.2f}] "
                    f"与 [{new_seg['start']:.2f}-{new_seg['end']:.2f}]"
                )

        # 修复 end <= start
        if new_seg["end"] <= new_seg["start"]:
            new_seg["end"] = new_seg["start"] + 1.0

        # 修复间隔过小
        if 0 < new_seg["start"] - prev["end"] < min_gap_between:
            new_seg["start"] = prev["end"] + min_gap_between

        repaired.append(new_seg)

    return repaired


def get_subtitle_stats(segments: List[Dict]) -> Dict:
    """
    返回字幕质量统计信息，用于 UI 展示。
    """
    if not segments:
        return {"total": 0, "coverage": 0.0, "gaps": [], "avg_duration": 0.0}

    total_text_duration = sum(s["end"] - s["start"] for s in segments if not s.get("is_gap"))
    total_span = segments[-1]["end"] - segments[0]["start"]
    gaps = analyze_subtitle_gaps([s for s in segments if not s.get("is_gap")])

    return {
        "total": len([s for s in segments if not s.get("is_gap")]),
        "total_with_gaps": len(segments),
        "coverage": round(total_text_duration / max(total_span, 1) * 100, 1),
        "total_text_duration": round(total_text_duration, 1),
        "total_span": round(total_span, 1),
        "gaps": gaps,
        "gap_count": len(gaps),
        "largest_gap": round(max((g["duration"] for g in gaps), default=0), 1),
        "avg_duration": round(total_text_duration / max(len(segments), 1), 1),
    }
