import { describe, it, expect } from "vitest";
import {
    ALL_GUARD_ERROR_CODES,
    resolveGuardErrorMessage,
} from "../GuardErrorMessage";

// Inline catalog mirrors lang/en/guard_errors.php for assertions.
// Subset — only what the tests below check. Drop-in: paste the real
// templates here when running against the live Inertia share.
const CATALOG = {
    NO_EVIDENCE_FOUND:
        "I couldn't find anything in the current project's documents that " +
        "addresses this. Try rephrasing or check that the documents you're " +
        "thinking of have been ingested.",
    ENTITY_NOT_FOUND:
        "I couldn't find :entity in the current project. Did you mean: " +
        ":suggested_aliases? Or this entity may not appear in the ingested " +
        "documents.",
    ENTITY_NOT_FOUND_NO_ALIASES:
        "I couldn't find :entity in the current project. This entity may " +
        "not appear in the ingested documents.",
    CONFLICTING_SOURCES:
        "Two documents disagree on this value. :document_a states :value_a. " +
        ":document_b states :value_b.",
    CONFLICTING_SOURCES_WITH_AUTHORITY:
        "Two documents disagree on this value. :document_a states :value_a. " +
        ":document_b states :value_b. The current source is " +
        ":authoritative_doc; the other has been superseded.",
    UNSUPPORTED_QUERY_TYPE:
        "This question falls outside what I can answer from the ingested " +
        "documents. :reason. You may want to :specific_alternative_action.",
    CITATION_INCOMPLETE:
        "I don't have enough citations to back up this answer confidently.",
};

describe("ALL_GUARD_ERROR_CODES locks down plan §4b code list", () => {
    it("includes the 16 canonical codes + DEATH_LOOP", () => {
        expect(ALL_GUARD_ERROR_CODES).toContain("NO_EVIDENCE_FOUND");
        expect(ALL_GUARD_ERROR_CODES).toContain("ENTITY_NOT_FOUND");
        expect(ALL_GUARD_ERROR_CODES).toContain("AMBIGUOUS_HOLE_ID");
        expect(ALL_GUARD_ERROR_CODES).toContain("CONFLICTING_SOURCES");
        expect(ALL_GUARD_ERROR_CODES).toContain("SOURCE_SCOPE_VIOLATION");
        expect(ALL_GUARD_ERROR_CODES).toContain("DEATH_LOOP");
        // 16 plan codes + DEATH_LOOP = 17
        expect(ALL_GUARD_ERROR_CODES).toHaveLength(17);
    });
});

describe("resolveGuardErrorMessage — canonical templates", () => {
    it("renders NO_EVIDENCE_FOUND verbatim", () => {
        const out = resolveGuardErrorMessage("NO_EVIDENCE_FOUND", {}, CATALOG);
        expect(out).toContain("I couldn't find anything");
    });

    it("substitutes :placeholder markers", () => {
        const out = resolveGuardErrorMessage(
            "ENTITY_NOT_FOUND",
            {
                entity: "Rowan",
                suggested_aliases: "WRLG Rowan, West Red Lake Rowan",
            },
            CATALOG,
        );
        expect(out).toContain("Rowan");
        expect(out).toContain("WRLG Rowan");
        expect(out).not.toContain(":entity");
        expect(out).not.toContain(":suggested_aliases");
    });

    it("leaves :placeholder literal when value missing", () => {
        const out = resolveGuardErrorMessage(
            "ENTITY_NOT_FOUND",
            { entity: "Rowan" }, // suggested_aliases omitted but with NO_ALIASES fallback…
            CATALOG,
        );
        // …in this case the degradation rule fires, so the rendered
        // template is _NO_ALIASES which doesn't have :suggested_aliases.
        expect(out).toContain("Rowan");
        expect(out).not.toContain(":suggested_aliases");
    });
});

describe("resolveGuardErrorMessage — degradation variants", () => {
    it("ENTITY_NOT_FOUND degrades to _NO_ALIASES when suggested_aliases empty", () => {
        const out = resolveGuardErrorMessage(
            "ENTITY_NOT_FOUND",
            { entity: "Rowan", suggested_aliases: "" },
            CATALOG,
        );
        expect(out).toContain("Rowan");
        expect(out).not.toContain("Did you mean");
        expect(out).toContain("may not appear in the ingested documents");
    });

    it("ENTITY_NOT_FOUND degrades to _NO_ALIASES when suggested_aliases null", () => {
        const out = resolveGuardErrorMessage(
            "ENTITY_NOT_FOUND",
            { entity: "Rowan", suggested_aliases: null },
            CATALOG,
        );
        expect(out).not.toContain("Did you mean");
    });

    it("ENTITY_NOT_FOUND degrades to _NO_ALIASES when suggested_aliases omitted", () => {
        const out = resolveGuardErrorMessage(
            "ENTITY_NOT_FOUND",
            { entity: "Rowan" },
            CATALOG,
        );
        expect(out).not.toContain("Did you mean");
    });

    it("CONFLICTING_SOURCES stays neutral when no authority", () => {
        const out = resolveGuardErrorMessage(
            "CONFLICTING_SOURCES",
            {
                document_a: "NI 43-101 (2024)",
                document_b: "Fact sheet (2024)",
                value_a: "1.18 Moz Au",
                value_b: "1.1 Moz Au",
            },
            CATALOG,
        );
        expect(out).toContain("NI 43-101");
        expect(out).toContain("Fact sheet");
        expect(out).not.toContain("current source is");
    });

    it("CONFLICTING_SOURCES picks _WITH_AUTHORITY when authoritative_doc set", () => {
        const out = resolveGuardErrorMessage(
            "CONFLICTING_SOURCES",
            {
                document_a: "NI 43-101 (2024)",
                document_b: "NI 43-101 (2021)",
                value_a: "1.18 Moz Au",
                value_b: "0.9 Moz Au",
                authoritative_doc: "NI 43-101 (2024)",
            },
            CATALOG,
        );
        expect(out).toContain("current source is");
        expect(out).toContain("NI 43-101 (2024)");
    });
});

describe("resolveGuardErrorMessage — fallbacks", () => {
    it("unknown code falls back to UNSUPPORTED_QUERY_TYPE with diagnostic", () => {
        const out = resolveGuardErrorMessage("NOT_A_REAL_CODE", {}, CATALOG);
        expect(out).toContain("outside what I can answer");
        // The fallback template carries the diagnostic reason.
        expect(out).toContain("unknown guard code");
        expect(out).toContain("NOT_A_REAL_CODE");
    });

    it("returns the code itself when catalog is undefined (Inertia share not loaded yet)", () => {
        const out = resolveGuardErrorMessage(
            "NO_EVIDENCE_FOUND",
            {},
            undefined,
        );
        expect(out).toBe("NO_EVIDENCE_FOUND");
    });
});
