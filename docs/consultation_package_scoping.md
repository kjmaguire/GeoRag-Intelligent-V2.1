# Consultation-Package Output Mode — Scoping Proposal

**Document version:** 1.0 (scoping — not yet a kickoff)
**Audience:** Kyle (decide whether to open this as a master-plan phase, defer to roadmap, or kill)
**Authored:** 2026-05-23, in response to discovery note `georag-cc-02-chatgpt-missed.md` Item 2.
**Status of implementation:** none — no `consultation`, `first_nation`, `indigenous`, `plain_language`, or `stakeholder` code exists in the repo as of this date.

---

## What this would ship, in plain language

> "Generate a self-contained, source-cited information package about a project that a junior exploration company can hand to a First Nation, a regulator, or a community group for meaningful consultation — without our customer's geologist having to assemble it by hand or having to trust an AI summary they can't verify."

The package is **not a research artifact for geologists**. It is a deliverable for an external non-technical stakeholder. The audience determines the shape: large maps, plain language, every claim cited to a source the recipient could in principle request, no jargon, no acronyms without expansion.

---

## Why Anna named this

Junior explorers in Canada are legally required to conduct meaningful consultation with Indigenous nations whose territory overlaps their claims. The current workflow is: a geologist spends days assembling a custom PDF (claim boundaries, drill plan, nearby sensitive features, historical disturbance, project narrative), often pulling from disparate sources, often without consistent citation. The output is high-stakes — bad consultation packages drive legal risk, project delays, and reputational damage.

This is **adjacent to GeoRAG's existing capabilities** (we already have claim boundaries, drill plans, NI 43-101 ingest, citation infrastructure) but is a **distinct deliverable** — neither QGIS export nor an internal RAG answer is shaped right for the recipient.

## Why this might be a wedge

- **Regulatory differentiator.** Junior miners' consultation obligation is mandatory and growing in scope. A tool that produces defensible, source-cited consultation materials reduces real legal exposure.
- **Commercial pull independent of geology workflow.** Many juniors would buy this even if they didn't trust AI for geology insight, because the assembly cost is so high today.
- **Not a feature most competitors will copy.** It requires combining spatial export, plain-language summarization with citation discipline, and a non-technical recipient profile — a stack we already have.

## Why it might still not be worth doing now

- **Compliance scope creep risk.** "Consultation package" can mean very different things in different jurisdictions; defining the scope narrowly enough to ship in 2-3 weeks rather than 6 months is non-obvious.
- **Plain-language summarization is a hallucination risk surface.** Today's §04i citation guards are tuned for geologist audiences who recognize wrong answers. Non-technical recipients can't sanity-check claims. The blast radius of a single wrong number in a consultation package is much higher than the same wrong number in an internal chat answer.
- **The customer relationship is not yet bidirectional.** We don't have any junior explorer using this in production today. Building consultation packages requires deep co-design with at least one real consultation lead at a junior, plus probably a First Nations community liaison. Without that input loop, we'd build the wrong thing.

---

## Recommended next step (NOT implementation)

**Before any code or kickoff: 3 conversations.**

1. **Anna** — pin down exactly what her current consultation-package workflow looks like. Specifically: what software does she use today (Word + ArcGIS + manual citation? Or something more integrated)? What gets included? What gets excluded? Who reviews it before it ships?
2. **A junior miner's consultation lead** (Anna's network or via Mining Hub once Carl is engaged — see CC-02 Item 7) — independent of Anna, what does *their* workflow look like? Where does it break down? What would they pay to make easier?
3. **A First Nations consultation expert or lawyer** — what makes a consultation package legally defensible vs. dismissible? What does the recipient side need that the sender side often forgets? (This is the hardest conversation to get but the most load-bearing.)

The output of these three conversations is a 1-page brief that defines:
- The minimum-viable scope (5-7 must-have package components)
- The legal-defensibility bar (what level of citation is sufficient)
- The non-technical readability bar (target reading level, what to avoid)
- One concrete pilot customer + use case

**Only after that brief exists** should we open this as a master-plan phase.

---

## Sketch of what the scope would look like (for sizing only — not a plan)

If we did pursue this, the rough deliverable would be:

| Deliverable | What it produces | Reuses |
|---|---|---|
| `ConsultationPackageGenerator` service | A multi-section bundle keyed on `(project_id, jurisdiction_code)` | Existing `GenerateExportJob`, MinIO presigned URL pattern |
| Plain-language summarizer | Per-section narrative paragraphs generated from silver tables + ingested reports, with mandatory citation per claim | §04i citation guards, §04p VL summarizer |
| Spatial map renderer | Static map images (PNG via MapLibre headless or equivalent) showing claim boundary + drill plan + sensitive features overlay | Existing tile infrastructure, MapLibre layer registry |
| Bundle assembler | DOCX/PDF compiler that produces the final document with cover page, TOC, embedded maps, full citation list, glossary of terms | `pdf_renderer.py`, WeasyPrint already in deps |
| Reviewer workflow | Geologist reviews + signs off before the package is exportable, recorded in `silver.consultation_package_reviews` | Existing review-workflow pattern |
| Non-technical readability gate | Automated check (Flesch-Kincaid or similar) that warns when generated text is above target reading level | New |
| Citation-density gate | Refuses to ship a package below a configurable claims-per-citation floor | Reuses §04i Layer 5 (Provenance) |

**Rough effort estimate (only if the brief lands):** 4-6 weeks for a v1 limited to Saskatchewan junior-explorer projects with the most common First Nations consultation requirements. Wider scope is multiples larger.

---

## Decision asks for Kyle

1. **Do you want to schedule the three scoping conversations above?** If yes, who books them? If no, this stays on the roadmap with no further work until something changes.
2. **Is there a pilot customer in mind?** Without a concrete first user, the scope will drift.
3. **Is this a priority before or after the existing master-plan §5–§11 backlog?** If after, this doc can be archived and revisited when those phases finish.

**Default if no answer:** archived as roadmap-only. No code work scheduled.
