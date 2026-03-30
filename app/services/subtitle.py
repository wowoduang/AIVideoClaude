import os
import os.path
import re
import sys
import traceback
from typing import Optional, List, Tuple

try:
    from faster_whisper import WhisperModel
except Exception:
    WhisperModel = None

from timeit import default_timer as timer
from loguru import logger
import google.generativeai as genai
from moviepy import VideoFileClip

from app.config import config
from app.utils import utils


model_size = config.whisper.get("model_size", "faster-whisper-large-v3")
device = config.whisper.get("device", "cpu")
compute_type = config.whisper.get("compute_type", "int8")
model = None
_model_identity = None


def _base_dirs() -> List[str]:
    """Return candidate base directories for locating app/models.

    In a normal Python environment ``utils.root_dir()`` is sufficient.
    When the application is compiled / packaged (e.g. PyInstaller),
    ``__file__`` may resolve to a temporary extraction directory so we
    also try the current working directory and the directory that holds
    the running executable.
    """
    seen: set = set()
    bases: List[str] = []
    for raw in (
        utils.root_dir(),
        os.getcwd(),
        os.path.dirname(os.path.abspath(sys.executable)),
    ):
        d = os.path.abspath(raw)
        if d not in seen:
            seen.add(d)
            bases.append(d)
    return bases


def _candidate_model_dirs(requested_model_size: str) -> List[str]:
    requested = (requested_model_size or "").strip()
    candidates = []
    if requested:
        candidates.append(requested)
    for fallback in ["faster-whisper-large-v3", "faster-whisper-large-v2", "large-v3", "large-v2"]:
        if fallback not in candidates:
            candidates.append(fallback)

    dirs: List[str] = []
    seen: set = set()
    for base in _base_dirs():
        for name in candidates:
            p = os.path.abspath(os.path.join(base, "app", "models", name))
            if p not in seen:
                seen.add(p)
                dirs.append(p)
    return dirs


_MODEL_FILES = ("model.bin", "model.safetensors")


def _is_valid_model_dir(path: str) -> bool:
    """Check if directory contains a CTranslate2 model file (model.bin or model.safetensors)."""
    return any(os.path.isfile(os.path.join(path, f)) for f in _MODEL_FILES)


def _resolve_model_path() -> Tuple[Optional[str], List[str]]:
    searched = _candidate_model_dirs(config.whisper.get("model_size", model_size))
    for path in searched:
        if os.path.isdir(path):
            if _is_valid_model_dir(path):
                return path, searched
            else:
                try:
                    contents = os.listdir(path)
                except OSError:
                    contents = []
                logger.warning(
                    f"模型目录存在但未找到模型文件 ({', '.join(_MODEL_FILES)}): {path}\n"
                    f"目录内容: {contents}"
                )
    return None, searched


def _load_model() -> bool:
    global model, device, compute_type, _model_identity
    if WhisperModel is None:
        logger.error("未安装 faster-whisper，请先安装依赖后再使用自动字幕生成")
        return False

    model_path, searched = _resolve_model_path()
    if not model_path:
        searched_text = "\n".join([f"- {p}" for p in searched])
        bases_text = "\n".join([f"- {d}" for d in _base_dirs()])
        logger.error(
            "请先下载 whisper 模型\n\n"
            "********************************************\n"
            "推荐下载：\n"
            "https://huggingface.co/guillaumekln/faster-whisper-large-v3\n"
            "或：\n"
            "https://huggingface.co/guillaumekln/faster-whisper-large-v2\n\n"
            "存放路径示例：app/models/faster-whisper-large-v3\n\n"
            f"root_dir(): {utils.root_dir()}\n"
            f"cwd: {os.getcwd()}\n"
            f"executable: {sys.executable}\n\n"
            "搜索基础目录：\n"
            f"{bases_text}\n\n"
            "已搜索目录：\n"
            f"{searched_text}\n"
            "********************************************\n"
        )
        return False

    if model is not None and _model_identity == model_path:
        return True

    use_cuda = False
    try:
        def check_cuda_available():
            try:
                import torch
                return torch.cuda.is_available()
            except Exception as e:
                logger.warning(f"检查CUDA可用性时出错: {e}")
                return False

        use_cuda = check_cuda_available()
        if use_cuda:
            logger.info(f"尝试使用 CUDA 加载模型: {model_path}")
            try:
                model = WhisperModel(
                    model_size_or_path=model_path,
                    device="cuda",
                    compute_type="float16",
                    local_files_only=True,
                )
                device = "cuda"
                compute_type = "float16"
                _model_identity = model_path
                logger.info("成功使用 CUDA 加载模型")
                return True
            except Exception as e:
                logger.warning(f"CUDA 加载失败，回退到 CPU: {e}")
                use_cuda = False
    except Exception as e:
        logger.warning(f"CUDA检查过程出错，默认使用CPU: {e}")
        use_cuda = False

    device = "cpu"
    compute_type = "int8"
    logger.info(f"使用 CPU 加载模型: {model_path}")
    model = WhisperModel(
        model_size_or_path=model_path,
        device=device,
        compute_type=compute_type,
        local_files_only=True,
    )
    _model_identity = model_path
    logger.info(f"模型加载完成，使用设备: {device}, 计算类型: {compute_type}")
    return True


def create(audio_file, subtitle_file: str = ""):
    global model
    if not _load_model():
        return None

    logger.info(f"start, output file: {subtitle_file}")
    if not subtitle_file:
        subtitle_file = f"{audio_file}.srt"

    segments, info = model.transcribe(
        audio_file,
        beam_size=5,
        word_timestamps=True,
        vad_filter=True,
        vad_parameters=dict(min_silence_duration_ms=500),
        initial_prompt="以下是普通话的句子",
    )

    logger.info(f"检测到的语言: '{info.language}', probability: {info.language_probability:.2f}")

    start = timer()
    subtitles = []

    def recognized(seg_text, seg_start, seg_end):
        seg_text = seg_text.strip()
        if not seg_text:
            return
        subtitles.append({"msg": seg_text, "start_time": seg_start, "end_time": seg_end})

    for segment in segments:
        words_idx = 0
        words_len = len(segment.words)
        seg_start = 0
        seg_end = 0
        seg_text = ""

        if segment.words:
            is_segmented = False
            for word in segment.words:
                if not is_segmented:
                    seg_start = word.start
                    is_segmented = True
                seg_end = word.end
                seg_text += word.word
                if utils.str_contains_punctuation(word.word):
                    seg_text = seg_text[:-1]
                    if seg_text:
                        recognized(seg_text, seg_start, seg_end)
                    is_segmented = False
                    seg_text = ""
                if words_idx == 0 and segment.start < word.start:
                    seg_start = word.start
                if words_idx == (words_len - 1) and segment.end > word.end:
                    seg_end = word.end
                words_idx += 1

        if seg_text:
            recognized(seg_text, seg_start, seg_end)

    end = timer()
    logger.info(f"complete, elapsed: {end - start:.2f} s")

    idx = 1
    lines = []
    for subtitle in subtitles:
        text = subtitle.get("msg")
        if text:
            lines.append(utils.text_to_srt(idx, text, subtitle.get("start_time"), subtitle.get("end_time")))
            idx += 1

    sub = "\n".join(lines) + "\n"
    with open(subtitle_file, "w", encoding="utf-8") as f:
        f.write(sub)
    logger.info(f"subtitle file created: {subtitle_file}")
    return subtitle_file if os.path.exists(subtitle_file) else None


def file_to_subtitles(filename):
    if not filename or not os.path.isfile(filename):
        return []
    times_texts = []
    current_times = None
    current_text = ""
    index = 0
    with open(filename, "r", encoding="utf-8") as f:
        for line in f:
            times = re.findall("([0-9]*:[0-9]*:[0-9]*,[0-9]*)", line)
            if times:
                current_times = line
            elif line.strip() == "" and current_times:
                index += 1
                times_texts.append((index, current_times.strip(), current_text.strip()))
                current_times, current_text = None, ""
            elif current_times:
                current_text += line
    return times_texts


def levenshtein_distance(s1, s2):
    if len(s1) < len(s2):
        return levenshtein_distance(s2, s1)
    if len(s2) == 0:
        return len(s1)
    previous_row = range(len(s2) + 1)
    for i, c1 in enumerate(s1):
        current_row = [i + 1]
        for j, c2 in enumerate(s2):
            insertions = previous_row[j + 1] + 1
            deletions = current_row[j] + 1
            substitutions = previous_row[j] + (c1 != c2)
            current_row.append(min(insertions, deletions, substitutions))
        previous_row = current_row
    return previous_row[-1]


def similarity(a, b):
    distance = levenshtein_distance(a.lower(), b.lower())
    max_length = max(len(a), len(b))
    return 1 - (distance / max_length)


def correct(subtitle_file, video_script):
    subtitle_items = file_to_subtitles(subtitle_file)
    script_lines = utils.split_string_by_punctuations(video_script)
    return subtitle_items, script_lines


def create_with_gemini(audio_file: str, subtitle_file: str, api_key: str):
    genai.configure(api_key=api_key)
    gemini_model = genai.GenerativeModel(model_name="gemini-1.5-flash")
    prompt = "生成这段语音的转录文本。请以SRT格式输出，包含时间戳。"
    try:
        with open(audio_file, "rb") as f:
            audio_data = f.read()
        response = gemini_model.generate_content([prompt, audio_data])
        transcript = response.text
        if not subtitle_file:
            subtitle_file = f"{audio_file}.srt"
        with open(subtitle_file, "w", encoding="utf-8") as f:
            f.write(transcript)
        logger.info(f"Gemini生成的字幕文件已保存: {subtitle_file}")
        return subtitle_file
    except Exception as e:
        logger.error(f"使用Gemini处理音频时出错: {e}")
        return None


def extract_audio_and_create_subtitle(video_file: str, subtitle_file: str = "") -> Optional[str]:
    """从视频文件中提取音频并生成字幕文件。"""
    audio_file = ""
    video = None
    try:
        video_name = os.path.splitext(os.path.basename(video_file))[0]
        audio_dir = utils.temp_dir("audio_extract")
        os.makedirs(audio_dir, exist_ok=True)
        audio_file = os.path.join(audio_dir, f"{video_name}_audio.wav")

        if not subtitle_file:
            subtitle_dir = utils.temp_dir("subtitles")
            os.makedirs(subtitle_dir, exist_ok=True)
            subtitle_file = os.path.join(subtitle_dir, f"{video_name}.srt")

        logger.info(f"开始从视频提取音频: {video_file}")
        video = VideoFileClip(video_file)
        if video.audio is None:
            logger.error("视频文件不包含音频轨道，无法自动生成字幕")
            return None

        logger.info(f"正在提取音频到: {audio_file}")
        video.audio.write_audiofile(audio_file, codec="pcm_s16le", logger=None)
        logger.info("音频提取完成，开始生成字幕")

        result = create(audio_file, subtitle_file)
        if result and os.path.exists(result):
            logger.info(f"字幕生成完成: {result}")
            return result
        logger.error("字幕生成失败，未输出字幕文件")
        return None
    except Exception as e:
        logger.error(f"处理视频文件时出错: {str(e)}")
        logger.error(traceback.format_exc())
        return None
    finally:
        try:
            if video is not None:
                video.close()
        except Exception:
            pass
        if audio_file and os.path.exists(audio_file):
            try:
                os.remove(audio_file)
                logger.info("已清理临时音频文件")
            except Exception as e:
                logger.warning(f"清理临时音频文件失败: {e}")
