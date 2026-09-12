"""Evaluation: mechanical checks, model comprehension probes, scoring, leakage control, comparison."""

from .compare import compare_runs
from .evaluate import evaluate_version, evaluation_cache_key, transcribe_cached
from .leakage import apply_leakage_result, load_leakage_result, run_no_media_control, save_leakage_result
from .mechanical import run_constraint_checks, run_mechanical_checks
from .probes import build_probe_media, run_probe_trials
from .scoring import ScoreTotals, score_answers
