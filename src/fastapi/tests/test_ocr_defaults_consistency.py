"""OCR_ENGINE / PDF_PARSE_MODE / IMAGE_EMBED_PAGE_SCOPE agree across code and deploy files.

Cohere Parse is the default engine everywhere (2026-10-04), the same rule the
code applies to EMBEDDING_BACKEND: an unset value is the hosted one production
runs. The air-gapped profile is the one place that says `tesseract`, explicitly.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parents[3]


def _read(*parts: str) -> str:
    return (_REPO.joinpath(*parts)).read_text(encoding="utf-8")


def test_compose_defaults_match_production() -> None:
    compose = _read("docker-compose.yml")
    assert re.findall(r"OCR_ENGINE:\s*\$\{OCR_ENGINE:-([a-z_]+)\}", compose) == ["cohere_parse"]
    assert re.findall(r"PDF_PARSE_MODE:\s*\$\{PDF_PARSE_MODE:-([a-z_]+)\}", compose) == ["all"]
    assert re.findall(
        r"IMAGE_EMBED_PAGE_SCOPE:\s*\$\{IMAGE_EMBED_PAGE_SCOPE:-([a-z_]+)\}", compose
    ) == ["figures"]


def test_terraform_sets_the_same_three() -> None:
    tf = _read("deploy", "aws", "terraform", "config.tf")
    assert re.search(r'^\s*OCR_ENGINE\s*=\s*"cohere_parse"', tf, re.M)
    assert re.search(r'^\s*PDF_PARSE_MODE\s*=\s*"all"', tf, re.M)
    assert re.search(r'^\s*IMAGE_EMBED_PAGE_SCOPE\s*=\s*"figures"', tf, re.M)


def test_the_production_env_example_agrees_with_terraform() -> None:
    env = _read(".env.production.example")
    assert re.search(r"^OCR_ENGINE=cohere_parse$", env, re.M)
    assert re.search(r"^PDF_PARSE_MODE=all$", env, re.M)
    assert re.search(r"^IMAGE_EMBED_PAGE_SCOPE=figures$", env, re.M)


def test_the_dev_env_example_keeps_ocr_only_on_purpose_and_says_why() -> None:
    env = _read(".env.example")
    assert re.search(r"^PDF_PARSE_MODE=ocr_only$", env, re.M)
    assert "keeps ocr_only on purpose" in env


def test_the_chart_exposes_the_engine_as_a_value_defaulting_to_cohere_parse() -> None:
    values = _read("charts", "georag", "values.yaml")
    assert re.search(r"^ocr:\n\s+engine:\s*cohere_parse\b", values, re.M)
    assert "air-gapped" in values.lower() or "AIR-GAPPED" in values
    for template in ("fastapi.yaml", "hatchet.yaml"):
        text = _read("charts", "georag", "templates", template)
        assert re.search(
            r"- name: OCR_ENGINE\n\s+value: \{\{ \.Values\.ocr\.engine \| quote \}\}", text
        ), template
        # Next to the EMBEDDING_BACKEND line the same change added.
        assert text.index("name: EMBEDDING_BACKEND") < text.index("name: OCR_ENGINE")


def test_the_airgap_profile_runs_tesseract() -> None:
    assert re.search(
        r"^ocr:\n\s+engine:\s*tesseract\b", _read("charts", "georag", "values-airgap.yaml"), re.M
    )


@pytest.mark.parametrize(
    ("flavor", "engine"),
    [("k3s", "cohere_parse"), ("vanilla", "cohere_parse"), ("airgap", "tesseract")],
)
def test_the_rendered_manifests_set_it_on_both_workloads(flavor: str, engine: str) -> None:
    text = _read("kubernetes", "manifests", f"{flavor}.yaml")
    embedding = re.findall(r"- name: EMBEDDING_BACKEND\n\s+value: \"local\"\n", text)
    ocr = re.findall(r"- name: OCR_ENGINE\n\s+value: \"([a-z_]+)\"\n", text)
    assert len(embedding) == 2, "fastapi and hatchet-worker"
    assert ocr == [engine, engine]
    # Rendered exactly as helm renders the template: the OCR block directly
    # follows the EMBEDDING_BACKEND entry.
    for match in re.finditer(r"- name: EMBEDDING_BACKEND\n\s+value: \"local\"\n(\s+#)", text):
        assert match.group(1)


def test_the_code_defaults_match_the_deploy_files(monkeypatch) -> None:
    from app.services.ingest import ocr_engine

    monkeypatch.delenv("OCR_ENGINE", raising=False)
    monkeypatch.delenv("PDF_PARSE_MODE", raising=False)
    monkeypatch.delenv("IMAGE_EMBED_PAGE_SCOPE", raising=False)
    assert ocr_engine.selected_engine() == "cohere_parse"
    assert ocr_engine.selected_parse_mode() == "all"

    # Documented, deliberate: the code default for page images stays `all`
    # (Kyle, 2026-08-18); compose and Terraform override it to `figures`.
    from app.services.ingest import page_image

    assert page_image.image_embed_scope() == "all"
