// @ts-nocheck
import { describe, it, expect, vi } from "vitest";
import { render, screen, fireEvent } from "@testing-library/react";

// GuardErrorMessage uses usePage() from @inertiajs/react to read the
// catalog off shared props. In test, we mock the hook to return an
// empty share — the picker still renders chips + placeholders, just
// without the human-readable prose body.
vi.mock("@inertiajs/react", () => ({
    usePage: () => ({ props: { guard_errors: undefined } }),
}));

import { UnitPickerCard } from "../UnitPickerCard";

describe("UnitPickerCard", () => {
    it("renders default unit candidates when none supplied", () => {
        render(<UnitPickerCard commodity="Au" />);
        // 5 defaults: g/t, ppm, ppb, wt%, %
        for (const c of ["g/t", "ppm", "ppb", "wt%", "%"]) {
            expect(screen.getByRole("button", { name: c })).toBeInTheDocument();
        }
    });

    it("renders custom candidate list when supplied", () => {
        render(
            <UnitPickerCard
                commodity="Cu"
                candidates={["wt%", "%"]}
            />,
        );
        expect(screen.getByRole("button", { name: "wt%" })).toBeInTheDocument();
        expect(screen.getByRole("button", { name: "%" })).toBeInTheDocument();
        // Default candidates not in this custom list should be absent.
        expect(screen.queryByRole("button", { name: "ppb" })).not.toBeInTheDocument();
    });

    it("invokes onPick with the chosen candidate", () => {
        const onPick = vi.fn();
        render(<UnitPickerCard commodity="Au" onPick={onPick} />);
        fireEvent.click(screen.getByRole("button", { name: "g/t" }));
        expect(onPick).toHaveBeenCalledWith("g/t");
    });

    it("carries the unit-picker data-surface attribute", () => {
        const { container } = render(<UnitPickerCard commodity="Au" />);
        expect(
            container.querySelector('[data-guard-surface="unit-picker"]'),
        ).not.toBeNull();
    });

    it("tags each candidate button with a data-candidate attribute", () => {
        const { container } = render(<UnitPickerCard commodity="Au" />);
        const buttons = container.querySelectorAll("button[data-candidate]");
        expect(buttons.length).toBe(5);
        // Each data-candidate matches the displayed text.
        for (const btn of buttons) {
            expect(btn.getAttribute("data-candidate")).toBe(btn.textContent);
        }
    });

    it("renders nothing in the candidates list when empty array supplied", () => {
        const { container } = render(
            <UnitPickerCard commodity="Au" candidates={[]} />,
        );
        // No buttons at all (the message body still renders).
        expect(container.querySelectorAll("button").length).toBe(0);
    });

    it("does not crash when commodity is omitted", () => {
        render(<UnitPickerCard />);
        expect(
            screen.getByRole("button", { name: "g/t" }),
        ).toBeInTheDocument();
    });
});
