"""The Bedrock chat path must not ask for more output than the window allows.

`prompt_tokens + max_tokens` over the model's context window is a 400 from
the provider, not a truncation — so the run fails AFTER paying to build the
prompt, on exactly the long-context queries the product exists to answer.

The OpenAI-compatible path has carried this guard since the vLLM era. The
first draft of the Bedrock port (ADR-0022) dropped it, and nothing noticed
except `scripts/check_settings_have_readers.py`, which flagged
BEDROCK_CHAT_MAX_MODEL_LEN as a field no code read. That is a thin thread to
hang a guard on: the setting could have been given a cosmetic reader and the
behaviour would still have been gone. These tests pin the behaviour.
"""

from __future__ import annotations

import pytest

from app.agent.llm_bedrock import (
    _MIN_OUTPUT_TOKENS,
    _SAFETY_MARGIN_TOKENS,
    cap_output_tokens,
)

WINDOW = 128_000


def test_short_prompt_gets_the_full_requested_output() -> None:
    """The common case must not be penalised by the guard existing."""
    assert cap_output_tokens(max_output=4096, max_model_len=WINDOW, prompt_chars=5_000) == 4096


def test_long_prompt_shrinks_the_output_request() -> None:
    # 125k tokens of prompt in a 128k window leaves 128000 - 125000 - 512 =
    # 2488 for output, which is less than the 4096 requested.
    #
    # 120k does NOT trip it (7488 of room), which is worth stating: the cap
    # only engages near the ceiling, so most long-context runs are untouched.
    capped = cap_output_tokens(
        max_output=4096, max_model_len=WINDOW, prompt_chars=int(125_000 * 2.2)
    )
    assert capped < 4096
    assert capped >= _MIN_OUTPUT_TOKENS

    untouched = cap_output_tokens(
        max_output=4096, max_model_len=WINDOW, prompt_chars=int(120_000 * 2.2)
    )
    assert untouched == 4096


def test_the_capped_request_fits_inside_the_window() -> None:
    """The property that actually matters, across the whole prompt range."""
    for prompt_tokens in (1_000, 50_000, 120_000, 127_000, 200_000):
        prompt_chars = int(prompt_tokens * 2.2)
        capped = cap_output_tokens(
            max_output=4096, max_model_len=WINDOW, prompt_chars=prompt_chars
        )
        estimated = int(prompt_chars / 2.2)
        if estimated + _MIN_OUTPUT_TOKENS + _SAFETY_MARGIN_TOKENS <= WINDOW:
            assert estimated + capped + _SAFETY_MARGIN_TOKENS <= WINDOW, (
                f"prompt~{estimated} + output {capped} overflows the {WINDOW} window"
            )


def test_a_prompt_that_fills_the_window_still_asks_for_something() -> None:
    """Never 0 and never negative.

    An over-long prompt should surface as a clean provider error on the
    failover ladder, not as a zero- or negative-length output request that
    different providers reject in different ways.
    """
    capped = cap_output_tokens(
        max_output=4096, max_model_len=WINDOW, prompt_chars=WINDOW * 10
    )
    assert capped == _MIN_OUTPUT_TOKENS


@pytest.mark.parametrize("window", [8_192, 32_000, 128_000])
def test_the_window_setting_is_honoured(window: int) -> None:
    """A smaller BEDROCK_CHAT_MAX_MODEL_LEN must cap harder.

    This is the assertion that makes the setting a control: change it and
    behaviour changes.
    """
    prompt_chars = int(7_000 * 2.2)
    capped = cap_output_tokens(max_output=4096, max_model_len=window, prompt_chars=prompt_chars)
    if window == 8_192:
        assert capped < 4096, "a 7k-token prompt in an 8k window must shrink the output"
    else:
        assert capped == 4096
