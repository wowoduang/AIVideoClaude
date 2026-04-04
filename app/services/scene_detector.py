"""
scene_detector.py
-----------------
PySceneDetect封装 + A/B/C/D级边界分级 + 运动保护误检过滤。

A级：强候选边界（硬切+长静默+主体变化）
B级：中候选边界（画面差异明显但字幕连续）
C级：弱候选边界（普通镜头切换）
D级：无效候选边界（相机运动误检，直接丢弃）
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import List, Optional

from loguru import logger

try:
    from scenedetect import open_video, SceneManager
    from scenedetect.detectors import AdaptiveDetector, ContentDetector
    _SCENEDETECT_AVAILABLE = True
except ImportError:
    _SCENEDETECT_AVAILABLE = False
    logger.warning("scenedetect 未安装，场景检测将不可用。pip install scenedetect")

try:
    import cv2
    _CV2_AVAILABLE = True
except ImportError:
    _CV2_AVAILABLE = False


# ── 配置 ──────────────────────────────────────────────────────

@dataclass
class SceneDetectorConfig:
    detector: str = "adaptive"          # adaptive | content
    content_threshold: float = 27.0
    min_scene_len_frames: int = 15      # 约0.5秒@30fps
    enable_fade_detection: bool = True
    enable_motion_guard: bool = True
    motion_guard_threshold: float = 0.85  # 运动强度超过此值视为误检
    merge_shorter_than: float = 1.5     # 合并小于1.5秒的碎片
    drop_black_frames: bool = True
    max_scene_duration: float = 120.0   # 超过此时长强制插入候选切点
    # 按片型调参
    film_type: str = "auto"             # auto | action | dialogue | documentary
    boundary_merge_window_sec: float = 2.0  # 字幕与视频边界对齐窗口


ACTION_CONFIG = SceneDetectorConfig(
    content_threshold=35.0,  # 提高阈值减少误检
    min_scene_len_frames=20,
    motion_guard_threshold=0.90,
)

DIALOGUE_CONFIG = SceneDetectorConfig(
    content_threshold=20.0,  # 降低阈值更敏感
    min_scene_len_frames=10,
)

DOCUMENTARY_CONFIG = SceneDetectorConfig(
    content_threshold=25.0,
    min_scene_len_frames=18,
)


# ── 输出结构 ──────────────────────────────────────────────────

@dataclass
class SceneBoundary:
    time: float                     # 秒
    grade: str                      # A|B|C|D
    score: float                    # 0-1
    reason: str
    motion_score: float = 0.0       # 画面运动强度
    is_fade: bool = False


@dataclass
class DetectedScene:
    scene_id: str
    start: float
    end: float
    has_dialogue: bool = False
    dialogue_density: float = 0.0
    motion_score: float = 0.0
    boundary_grade: str = "C"       # 本场景起始边界的等级

    @property
    def duration(self) -> float:
        return self.end - self.start


# ── 主检测器 ──────────────────────────────────────────────────

class SceneDetector:

    def __init__(self, config: Optional[SceneDetectorConfig] = None):
        self.config = config or SceneDetectorConfig()

    def detect(self, video_path: str) -> List[DetectedScene]:
        """
        主入口：对视频做场景检测，返回带边界等级的场景列表。
        """
        if not os.path.isfile(video_path):
            raise FileNotFoundError(f"视频文件不存在: {video_path}")

        if not _SCENEDETECT_AVAILABLE:
            logger.warning("scenedetect不可用，返回空场景列表")
            return self._fallback_fixed_interval(video_path)

        logger.info("开始场景检测: {}", video_path)
        raw_boundaries = self._run_scenedetect(video_path)
        boundaries = self._classify_boundaries(raw_boundaries, video_path)
        boundaries = self._filter_d_grade(boundaries)
        scenes = self._boundaries_to_scenes(boundaries, video_path)
        scenes = self._merge_short_scenes(scenes)
        scenes = self._split_long_scenes(scenes)
        logger.info("场景检测完成: {} 个场景", len(scenes))
        return scenes

    def _run_scenedetect(self, video_path: str) -> List[dict]:
        """调用 PySceneDetect 获取原始切点"""
        try:
            video = open_video(video_path)
            scene_manager = SceneManager()

            if self.config.detector == "adaptive":
                scene_manager.add_detector(AdaptiveDetector(
                    adaptive_threshold=self.config.content_threshold / 10,
                    min_scene_len=self.config.min_scene_len_frames,
                ))
            else:
                scene_manager.add_detector(ContentDetector(
                    threshold=self.config.content_threshold,
                    min_scene_len=self.config.min_scene_len_frames,
                ))

            scene_manager.detect_scenes(video, show_progress=False)
            scene_list = scene_manager.get_scene_list()

            boundaries = []
            for i, (start, end) in enumerate(scene_list):
                boundaries.append({
                    "time": start.get_seconds(),
                    "end": end.get_seconds(),
                    "index": i,
                })
            return boundaries
        except Exception as e:
            logger.error("PySceneDetect 执行失败: {}", e)
            return []

    def _classify_boundaries(self, raw: List[dict], video_path: str) -> List[SceneBoundary]:
        """对每个切点打A/B/C/D等级"""
        boundaries = []
        fps = self._get_fps(video_path)

        for item in raw:
            t = item["time"]
            motion = self._sample_motion_score(video_path, t, fps) if _CV2_AVAILABLE else 0.5

            # D级：运动保护——相机快速运动导致的假切点
            if self.config.enable_motion_guard and motion > self.config.motion_guard_threshold:
                boundaries.append(SceneBoundary(
                    time=t, grade="D", score=0.1,
                    reason="相机快速运动误检", motion_score=motion
                ))
                continue

            duration_to_next = item.get("end", t + 10) - t

            # A级：强候选
            if duration_to_next > 8 and motion < 0.5:
                grade, score, reason = "A", 0.9, "硬切+场景持续时间长"
            elif duration_to_next > 4:
                grade, score, reason = "B", 0.65, "画面差异明显"
            else:
                grade, score, reason = "C", 0.4, "普通镜头切换"

            boundaries.append(SceneBoundary(
                time=t, grade=grade, score=score,
                reason=reason, motion_score=motion
            ))

        return boundaries

    def _filter_d_grade(self, boundaries: List[SceneBoundary]) -> List[SceneBoundary]:
        """过滤D级无效边界"""
        valid = [b for b in boundaries if b.grade != "D"]
        dropped = len(boundaries) - len(valid)
        if dropped:
            logger.info("过滤D级误检边界: {} 个", dropped)
        return valid

    def _boundaries_to_scenes(self, boundaries: List[SceneBoundary], video_path: str) -> List[DetectedScene]:
        """把边界列表转换成场景列表"""
        total_duration = self._get_duration(video_path)
        scenes = []

        times = [0.0] + [b.time for b in boundaries] + [total_duration]
        grade_map = {b.time: b for b in boundaries}

        for i in range(len(times) - 1):
            start = times[i]
            end = times[i + 1]
            boundary = grade_map.get(start)
            scene = DetectedScene(
                scene_id=f"scene_{i+1:04d}",
                start=round(start, 3),
                end=round(end, 3),
                boundary_grade=boundary.grade if boundary else "C",
                motion_score=boundary.motion_score if boundary else 0.0,
            )
            scenes.append(scene)

        return scenes

    def _merge_short_scenes(self, scenes: List[DetectedScene]) -> List[DetectedScene]:
        """合并小于 merge_shorter_than 秒的碎片场景"""
        if not scenes:
            return []
        min_dur = self.config.merge_shorter_than
        merged = []
        bucket = [scenes[0]]

        for scene in scenes[1:]:
            if bucket[-1].duration < min_dur:
                bucket.append(scene)
            else:
                merged.append(self._collapse(bucket))
                bucket = [scene]
        if bucket:
            merged.append(self._collapse(bucket))

        # 重新编号
        for i, s in enumerate(merged):
            s.scene_id = f"scene_{i+1:04d}"
        logger.info("合并碎片场景后剩余: {} 个", len(merged))
        return merged

    def _collapse(self, bucket: List[DetectedScene]) -> DetectedScene:
        if len(bucket) == 1:
            return bucket[0]
        total_dur = sum(s.duration for s in bucket)
        avg_motion = sum(s.motion_score * s.duration for s in bucket) / max(total_dur, 0.001)
        return DetectedScene(
            scene_id=bucket[0].scene_id,
            start=bucket[0].start,
            end=bucket[-1].end,
            motion_score=avg_motion,
            boundary_grade=bucket[0].boundary_grade,
        )

    def _split_long_scenes(self, scenes: List[DetectedScene]) -> List[DetectedScene]:
        """超过 max_scene_duration 的场景强制插入中间切点"""
        max_dur = self.config.max_scene_duration
        result = []
        for scene in scenes:
            if scene.duration <= max_dur:
                result.append(scene)
                continue
            # 按 max_dur 均分
            n = int(scene.duration // max_dur) + 1
            step = scene.duration / n
            for i in range(n):
                start = scene.start + i * step
                end = scene.start + (i + 1) * step
                result.append(DetectedScene(
                    scene_id=f"{scene.scene_id}_p{i+1}",
                    start=round(start, 3),
                    end=round(min(end, scene.end), 3),
                    motion_score=scene.motion_score,
                    boundary_grade="C",
                ))
        return result

    # ── 工具方法 ───────────────────────────────────────────────

    def _get_fps(self, video_path: str) -> float:
        if not _CV2_AVAILABLE:
            return 25.0
        cap = cv2.VideoCapture(video_path)
        fps = cap.get(cv2.CAP_PROP_FPS) or 25.0
        cap.release()
        return fps

    def _get_duration(self, video_path: str) -> float:
        if not _CV2_AVAILABLE:
            return 0.0
        cap = cv2.VideoCapture(video_path)
        fps = cap.get(cv2.CAP_PROP_FPS) or 25.0
        frames = cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0
        cap.release()
        return frames / fps

    def _sample_motion_score(self, video_path: str, t: float, fps: float) -> float:
        """采样切点前后几帧计算运动强度，用于D级误检判断"""
        try:
            cap = cv2.VideoCapture(video_path)
            frames = []
            for offset in [-0.5, -0.2, 0.0, 0.2, 0.5]:
                cap.set(cv2.CAP_PROP_POS_MSEC, max(0, (t + offset) * 1000))
                ok, frame = cap.read()
                if ok:
                    gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
                    frames.append(gray)
            cap.release()
            if len(frames) < 2:
                return 0.3
            diffs = []
            for i in range(len(frames) - 1):
                diff = cv2.absdiff(frames[i], frames[i + 1])
                diffs.append(diff.mean() / 255.0)
            return sum(diffs) / len(diffs)
        except Exception:
            return 0.3

    def _fallback_fixed_interval(self, video_path: str, interval: float = 30.0) -> List[DetectedScene]:
        """scenedetect不可用时按固定间隔切片"""
        total = self._get_duration(video_path)
        if total <= 0:
            return []
        scenes = []
        t = 0.0
        idx = 1
        while t < total:
            end = min(t + interval, total)
            scenes.append(DetectedScene(
                scene_id=f"scene_{idx:04d}",
                start=round(t, 3),
                end=round(end, 3),
                boundary_grade="C",
            ))
            t = end
            idx += 1
        logger.warning("使用固定间隔切片（fallback）: {} 个场景", len(scenes))
        return scenes


def detect_scenes(video_path: str,
                  film_type: str = "auto",
                  config: Optional[SceneDetectorConfig] = None) -> List[DetectedScene]:
    """
    便捷入口函数。
    film_type: auto | action | dialogue | documentary
    """
    if config is None:
        cfg_map = {
            "action": ACTION_CONFIG,
            "dialogue": DIALOGUE_CONFIG,
            "documentary": DOCUMENTARY_CONFIG,
        }
        config = cfg_map.get(film_type, SceneDetectorConfig())
        config.film_type = film_type

    detector = SceneDetector(config)
    return detector.detect(video_path)
