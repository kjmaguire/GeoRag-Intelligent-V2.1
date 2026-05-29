// @ts-nocheck — test file, type safety enforced at component level
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { render, screen, waitFor, fireEvent } from '@testing-library/react';
import React from 'react';
import { EvidenceInspector } from '../chat/EvidenceInspector';

const mockFetch = vi.fn();
global.fetch = mockFetch;

vi.mock("radix-ui", async (importOriginal) => {
  const actual = await importOriginal();
  return {
    ...actual,
    Dialog: {
      ...actual.Dialog,
      Root: ({ open, children }) => open ? <div data-testid="sheet-root">{children}</div> : null,
      Portal: ({ children }) => <div>{children}</div>,
      Overlay: () => null,
      Content: ({ children }) => <div data-testid="sheet-content">{children}</div>,
      Title: ({ children }) => <h2 data-testid="sheet-title">{children}</h2>,
      Description: ({ children }) => <p>{children}</p>,
      Close: ({ children }) => <button data-testid="sheet-close">{children}</button>,
      Trigger: ({ children }) => <button>{children}</button>,
    },
    ScrollArea: {
      Root: ({ children }) => <div data-testid="scroll-area">{children}</div>,
      Viewport: ({ children }) => <div>{children}</div>,
      ScrollAreaScrollbar: () => null,
      ScrollAreaThumb: () => null,
      Corner: () => null,
    },
  };
});

function makeLeg(ov) { return { citation_id: "[DATA:1]", citation_type: "DATA", source_chunk_id: "chunk-abc", document_title: "Test Doc", relevance_score: 0.85, section: "S3", page: 42, ...ov }; }

describe("EvidenceInspector legacy path", () => {
  afterEach(() => vi.clearAllMocks());
  it("renders legacy doc title", () => {
    const cit = makeLeg();
    render(<EvidenceInspector open={true} onOpenChange={vi.fn()} legacyCitation={cit} />);
    expect(screen.getByText(/Test Doc/)).toBeTruthy();
  });
  it("shows Source Reference title in legacy mode", () => {
    render(<EvidenceInspector open={true} onOpenChange={vi.fn()} legacyCitation={makeLeg()} />);
    expect(screen.getByTestId("sheet-title").textContent).toContain("Source Reference");
  });
  it("does not fetch when no evidenceId", () => {
    render(<EvidenceInspector open={true} onOpenChange={vi.fn()} legacyCitation={makeLeg()} />);
    expect(mockFetch).not.toHaveBeenCalled();
  });
  it("renders nothing when open=false", () => {
    const { container } = render(<EvidenceInspector open={false} onOpenChange={vi.fn()} evidenceId="x" />);
    expect(container.querySelector("[data-testid=sheet-root]")).toBeNull();
  });
});

describe("EvidenceInspector fetch error states", () => {
  afterEach(() => vi.clearAllMocks());
  it("shows 404 error message", async () => {
    mockFetch.mockResolvedValueOnce({ ok: false, status: 404, json: async () => ({}) });
    render(<EvidenceInspector open={true} onOpenChange={vi.fn()} evidenceId="missing" />);
    await waitFor(() => expect(screen.getByText(/Evidence not found/)).toBeTruthy());
  });
  it("shows 500 error message", async () => {
    mockFetch.mockResolvedValueOnce({ ok: false, status: 500, json: async () => ({}) });
    render(<EvidenceInspector open={true} onOpenChange={vi.fn()} evidenceId="bad" />);
    await waitFor(() => expect(screen.getByText(/Failed to load evidence details/)).toBeTruthy());
  });
  it("shows network error on fetch rejection", async () => {
    mockFetch.mockRejectedValueOnce(new Error("Network error"));
    render(<EvidenceInspector open={true} onOpenChange={vi.fn()} evidenceId="any" />);
    await waitFor(() => expect(screen.getByText(/Network error/)).toBeTruthy());
  });
  it("renders Retry button on error", async () => {
    mockFetch.mockResolvedValueOnce({ ok: false, status: 500, json: async () => ({}) });
    render(<EvidenceInspector open={true} onOpenChange={vi.fn()} evidenceId="bad" />);
    await waitFor(() => expect(screen.getByText(/Retry/)).toBeTruthy());
  });
});

describe("EvidenceInspector document_passage branch", () => {
  afterEach(() => vi.clearAllMocks());
  it("renders passage_text", async () => {
    mockFetch.mockResolvedValueOnce({ ok: true, json: async () => ({ evidence_type: "document_passage", passage_text: "Gold mineralization in altered granite.", page: 47, deep_link: "/api/v1/documents/view?page=47" }) });
    render(<EvidenceInspector open={true} onOpenChange={vi.fn()} evidenceId="doc-uuid" />);
    await waitFor(() => expect(screen.getByText(/Gold mineralization/)).toBeTruthy());
  });
  it("renders context_before and context_after", async () => {
    mockFetch.mockResolvedValueOnce({ ok: true, json: async () => ({ evidence_type: "document_passage", passage_text: "Main.", context_before: "Before context here.", context_after: "After context here." }) });
    render(<EvidenceInspector open={true} onOpenChange={vi.fn()} evidenceId="doc-uuid-2" />);
    await waitFor(() => { expect(screen.getByText(/Before context here/)).toBeTruthy(); expect(screen.getByText(/After context here/)).toBeTruthy(); });
  });
  it("renders deep_link anchor", async () => {
    mockFetch.mockResolvedValueOnce({ ok: true, json: async () => ({ evidence_type: "document_passage", passage_text: "P.", page: 12, deep_link: "/api/v1/documents/view?page=12" }) });
    render(<EvidenceInspector open={true} onOpenChange={vi.fn()} evidenceId="doc-uuid-3" />);
    await waitFor(() => { const link = screen.getByRole("link"); expect(link.getAttribute("href")).toContain("/api/v1/documents/view"); });
  });
});

describe("EvidenceInspector structured_record branch", () => {
  afterEach(() => vi.clearAllMocks());
  it("renders structured_ref key-value rows", async () => {
    mockFetch.mockResolvedValueOnce({ ok: true, json: async () => ({ evidence_type: "structured_record", structured_ref: { schema_name: "silver", table_name: "assay_results" }, lineage: { bronze_uri: "s3://b/raw.csv", parser_name: "csv_parser" } }) });
    render(<EvidenceInspector open={true} onOpenChange={vi.fn()} evidenceId="struct-uuid" />);
    await waitFor(() => { expect(screen.getByText(/schema_name/)).toBeTruthy(); expect(screen.getByText(/silver/)).toBeTruthy(); });
  });
  it("renders lineage provenance", async () => {
    mockFetch.mockResolvedValueOnce({ ok: true, json: async () => ({ evidence_type: "structured_record", structured_ref: {}, lineage: { parser_name: "test_parser", parser_version: "2.0" } }) });
    render(<EvidenceInspector open={true} onOpenChange={vi.fn()} evidenceId="struct-uuid-2" />);
    await waitFor(() => expect(screen.getByText(/test_parser/)).toBeTruthy());
  });
});

describe("EvidenceInspector graph_edge branch", () => {
  afterEach(() => vi.clearAllMocks());
  it("renders start and end node labels", async () => {
    mockFetch.mockResolvedValueOnce({ ok: true, json: async () => ({ evidence_type: "graph_edge", graph_edge_ref: { type: "INTERSECTS" }, start_node_labels: ["Deposit"], start_node_preview: { name: "Zone A" }, end_node_labels: ["DrillHole"], end_node_preview: { hole_id: "DH-001" }, described_in: [] }) });
    render(<EvidenceInspector open={true} onOpenChange={vi.fn()} evidenceId="graph-uuid" />);
    await waitFor(() => { expect(screen.getByText(/Deposit/)).toBeTruthy(); expect(screen.getByText(/DrillHole/)).toBeTruthy(); });
  });
  it("renders described_in items", async () => {
    mockFetch.mockResolvedValueOnce({ ok: true, json: async () => ({ evidence_type: "graph_edge", graph_edge_ref: {}, start_node_labels: ["A"], end_node_labels: ["B"], described_in: ["ev:linked-doc-1"] }) });
    render(<EvidenceInspector open={true} onOpenChange={vi.fn()} evidenceId="graph-uuid-2" />);
    await waitFor(() => expect(screen.getByText(/ev:linked-doc-1/)).toBeTruthy());
  });
});

describe("EvidenceInspector map_feature branch", () => {
  afterEach(() => vi.clearAllMocks());
  it("renders bbox coordinates", async () => {
    mockFetch.mockResolvedValueOnce({ ok: true, json: async () => ({ evidence_type: "map_feature", bbox: [-120.5, 49.2, -120.1, 49.5], tile_function: "public.occurrences", feature_properties: { name: "Test Mine" } }) });
    render(<EvidenceInspector open={true} onOpenChange={vi.fn()} evidenceId="map-uuid" />);
    await waitFor(() => expect(screen.getByText(/-120.500000/)).toBeTruthy());
  });
  it("renders feature_properties table", async () => {
    mockFetch.mockResolvedValueOnce({ ok: true, json: async () => ({ evidence_type: "map_feature", bbox: [-120.5, 49.2, -120.1, 49.5], feature_properties: { commodity: "Uranium", status: "Active" } }) });
    render(<EvidenceInspector open={true} onOpenChange={vi.fn()} evidenceId="map-uuid-2" />);
    await waitFor(() => { expect(screen.getByText(/commodity/)).toBeTruthy(); expect(screen.getByText(/Uranium/)).toBeTruthy(); });
  });
});

// ── Auth surface (security regression) ───────────────────────────────────────
describe('EvidenceInspector — auth surface', () => {
  let getItemSpy: ReturnType<typeof vi.spyOn>;

  beforeEach(() => {
    getItemSpy = vi.spyOn(Storage.prototype, 'getItem');
    mockFetch.mockResolvedValue({
      ok: true,
      json: async () => ({ evidence_type: 'document_passage', passage_text: 'Test.' }),
    });
  });

  afterEach(() => {
    getItemSpy.mockRestore();
    vi.clearAllMocks();
  });

  it('does not read auth tokens from localStorage when fetching evidence', async () => {
    render(<EvidenceInspector open={true} onOpenChange={vi.fn()} evidenceId="sec-uuid" />);
    await waitFor(() => expect(mockFetch).toHaveBeenCalled());

    const tokenLike = /token|jwt|secret/i;
    const offendingKeys = getItemSpy.mock.calls
      .map((call) => String(call[0]))
      .filter((key) => tokenLike.test(key));
    expect(offendingKeys).toEqual([]);
  });

  it('sends the fetch with same-origin credentials', async () => {
    render(<EvidenceInspector open={true} onOpenChange={vi.fn()} evidenceId="sec-uuid-2" />);
    await waitFor(() => expect(mockFetch).toHaveBeenCalled());

    const [, init] = mockFetch.mock.calls[0] as [string, RequestInit];
    expect(init?.credentials).toBe('same-origin');

    const headers = (init?.headers ?? {}) as Record<string, string>;
    expect(headers.Authorization).toBeUndefined();
  });
});