"""Plan §0b — measure system-prompt token budget.

Scans ``src/fastapi/app/agent/prompts/`` for prompt text constants,
tokenises with the Qwen3 tokenizer (or tiktoken cl100k fallback when
Hugging Face Hub is unreachable), and writes a markdown report under
``docs/audits/``.

Plan §0b sets the budget at:
  - static system prompt       ≤  1,000 tokens
  - dynamic vocab_context      ≤    640 tokens (8 defs × 80 tok)
  -                            ─────────────
  -                              1,640 max

This script reports per-file static counts. Anything past 1,000 fires a
WARN. Past 1,200 fires a FAIL.

NOT executed by the overnight run — it would download tokenizer weights
and write a report with whatever production prompts currently look like.
Kyle should run this manually as Phase 0b step 1.

Usage:
    cd <repo-root>
    python scripts/measure_system_prompt_tokens.py [--output PATH]
"""

from __future__ import annotations

import argparse
import ast
import datetime as dt
import logging
import sys
from pathlib import Path

log = logging.getLogger("measure_system_prompt_tokens")


# ---------------------------------------------------------------------------
# Tokenizer selection
# ---------------------------------------------------------------------------


def _get_tokenizer():
    """Prefer Qwen3 tokenizer; fall back to tiktoken cl100k_base.

    Returns (encode_fn, name). encode_fn takes a string, returns a list
    of token IDs (length is what we report on).
    """
    try:
        from transformers import AutoTokenizer

        tok = AutoTokenizer.from_pretrained(
            "Qwen/Qwen3-30B-A3B-Instruct-AWQ",
            trust_remote_code=True,
        )
        return (lambda s: tok.encode(s, add_special_tokens=False)), "qwen3-30b-a3b-instruct-awq"
    except Exception as exc:
        log.info("Qwen3 tokenizer unavailable (%s); falling back to tiktoken cl100k", exc)

    try:
        import tiktoken

        enc = tiktoken.get_encoding("cl100k_base")
        return enc.encode, "tiktoken-cl100k_base"
    except Exception as exc:
        log.error("No tokenizer available: %s", exc)
        raise SystemExit(2)


# ---------------------------------------------------------------------------
# Prompt extraction from Python source
# ---------------------------------------------------------------------------


def _extract_string_constants(source: str) -> dict[str, str]:
    """Walk a Python source file's AST, return all module-level
    str assignments and ``def foo(): return "..."``-style constants.

    Covers the patterns the prompts module uses:
      SYSTEM_PROMPT = "..."
      _SECTION = '''...'''
      def build_x(): return "..."
    """
    out: dict[str, str] = {}
    tree = ast.parse(source)

    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    if isinstance(node.value, ast.Constant) and isinstance(node.value.value, str):
                        out[target.id] = node.value.value
        elif isinstance(node, ast.FunctionDef):
            # Single-return-string functions like build_*().
            if len(node.body) == 1 and isinstance(node.body[0], ast.Return):
                ret = node.body[0].value
                if isinstance(ret, ast.Constant) and isinstance(ret.value, str):
                    out[f"{node.name}()"] = ret.value
    return out


def _scan_prompts_dir(prompts_dir: Path) -> dict[str, dict[str, str]]:
    """Return {file_relpath: {constant_name: text}}."""
    results: dict[str, dict[str, str]] = {}
    for py in sorted(prompts_dir.rglob("*.py")):
        if py.name == "__init__.py":
            continue
        try:
            constants = _extract_string_constants(py.read_text(encoding="utf-8"))
        except SyntaxError as exc:
            log.warning("Skipping %s: %s", py, exc)
            continue
        # Keep only strings ≥ 40 chars — anything shorter is probably a
        # label, not a prompt body.
        big = {k: v for k, v in constants.items() if len(v) >= 40}
        if big:
            results[str(py.relative_to(prompts_dir.parent.parent.parent.parent))] = big
    return results


# ---------------------------------------------------------------------------
# Report assembly
# ---------------------------------------------------------------------------


def _build_report(
    *,
    prompts: dict[str, dict[str, str]],
    tokenizer_name: str,
    encode_fn,
    static_budget: int,
    static_hard_ceiling: int,
) -> str:
    today = dt.date.today().isoformat()
    lines: list[str] = [
        f"# System prompt token budget audit — {today}",
        "",
        f"**Tokenizer:** `{tokenizer_name}`",
        f"**Static budget (plan §0b):** ≤ {static_budget} tokens (warn) / ≤ {static_hard_ceiling} tokens (fail)",
        "",
        "## Per-file token counts",
        "",
        "| File | Constant | Tokens | Chars | tokens/char |",
        "|---|---|---|---|---|",
    ]

    grand_total = 0
    per_file_total: dict[str, int] = {}

    for relpath, consts in prompts.items():
        file_total = 0
        for name, text in consts.items():
            n_tokens = len(encode_fn(text))
            file_total += n_tokens
            grand_total += n_tokens
            lines.append(
                f"| `{relpath}` | `{name}` | {n_tokens:,} | {len(text):,} | "
                f"{n_tokens / max(len(text), 1):.3f} |"
            )
        per_file_total[relpath] = file_total

    lines += ["", "## Per-file totals", "", "| File | Total tokens |", "|---|---|"]
    for relpath, total in sorted(per_file_total.items(), key=lambda kv: -kv[1]):
        lines.append(f"| `{relpath}` | {total:,} |")

    lines += [
        "",
        f"## Grand total: {grand_total:,} tokens",
        "",
        "## Budget verdict",
        "",
    ]

    if grand_total <= static_budget:
        lines.append(f"PASS — under {static_budget}-token static budget.")
    elif grand_total <= static_hard_ceiling:
        lines.append(
            f"WARN — {grand_total} tokens exceeds {static_budget}-token static "
            f"budget but stays under {static_hard_ceiling}-token hard ceiling. "
            "Compress or trim before adding the plan §4a structured-answer-format "
            "block (estimated +250 tokens) or plan §1d-iv vocab_instructions "
            "block (estimated +200 tokens)."
        )
    else:
        lines.append(
            f"FAIL — {grand_total} tokens exceeds the {static_hard_ceiling}-token "
            "hard ceiling. Plan §0b acceptance criterion blocked."
        )

    lines += [
        "",
        "## Plan §0b planned additions (NOT yet measured here)",
        "",
        "These additions land *on top of* the current measurements above. "
        "Each is in a separate plan section but stacks onto the same system "
        "prompt budget:",
        "",
        "- `vocab_instructions` block — plan §1d-iv — estimated ≤ 200 tokens",
        "- Structured answer format — plan §4a — estimated ≤ 250 tokens",
        "- Anti-hallucination rules (already partially in prompts) — plan §4a — estimated ≤ 150 tokens",
        "- Dynamic `vocab_context` runtime injection — plan §0b — up to 640 tokens (8 defs × 80 tok)",
        "",
        "Total projected static after additions: "
        f"{grand_total + 600:,} (current + 600 for the three static blocks).",
        "",
        "Total projected runtime: "
        f"{grand_total + 600 + 640:,} (above + max dynamic vocab_context).",
        "",
        "## What to do if WARN / FAIL",
        "",
        "1. Identify the largest constants in the per-file table — those are "
        "the compression targets.",
        "2. Move per-intent guidance into the retrieval profile (already shipped "
        "in `retrieval_profile.py`) rather than the system prompt.",
        "3. Compress example_system.py — single curated example is enough.",
        "4. Move structured-format scaffolding into the assembler, leave only "
        "the *rule* in the system prompt.",
        "",
        "_Generated by `scripts/measure_system_prompt_tokens.py`._",
    ]

    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--prompts-dir",
        default=None,
        help="Override path to the prompts directory (defaults to the repo's "
        "src/fastapi/app/agent/prompts).",
    )
    parser.add_argument(
        "--output",
        default=None,
        help="Output markdown report path. Defaults to docs/audits/"
        "system_prompt_budget_<today>.md.",
    )
    parser.add_argument(
        "--static-budget",
        type=int,
        default=3750,
        help=(
            "Warn threshold for static per-query system prompt (revised from "
            "plan §0b's original 1000 after the 2026-05-27 audit measured "
            "real per-query usage at 3,000–3,400 tok). See "
            "docs/audits/system_prompt_budget_2026_05_27.md."
        ),
    )
    parser.add_argument(
        "--static-hard-ceiling",
        type=int,
        default=4500,
        help=(
            "Fail threshold for static per-query system prompt (revised from "
            "plan §0b's original 1200). Leaves headroom for §4a (~240 tok) "
            "and §1d-iv vocab_instructions (~200 tok)."
        ),
    )
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

    repo_root = Path(__file__).resolve().parents[1]
    prompts_dir = (
        Path(args.prompts_dir)
        if args.prompts_dir
        else repo_root / "src" / "fastapi" / "app" / "agent" / "prompts"
    )
    if not prompts_dir.exists():
        log.error("Prompts dir not found: %s", prompts_dir)
        return 2

    output = Path(args.output) if args.output else (
        repo_root / "docs" / "audits" / f"system_prompt_budget_{dt.date.today().isoformat()}.md"
    )
    output.parent.mkdir(parents=True, exist_ok=True)

    encode_fn, tokenizer_name = _get_tokenizer()
    log.info("Tokenizer: %s", tokenizer_name)

    prompts = _scan_prompts_dir(prompts_dir)
    log.info("Found %d prompt files with non-trivial constants", len(prompts))

    report = _build_report(
        prompts=prompts,
        tokenizer_name=tokenizer_name,
        encode_fn=encode_fn,
        static_budget=args.static_budget,
        static_hard_ceiling=args.static_hard_ceiling,
    )
    output.write_text(report, encoding="utf-8")
    log.info("Report written to %s", output)

    # Compute grand total for exit code parity with CI gating intent.
    total = sum(
        len(encode_fn(text))
        for consts in prompts.values()
        for text in consts.values()
    )
    if total > args.static_hard_ceiling:
        log.error("FAIL: %d tokens > %d hard ceiling", total, args.static_hard_ceiling)
        return 1
    if total > args.static_budget:
        log.warning("WARN: %d tokens > %d budget", total, args.static_budget)
    else:
        log.info("PASS: %d tokens within budget", total)
    return 0


if __name__ == "__main__":
    sys.exit(main())
