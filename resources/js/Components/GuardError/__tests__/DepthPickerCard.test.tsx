import { describe, it, expect, vi } from "vitest";
import { render, screen, fireEvent } from "@testing-library/react";

vi.mock("@inertiajs/react", () => ({
    usePage: () => ({ props: { guard_errors: undefined } }),
}));

import { DepthPickerCard } from "../DepthPickerCard";

describe("DepthPickerCard", () => {
    it("renders default m / ft candidates when none supplied", () => {
        render(<DepthPickerCard value="250.5" />);
        expect(screen.getByRole("button", { name: "m" })).toBeInTheDocument();
        expect(screen.getByRole("button", { name: "ft" })).toBeInTheDocument();
    });

    it("renders custom candidate list when supplied", () => {
        render(
            <DepthPickerCard
                value="100"
                candidates={["yd", "fathom"]}
            />,
        );
        expect(screen.getByRole("button", { name: "yd" })).toBeInTheDocument();
        expect(screen.getByRole("button", { name: "fathom" })).toBeInTheDocument();
        // m / ft defaults must NOT be present.
        expect(screen.queryByRole("button", { name: "m" })).not.toBeInTheDocument();
    });

    it("invokes onPick with the chosen unit", () => {
        const onPick = vi.fn();
        render(<DepthPickerCard value="250.5" onPick={onPick} />);
        fireEvent.click(screen.getByRole("button", { name: "ft" }));
        expect(onPick).toHaveBeenCalledWith("ft");
    });

    it("carries the depth-picker data-surface attribute", () => {
        const { container } = render(<DepthPickerCard value="250.5" />);
        expect(
            container.querySelector('[data-guard-surface="depth-picker"]'),
        ).not.toBeNull();
    });

    it("tags each candidate button with a data-candidate attribute", () => {
        const { container } = render(<DepthPickerCard value="100" />);
        const buttons = container.querySelectorAll("button[data-candidate]");
        expect(buttons.length).toBe(2);
        for (const btn of buttons) {
            expect(btn.getAttribute("data-candidate")).toBe(btn.textContent);
        }
    });

    it("renders without the candidates list when empty array supplied", () => {
        const { container } = render(
            <DepthPickerCard value="100" candidates={[]} />,
        );
        expect(container.querySelectorAll("button").length).toBe(0);
    });

    it("does not crash when value is omitted", () => {
        render(<DepthPickerCard />);
        expect(screen.getByRole("button", { name: "m" })).toBeInTheDocument();
    });
});
