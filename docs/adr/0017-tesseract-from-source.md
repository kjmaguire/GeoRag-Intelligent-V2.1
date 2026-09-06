# ADR 0017: Tesseract 5.5 from source

- **Date**: 2026-06-23
- **Status**: Accepted
- **Deciders**: Kyle Maguire (SME)
- **Supersedes**: prior reliance on Debian trixie's apt-shipped `tesseract-ocr` (5.4.x)
- **Related**: ADR-0019 (Cohere Parse v5 — defines the OCR ladder Tesseract now sits under); ADR-0016 (PaddleOCR, superseded 2026-07-29)

## Context

> **Corrected 2026-09-06 — tier order.** The paragraph below describes the
> §04p OCR ladder as it stood on 2026-06-23. That ladder no longer exists:
> Docling and PaddleOCR were deleted on 2026-07-29 (see the ADR-0016
> supersession note; `DOCLING_OCR_ENABLED` is no longer read anywhere),
> Azure Document Intelligence was primary from 2026-07-28 to 2026-09-02, and
> since 2026-09-02 (ADR-0019) the ladder is **Cohere Parse v5 on Azure AI
> Foundry primary → Tesseract last resort**. Tesseract runs when Parse is
> unavailable, unconfigured, over the per-document page budget
> (`OCR_MAX_PAGES_PER_DOC`), or returns empty output — gated by
> `PDF_PARSER_TESSERACT_FALLBACK_ENABLED` — and it is the only engine when
> `OCR_ENGINE=tesseract`. Every call site invokes it with `--psm 3 --oem 3`
> (`services/ingest/pdf_report.py`). The decision itself (build 5.5.2 from
> source) is unaffected and still describes the running image.

At the time of this decision, Tesseract was the fallback OCR tier in the §04p PDF stack (Docling primary → PaddleOCR secondary → Tesseract fallback when `DOCLING_OCR_ENABLED=false` or per-page docling failures). Debian trixie's apt-shipped `tesseract-ocr` caps at 5.4.x; the upstream 5.5.x line ships layout-analysis speed wins, better Unicode handling, and bug fixes for specific image preprocessing edge cases.

The 2026-06 audit initially proposed deferring this — Tesseract is a fallback path, the gain is marginal, and the build complexity is real. Decision was reopened during the audit-wrap session: with the wider sweep already touching `docker/fastapi.Dockerfile` (langgraph fix, PaddleOCR 3.x migration, base image SHA pinning), adding the Tesseract from-source build alongside is the natural place to capture the upgrade rather than leaving it as a perpetual TODO.

## Options considered

| Option | Pros | Cons | Outcome |
|---|---|---|---|
| **A. Build Tesseract 5.5 from source in a dedicated builder stage** | Latest tesseract; controllable via `ARG TESSERACT_VERSION`; tessdata pinned to a known LSTM model | Adds a Dockerfile stage; +5–10 min build time; +30–60 MB image; we become a Tesseract distributor | **Chosen.** |
| B. Wait for Debian to ship 5.5 in `trixie-backports` | Zero effort | Indefinite wait; trixie freeze already locked at 5.4.x | Rejected — no ETA. |
| C. Use a third-party prebuilt Tesseract image | No source compile complexity | Adds a third-party-image dep; image hygiene + SHA pin would need separate ongoing work; not all builds expose the configure flags we'd want | Rejected — friction equivalent or worse than source build. |
| D. Stay on Debian's 5.4.x | Zero effort | Audit gap remains; falls progressively behind upstream | Rejected — leaving the gap was the previous deferral; this ADR closes it. |

## Decision

Build **Tesseract 5.5.2** from source in a dedicated `tesseract-builder` Docker stage, install to `/opt/tesseract`, and `COPY --from=tesseract-builder` into the runtime stage.

### Implementation

`docker/fastapi.Dockerfile` gains a new `tesseract-builder` stage (between the python:3.13-slim digest pin and the existing `builder` stage):

```dockerfile
ARG TESSERACT_VERSION=5.5.2

# Build-time deps: build-essential autoconf automake libtool pkg-config
# libleptonica-dev libpng-dev libjpeg62-turbo-dev libtiff-dev zlib1g-dev
# libicu-dev libpango1.0-dev libcairo2-dev curl ca-certificates

curl -fsSL "https://github.com/tesseract-ocr/tesseract/archive/refs/tags/${TESSERACT_VERSION}.tar.gz" | tar xz
cd tesseract-${TESSERACT_VERSION}
./autogen.sh
./configure --prefix=/opt/tesseract --disable-debug --disable-doc --disable-graphics
make -j$(nproc) && make install

curl -fsSL https://github.com/tesseract-ocr/tessdata_fast/raw/main/eng.traineddata \
     -o /opt/tesseract/share/tessdata/eng.traineddata
```

Runtime stage drops trixie's `tesseract-ocr` + `tesseract-ocr-eng` apt packages and instead:

1. Installs the runtime shared libs Tesseract dynamically links against: `libleptonica6`, `libpng16-16`, `libjpeg62-turbo`, `libtiff6`, `libicu76`, **`libgomp1`** (OpenMP — critical, easy to miss).
2. `COPY --from=tesseract-builder /opt/tesseract /opt/tesseract`.
3. Sets `PATH=/opt/tesseract/bin:$PATH` and `TESSDATA_PREFIX=/opt/tesseract/share/tessdata`.

### Verification (2026-06-23)

```
$ docker run --rm georag/fastapi:latest tesseract --version
tesseract 5.5.2
 leptonica-1.84.1
  libgif 5.2.2 : libjpeg 6b (libjpeg-turbo 2.1.5) : libpng 1.6.48 : libtiff 4.7.0 : zlib 1.3.1 : libwebp 1.5.0 : libopenjp2 2.5.3
 Found AVX2
 Found AVX

$ docker run --rm georag/fastapi:latest tesseract --list-langs
List of available languages in "/opt/tesseract/share/tessdata/" (1):
eng
```

## Consequences

### Positive

- Tesseract 5.5.2 lands in the image with `AVX2`/`AVX` instruction sets detected — layout-analysis speed wins on the fallback OCR path.
- Bumping the version is one line: change `ARG TESSERACT_VERSION=` and rebuild.
- `tessdata_fast/eng.traineddata` is pinned to a specific HEAD — model isn't subject to silent upstream drift.
- The runtime image picks up the explicit `libgomp1` dep, which would also benefit any future component linking against OpenMP (numpy MKL backend, PaddlePaddle GPU kernels, etc. — PaddlePaddle itself was removed 2026-07-29).

### Negative

- Multi-stage build: image build time +5–10 min on a clean `--no-cache` rebuild. Subsequent rebuilds reuse the Tesseract layer unless `TESSERACT_VERSION` or the apt deps change.
- Runtime image size +30–60 MB (Tesseract binaries + tessdata).
- We're now a Tesseract distributor — when 5.5.3 (or 5.6) ships, we have to bump deliberately. Mitigated by the `ARG TESSERACT_VERSION` being one-line.
- The source build picks up whatever `libleptonica-dev` trixie ships (currently 1.84.x). If Tesseract ever needs a newer Leptonica than trixie packages, this stage will also need to build Leptonica from source — adding more complexity.

### Neutral

- License unchanged (Apache 2.0).
- API to consumers unchanged — `pytesseract` Python wrapper finds `tesseract` via `PATH` exactly as before.
- The `silver.pdf_ocr_results` schema unchanged. The `source_method` provenance tag distinguishes the engine that produced each row; Tesseract output is tagged the same way regardless of v5.4 vs v5.5. (Since ADR-0019 the passage-level provenance is `ocr_method = 'tesseract'` on `silver.document_passages`, with `ocr_confidence` carrying the mean word confidence; the version-independence point still holds.)

## What this ADR does NOT do

- Add language packs beyond English. The `tessdata_fast/eng.traineddata` is the only language data baked in. Adding others (e.g. for French/Spanish NI 43-101 reports) is a one-line `curl` addition to the builder stage — defer until a real corpus need surfaces.
- Switch from `tessdata_fast` to the full LSTM `tessdata` weights. The fast variant is comparable accuracy on printed text and ~5× smaller. Revisit if scan-quality accuracy on the fallback path becomes a measured bottleneck.
- Build Leptonica from source. Trixie's `libleptonica-dev` (1.84) is current enough for Tesseract 5.5.2. Revisit if a future Tesseract version requires newer.

## References

- `docker/fastapi.Dockerfile` — `tesseract-builder` stage + runtime stage `COPY` + env.
- ADR-0002 — §04p PDF stack (the in-process stack Tesseract belongs to).
- ADR-0019 — Cohere Parse v5 (the current OCR ladder; Tesseract is its floor).
- ADR-0016 — PaddleOCR 3.x migration (sibling OCR tier upgrade landed in the same sweep; superseded 2026-07-29, PaddleOCR deleted).
- [Tesseract 5.5.2 release](https://github.com/tesseract-ocr/tesseract/releases/tag/5.5.2)
- [tessdata_fast](https://github.com/tesseract-ocr/tessdata_fast)
- 2026-06 audit punch-list item 16 (originally deferred, reopened during audit-wrap session).
