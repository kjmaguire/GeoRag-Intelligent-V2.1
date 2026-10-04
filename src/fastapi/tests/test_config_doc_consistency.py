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


# ---------------------------------------------------------------------------
# EMBEDDING_BACKEND's default, stated in code
# ---------------------------------------------------------------------------
# The default is not a Settings field, so the boolean check above cannot see
# it. It is written out in several places that must agree: the module that
# reads it, config.py's validators, and the Qdrant bootstrap. Until ADR-0025
# (2026-10-04) they all said "bedrock"; a site that kept saying it would
# select a different backend from the one the module that reads it does, and
# the symptom is a startup validator or a collection sized for the wrong
# model, not an error.

_BACKEND_DEFAULT = re.compile(r'os(?:\.environ)?\.get\("EMBEDDING_BACKEND"\)\s*or\s*"([a-z_]+)"')
_REPO = Path(__file__).resolve().parents[3]


def test_every_embedding_backend_default_in_code_is_cohere() -> None:
    sites = [
        _APP / "services" / "embedding.py",
        _APP / "config.py",
        _REPO / "src" / "fastapi" / "scripts" / "init_qdrant.py",
    ]
    found: dict[str, list[str]] = {}
    for path in sites:
        found[path.name] = _BACKEND_DEFAULT.findall(path.read_text(encoding="utf-8"))
        assert found[path.name], f"{path.name}: no EMBEDDING_BACKEND default found -- the pattern is stale"
    wrong = {name: values for name, values in found.items() if set(values) != {"cohere"}}
    assert not wrong, f"EMBEDDING_BACKEND defaults other than 'cohere' (ADR-0025): {wrong}"


def test_the_compose_and_terraform_defaults_agree_with_the_code() -> None:
    compose = (_REPO / "docker-compose.yml").read_text(encoding="utf-8")
    sites = re.findall(r"EMBEDDING_BACKEND:\s*\$\{EMBEDDING_BACKEND:-([a-z_]+)\}", compose)
    assert len(sites) == 2, "expected the fastapi and hatchet-worker services"
    assert set(sites) == {"cohere"}, sites

    # Terraform does NOT hard-code the backend: production moves to Embed 5
    # only when the operator sets var.embedding_backend = "cohere" (the
    # ADR-0025 cutover, after the probe and the snapshot). The variable's
    # default is the rollback, bedrock, so an unrelated apply cannot flip the
    # vector space; its validation names cohere as the other value.
    terraform = (_REPO / "deploy" / "aws" / "terraform" / "config.tf").read_text(encoding="utf-8")
    assert re.search(r"EMBEDDING_BACKEND\s*=\s*var\.embedding_backend", terraform)
    variables = (_REPO / "deploy" / "aws" / "terraform" / "variables.tf").read_text(encoding="utf-8")
    block = re.search(r'variable "embedding_backend" \{.*?\n\}', variables, re.S)
    assert block is not None
    assert re.search(r'default\s*=\s*"bedrock"', block.group(0))
    assert re.search(r'contains\(\["bedrock", "cohere"\]', block.group(0))
