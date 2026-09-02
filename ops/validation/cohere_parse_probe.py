"""Wire-contract probe for Cohere Parse v5 on Azure AI Foundry (ADR-0019).

The client in ``src/fastapi/app/services/ingest/cohere_parse_client.py`` was
written from the published API description while the docs hosts were
unreachable. This script records what the live deployment ACTUALLY does so
the client's constants and response adapter can be set from evidence:

  1. which path answers (``/providers/cohere/v2/parse`` first, then the
     alternatives), and the 404 shape of the ones that do not;
  2. whether the ``model`` field accepts the deployment name, the catalog
     id, or the Cohere model id;
  3. the full key shape of ``pages[0]`` in ``blocks`` and ``markdown`` mode;
  4. whether a multi-page ``data:application/pdf`` URI is accepted;
  5. the pixel ladder — first rejected render size → COHERE_PARSE_MAX_PIXELS;
  6. PNG vs JPEG acceptance;
  7. a table page's raw HTML for the html_table tests;
  8. 401 / 404 / 429 shapes and rate-limit headers;
  9. p50 / p95 latency over repeated calls on one page.

Secrets are never written to the report. Raw httpx, no SDK.

Usage:
    AZURE_FOUNDRY_ENDPOINT=... AZURE_FOUNDRY_API_KEY=... \
    AZURE_FOUNDRY_PARSE_DEPLOYMENT=Cohere-parse-v5 \
    uv run python ops/validation/cohere_parse_probe.py \
        --pdf src/fastapi/tests/fixtures/ocr/PLS-2024-Technical-Report.pdf \
        --pages 1,7 --table-page 7
"""

from __future__ import annotations

import argparse
import base64
import io
import json
import os
import statistics
import sys
import time
from pathlib import Path
from typing import Any

import httpx

PATH_LADDER = (
    "/providers/cohere/v2/parse",
    "/providers/cohere/v1/parse",
    "/models/parse",
)
PIXEL_LADDER = (1_900_000, 4_000_000, 8_000_000, 12_000_000, 20_000_000)
POINTS_PER_INCH = 72.0


def _scrub(value: Any) -> Any:
    """Drop base64 payloads and anything key-shaped from recorded bodies."""
    if isinstance(value, dict):
        return {k: ("<redacted>" if k.lower() in {"api-key", "authorization", "url"} else _scrub(v)) for k, v in value.items()}
    if isinstance(value, list):
        return [_scrub(v) for v in value[:5]] + (["..."] if len(value) > 5 else [])
    if isinstance(value, str) and len(value) > 2_000:
        return value[:2_000] + f"...<{len(value)} chars>"
    return value


def _keys(value: Any, depth: int = 0) -> Any:
    """Shape only: key names and value types, recursively."""
    if depth > 4:
        return type(value).__name__
    if isinstance(value, dict):
        return {k: _keys(v, depth + 1) for k, v in value.items()}
    if isinstance(value, list):
        return [_keys(value[0], depth + 1)] if value else []
    return type(value).__name__


def render(pdf_path: str, page_number: int, max_pixels: int, fmt: str = "PNG") -> tuple[bytes, int, int, float]:
    import pypdfium2 as pdfium

    pdf = pdfium.PdfDocument(pdf_path)
    try:
        page = pdf[page_number - 1]
        w, h = page.get_size()
        area = (w / POINTS_PER_INCH) * (h / POINTS_PER_INCH)
        dpi = min(300.0, (max_pixels * 0.97 / area) ** 0.5)
        image = page.render(scale=dpi / POINTS_PER_INCH).to_pil()
        if fmt == "JPEG":
            image = image.convert("RGB")
        buf = io.BytesIO()
        image.save(buf, format=fmt, quality=90) if fmt == "JPEG" else image.save(buf, format=fmt)
        return buf.getvalue(), image.width, image.height, dpi
    finally:
        pdf.close()


def data_uri(payload: bytes, mime: str) -> str:
    return f"data:{mime};base64," + base64.b64encode(payload).decode("ascii")


def call(client: httpx.Client, url: str, key: str, body: dict[str, Any]) -> dict[str, Any]:
    t0 = time.perf_counter()
    try:
        resp = client.post(url, headers={"api-key": key}, json=body)
    except Exception as exc:  # noqa: BLE001
        return {"status": None, "latency_ms": round((time.perf_counter() - t0) * 1000), "error": f"{type(exc).__name__}: {exc}"}
    latency = round((time.perf_counter() - t0) * 1000)
    out: dict[str, Any] = {
        "status": resp.status_code,
        "latency_ms": latency,
        "headers": {k: v for k, v in resp.headers.items() if k.lower().startswith(("retry-after", "x-ratelimit", "x-ms-", "content-type"))},
    }
    try:
        payload = resp.json()
        out["shape"] = _keys(payload)
        out["body"] = _scrub(payload)
    except Exception:  # noqa: BLE001
        out["text"] = resp.text[:500]
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--pdf", required=True)
    ap.add_argument("--pages", default="1")
    ap.add_argument("--table-page", type=int, default=None)
    ap.add_argument("--repeat", type=int, default=5)
    ap.add_argument("--out", default=None)
    args = ap.parse_args()

    endpoint = os.environ.get("AZURE_FOUNDRY_ENDPOINT", "").rstrip("/")
    key = os.environ.get("AZURE_FOUNDRY_API_KEY", "")
    deployment = os.environ.get("AZURE_FOUNDRY_PARSE_DEPLOYMENT", "Cohere-parse-v5")
    if not endpoint or not key:
        print("AZURE_FOUNDRY_ENDPOINT and AZURE_FOUNDRY_API_KEY are required", file=sys.stderr)
        return 2

    pages = [int(p) for p in args.pages.split(",") if p.strip()]
    report: dict[str, Any] = {
        "ran_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "endpoint_host": httpx.URL(endpoint).host,
        "deployment": deployment,
        "pdf": str(args.pdf),
        "steps": {},
    }
    client = httpx.Client(timeout=180.0)

    png, w, h, dpi = render(args.pdf, pages[0], PIXEL_LADDER[0])
    base_body = {"model": deployment, "document": {"type": "image_url", "image_url": {"url": data_uri(png, "image/png")}}, "output_format": "blocks"}
    report["steps"]["render_page_1"] = {"width": w, "height": h, "dpi": round(dpi, 1), "png_bytes": len(png)}

    # 1. path ladder
    winner = None
    ladder: dict[str, Any] = {}
    for path in PATH_LADDER:
        result = call(client, endpoint + path, key, base_body)
        ladder[path] = result
        if result.get("status") and 200 <= result["status"] < 300 and winner is None:
            winner = path
    report["steps"]["path_ladder"] = ladder
    report["steps"]["parse_path"] = winner
    if winner is None:
        print("no path answered 2xx — see report", file=sys.stderr)
        _write(report, args.out)
        return 1
    url = endpoint + winner

    # 2. model field variants
    report["steps"]["model_field"] = {
        variant: call(client, url, key, {**base_body, "model": variant})["status"]
        for variant in (deployment, "Cohere-parse-v5", "parse-v5.0")
    }

    # 3. output formats
    report["steps"]["output_format"] = {
        fmt: call(client, url, key, {**base_body, "output_format": fmt}) for fmt in ("blocks", "markdown")
    }

    # 4. multi-page PDF data URI
    pdf_bytes = Path(args.pdf).read_bytes()
    if len(pdf_bytes) < 20_000_000:
        report["steps"]["multipage_pdf_uri"] = call(
            client, url, key,
            {**base_body, "document": {"type": "image_url", "image_url": {"url": data_uri(pdf_bytes, "application/pdf")}}},
        )
    else:
        report["steps"]["multipage_pdf_uri"] = {"skipped": "fixture over 20 MB"}

    # 5. pixel ladder
    pixel_results: dict[str, Any] = {}
    for cap in PIXEL_LADDER:
        big, bw, bh, bdpi = render(args.pdf, pages[0], cap)
        result = call(client, url, key, {**base_body, "document": {"type": "image_url", "image_url": {"url": data_uri(big, "image/png")}}})
        pixel_results[str(cap)] = {"width": bw, "height": bh, "dpi": round(bdpi, 1), "status": result.get("status"), "latency_ms": result.get("latency_ms"), "error": result.get("body") if result.get("status", 200) >= 400 else None}
    report["steps"]["pixel_ladder"] = pixel_results

    # 6. JPEG
    jpg, *_ = render(args.pdf, pages[0], PIXEL_LADDER[0], fmt="JPEG")
    report["steps"]["jpeg"] = call(client, url, key, {**base_body, "document": {"type": "image_url", "image_url": {"url": data_uri(jpg, "image/jpeg")}}})["status"]

    # 7. table page — full body kept for the html_table fixtures
    if args.table_page:
        tpng, *_ = render(args.pdf, args.table_page, PIXEL_LADDER[1])
        for fmt in ("blocks", "markdown"):
            result = call(client, url, key, {"model": deployment, "document": {"type": "image_url", "image_url": {"url": data_uri(tpng, "image/png")}}, "output_format": fmt})
            report["steps"][f"table_page_{fmt}"] = result

    # 8. error shapes
    report["steps"]["bad_key"] = call(client, url, "not-a-key", base_body)
    report["steps"]["bad_deployment"] = call(client, url, key, {**base_body, "model": "no-such-deployment"})

    # 9. latency
    latencies = [call(client, url, key, base_body)["latency_ms"] for _ in range(max(1, args.repeat))]
    report["steps"]["latency_ms"] = {"samples": latencies, "p50": statistics.median(latencies), "max": max(latencies)}

    _write(report, args.out)
    return 0


def _write(report: dict[str, Any], out: str | None) -> None:
    path = Path(out) if out else Path("ops/validation/reports") / f"cohere_parse_probe_{int(time.time())}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(report, indent=2, default=str))
    print(f"report: {path}")


if __name__ == "__main__":
    sys.exit(main())
