// @ts-nocheck — test file, type safety enforced at component level
import { describe, it, expect } from "vitest";
import { surfaceFor } from "../GuardErrorDispatcher";

describe("surfaceFor — code → surface kind mapping", () => {
    it("maps refusal codes to 'refusal'", () => {
        expect(surfaceFor("NO_EVIDENCE_FOUND")).toBe("refusal");
        expect(surfaceFor("CITATION_INCOMPLETE")).toBe("refusal");
        expect(surfaceFor("UNSUPPORTED_QUERY_TYPE")).toBe("refusal");
        expect(surfaceFor("SPATIAL_QUERY_EMPTY")).toBe("refusal");
        expect(surfaceFor("SPATIAL_CRS_MISMATCH")).toBe("refusal");
        expect(surfaceFor("GRAPH_PATH_NOT_FOUND")).toBe("refusal");
        expect(surfaceFor("DEATH_LOOP")).toBe("refusal");
    });

    it("maps ambiguity codes to 'ambiguity'", () => {
        expect(surfaceFor("AMBIGUOUS_HOLE_ID")).toBe("ambiguity");
        expect(surfaceFor("AMBIGUOUS_FORMATION_NAME")).toBe("ambiguity");
        expect(surfaceFor("AMBIGUOUS_PROPERTY_NAME")).toBe("ambiguity");
    });

    it("maps CONFLICTING_SOURCES to 'conflict'", () => {
        expect(surfaceFor("CONFLICTING_SOURCES")).toBe("conflict");
    });

    it("maps non-clarification evidence-quality codes to 'partial'", () => {
        expect(surfaceFor("NUMERIC_GROUNDING_FAILED")).toBe("partial");
        expect(surfaceFor("OVER_FILTERED_QUERY")).toBe("partial");
    });

    it("maps unit-clarification codes to 'unit-picker' (§4b Stage 2)", () => {
        expect(surfaceFor("MISSING_ASSAY_UNITS")).toBe("unit-picker");
        expect(surfaceFor("REQUEST_UNIT_CLARIFICATION")).toBe("unit-picker");
    });

    it("maps depth-clarification codes to 'depth-picker' (§4b Stage 2)", () => {
        expect(surfaceFor("MISSING_DEPTH_INTERVAL")).toBe("depth-picker");
        expect(surfaceFor("REQUEST_DEPTH_CLARIFICATION")).toBe("depth-picker");
    });

    it("maps SOURCE_SCOPE_VIOLATION to 'incident' — the only red surface", () => {
        expect(surfaceFor("SOURCE_SCOPE_VIOLATION")).toBe("incident");
    });

    it("unknown codes fall back to 'refusal' (safe default)", () => {
        expect(surfaceFor("NOT_A_REAL_CODE")).toBe("refusal");
        expect(surfaceFor("")).toBe("refusal");
    });
});

describe("CODE_TO_SURFACE coverage — every plan §4b code maps to a surface", () => {
    const ALL_PLAN_4B_CODES = [
        "NO_EVIDENCE_FOUND",
        "ENTITY_NOT_FOUND",
        "AMBIGUOUS_HOLE_ID",
        "AMBIGUOUS_FORMATION_NAME",
        "AMBIGUOUS_PROPERTY_NAME",
        "OVER_FILTERED_QUERY",
        "SPATIAL_QUERY_EMPTY",
        "SPATIAL_CRS_MISMATCH",
        "GRAPH_PATH_NOT_FOUND",
        "NUMERIC_GROUNDING_FAILED",
        "CITATION_INCOMPLETE",
        "CONFLICTING_SOURCES",
        "MISSING_DEPTH_INTERVAL",
        "MISSING_ASSAY_UNITS",
        "SOURCE_SCOPE_VIOLATION",
        "UNSUPPORTED_QUERY_TYPE",
    ];

    it("every code returns a valid surface kind", () => {
        const validKinds = new Set([
            "refusal",
            "ambiguity",
            "conflict",
            "partial",
            "incident",
            // §4b Stage 2 — terminal picker surfaces
            "unit-picker",
            "depth-picker",
        ]);
        for (const code of ALL_PLAN_4B_CODES) {
            expect(validKinds.has(surfaceFor(code))).toBe(true);
        }
    });

    it("ENTITY_NOT_FOUND has no explicit surface — falls back to refusal", () => {
        // ENTITY_NOT_FOUND is handled inline by the message renderer
        // via the _NO_ALIASES degradation; no separate surface kind.
        expect(surfaceFor("ENTITY_NOT_FOUND")).toBe("refusal");
    });
});
