"""Tests that the evaluator system prompt is stable across retries so the
Claude prompt cache hits on retry 2 and 3 within its TTL."""

from pathlib import Path

from src.core.orchestrator import _build_eval_replacements, load_prompt


# tests/ lives at the repo root, so parent.parent == repo root
PROMPTS_DIR = Path(__file__).parent.parent / "src" / "prompts"


class TestEvalPromptCacheStability:
    def test_system_prompt_is_stable_across_retries(self, tmp_path):
        """Rendered evaluator system prompt must be byte-identical for retry 1, 2, 3
        so the Claude prompt cache hits on retries."""
        config = {
            "evaluator": {"browser_verification": "never"},
            "generator": {"max_retries_per_feature": 3},
        }

        repl_1, eval_file, eval_fallback, _ = _build_eval_replacements(
            "feat-1", tmp_path, config, "go test ./...", retry_count=1)
        repl_2, _, _, _ = _build_eval_replacements(
            "feat-1", tmp_path, config, "go test ./...", retry_count=2)
        repl_3, _, _, _ = _build_eval_replacements(
            "feat-1", tmp_path, config, "go test ./...", retry_count=3)

        prompt_path = PROMPTS_DIR / eval_file
        if not prompt_path.exists():
            prompt_path = PROMPTS_DIR / eval_fallback

        rendered_1 = load_prompt(prompt_path, repl_1)
        rendered_2 = load_prompt(prompt_path, repl_2)
        rendered_3 = load_prompt(prompt_path, repl_3)

        assert rendered_1 == rendered_2 == rendered_3, (
            "Evaluator system prompt changed across retries — prompt cache will miss. "
            "RETRY_COUNT / MAX_RETRIES must not appear in system prompt replacements."
        )

    def test_user_message_carries_retry_context_when_retry_gt_1(self, tmp_path):
        """Retry context still needs to reach the evaluator — it just moves from
        system prompt into user message."""
        config = {
            "evaluator": {"browser_verification": "never"},
            "generator": {"max_retries_per_feature": 3},
        }

        _, _, _, msg_first = _build_eval_replacements(
            "feat-1", tmp_path, config, "go test ./...", retry_count=1)
        _, _, _, msg_retry = _build_eval_replacements(
            "feat-1", tmp_path, config, "go test ./...", retry_count=2)

        assert "attempt" not in msg_first.lower()
        assert "attempt 2 of 3" in msg_retry

    def test_template_has_no_retry_vars(self):
        """Evaluator prompt template must not reference {{RETRY_COUNT}} or {{MAX_RETRIES}}."""
        text = (PROMPTS_DIR / "evaluator-v2.md").read_text()
        assert "{{RETRY_COUNT}}" not in text
        assert "{{MAX_RETRIES}}" not in text
