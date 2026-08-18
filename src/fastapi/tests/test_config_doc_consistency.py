"""Guard against comments that state the wrong default for a settings flag.

Three separate instances of this were found on 2026-08-18, and each one cost
real investigation time or pointed an operator at a broken configuration:

  * pdf_report.py documented the Azure OCR selector as
    ``OCR_ENGINE=document_intelligence``. The value actually matched is
    ``azure_document_intelligence``; following the docstring silently leaves
    the engine on the Tesseract default.
  * agentic_retrieval/graph.py said MULTI_TURN_RESOLUTION_ENABLED was "False
    (default)" when it ships True — i.e. it read as "multi-turn is off unless
    you opt in".
  * agent/deps.py said MULTI_TENANT_ENFORCEMENT_ENABLED was "False (default)"
    when it ships True — claiming tenant isolation is opt-in when it is on.

A wrong comment about a flag is worse than no comment: it is trusted, and it
sends people looking in the wrong place (or worse, reassures them about an
isolation guarantee that works the other way round). This test parses the real
defaults out of config.py and fails if prose anywhere under app/ contradicts
them.
"""

from __future__ import annotations

import re
from pathlib import Path

_APP = Path(__file__).resolve().parents[1] / "app"
_CONFIG = _APP / "config.py"


def _boolean_defaults() -> dict[str, str]:
    """Map SETTING_NAME -> "True"/"False" as declared on the Settings model."""
    text = _CONFIG.read_text(encoding="utf-8")
    return {
        m.group(1): m.group(2)
        for m in re.finditer(
            r"^\s{4}([A-Z][A-Z0-9_]+):\s*bool\s*=\s*(True|False)", text, re.M
        )
    }


def _python_sources() -> list[Path]:
    return [p for p in _APP.rglob("*.py") if "__pycache__" not in p.parts]


def test_config_has_boolean_settings_to_check() -> None:
    """Sanity check: the regex still matches the Settings model's shape.

    Without this, a refactor of config.py that broke the parse would make
    every assertion below vacuously pass.
    """
    assert len(_boolean_defaults()) >= 10, (
        "parsed suspiciously few boolean settings out of config.py — the "
        "Settings declaration style probably changed and this guard is now blind"
    )


def test_no_comment_claims_the_opposite_default() -> None:
    """No prose under app/ may state a boolean flag's default backwards."""
    defaults = _boolean_defaults()
    violations: list[str] = []

    for path in _python_sources():
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except UnicodeDecodeError:  # pragma: no cover — defensive
            continue

        for name, actual in defaults.items():
            wrong = "False" if actual == "True" else "True"
            # Only phrasings that actually assert a default, so an ordinary
            # `if settings.FLAG is False:` branch is not flagged.
            claim = re.compile(
                rf"\b{wrong}\b[^.]{{0,25}}\(default\)|defaults?\s+to\s+{wrong}\b"
            )
            for lineno, line in enumerate(lines, start=1):
                if name in line and claim.search(line):
                    violations.append(
                        f"{path.relative_to(_APP.parent)}:{lineno} says {name} "
                        f"defaults to {wrong}, but config.py declares {actual} "
                        f"— {line.strip()[:100]}"
                    )

    assert not violations, "settings documented with the wrong default:\n" + "\n".join(
        violations
    )
