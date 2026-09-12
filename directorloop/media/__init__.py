"""Media pipeline: inspection, frames, transcription, captions, rendering, validation."""

from .captions import CaptionLayout, layout_caption, render_caption_png
from .frames import MotionRegion, SampledFrame, extract_frame, locate_motion_region, sample_frames, sample_timestamps
from .probe import FFMPEG as FFMPEG_PATH
from .probe import MediaError, ffmpeg_capabilities, ffmpeg_version, has_faststart, inspect_media
from .render import RenderError, RenderResult, build_ffmpeg_argv, render_plan
from .transcribe import Transcript, TranscriptSegment, transcribe, whisper_available
from .validate import ValidationResult, actions_for_ops, validate_ops_scope, validate_plan
