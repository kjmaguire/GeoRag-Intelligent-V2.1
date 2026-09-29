/**
 * EditProjectSheet.test.tsx — the Overview's "Edit project" sheet.
 *
 * The sheet talks to PATCH /api/v1/projects/{id} through Inertia's `useHttp`,
 * which goes through Inertia's pluggable HTTP client rather than `fetch`, so
 * these tests swap that client for a stub instead of mocking globalThis.fetch.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { render, screen, waitFor, fireEvent } from '@testing-library/react';
import { http, router } from '@inertiajs/react';
import EditProjectSheet, { type EditableProject } from '@/Components/EditProjectSheet';

const project: EditableProject = {
    project_id: '11111111-2222-3333-4444-555555555555',
    project_name: 'Shirley Basin',
    company: 'Cameco Resources',
    commodity: 'uranium',
    region: 'WY',
    crs_epsg: 26913,
};

type StubResponse = { status: number; data: string; headers: Record<string, string> };

function respondWith(status: number, body: unknown): StubResponse {
    return { status, data: JSON.stringify(body), headers: { 'content-type': 'application/json' } };
}

describe('EditProjectSheet', () => {
    let request: ReturnType<typeof vi.fn>;
    let originalClient: ReturnType<typeof http.getClient>;
    let reload: ReturnType<typeof vi.spyOn>;

    beforeEach(() => {
        originalClient = http.getClient();
        request = vi.fn();
        http.setClient({ request } as unknown as ReturnType<typeof http.getClient>);
        reload = vi.spyOn(router, 'reload').mockImplementation(() => {});
    });

    afterEach(() => {
        http.setClient(originalClient);
        vi.restoreAllMocks();
    });

    it('renders nothing while closed', () => {
        render(<EditProjectSheet project={project} open={false} onOpenChange={() => {}} />);
        expect(screen.queryByTestId('edit-project-sheet')).not.toBeInTheDocument();
    });

    it('opens prefilled with the current values and shows the CRS read-only', () => {
        render(<EditProjectSheet project={project} open onOpenChange={() => {}} />);

        expect(screen.getByLabelText(/Project name/)).toHaveValue('Shirley Basin');
        expect(screen.getByLabelText('Operator')).toHaveValue('Cameco Resources');
        expect(screen.getByLabelText('Commodity')).toHaveValue('uranium');
        expect(screen.getByLabelText(/Region/)).toHaveValue('WY');
        expect(screen.getByTestId('edit-project-crs')).toHaveTextContent('EPSG:26913');
        // The CRS is information, not an input.
        expect(screen.queryByDisplayValue('26913')).not.toBeInTheDocument();
    });

    it('keeps a commodity outside the picker list selectable', () => {
        render(<EditProjectSheet project={{ ...project, commodity: 'U3O8' }} open onOpenChange={() => {}} />);
        expect(screen.getByLabelText('Commodity')).toHaveValue('U3O8');
    });

    it('PATCHes only the editable fields, then closes and reloads the Overview', async () => {
        request.mockResolvedValue(respondWith(200, { data: { ...project, project_name: 'Shirley Basin North' } }));
        const onOpenChange = vi.fn();

        render(<EditProjectSheet project={project} open onOpenChange={onOpenChange} />);

        fireEvent.change(screen.getByLabelText(/Project name/), { target: { value: 'Shirley Basin North' } });
        fireEvent.change(screen.getByLabelText('Operator'), { target: { value: '' } });
        fireEvent.change(screen.getByLabelText('Commodity'), { target: { value: 'gold' } });
        fireEvent.click(screen.getByRole('button', { name: 'Save changes' }));

        await waitFor(() => expect(onOpenChange).toHaveBeenCalledWith(false));
        expect(reload).toHaveBeenCalledTimes(1);

        expect(request).toHaveBeenCalledTimes(1);
        const config = request.mock.calls[0][0];
        expect(config.method).toBe('patch');
        expect(config.url).toBe(`/api/v1/projects/${project.project_id}`);
        expect(JSON.parse(config.data)).toEqual({
            project_name: 'Shirley Basin North',
            company: '',
            commodity: 'gold',
            region: 'WY',
        });
    });

    it('shows validation errors against their fields and stays open on a 422', async () => {
        request.mockResolvedValue(
            respondWith(422, {
                message: 'A project name is required.',
                errors: { project_name: ['A project name is required.'] },
            }),
        );
        const onOpenChange = vi.fn();

        render(<EditProjectSheet project={project} open onOpenChange={onOpenChange} />);

        fireEvent.change(screen.getByLabelText(/Project name/), { target: { value: '' } });
        fireEvent.click(screen.getByRole('button', { name: 'Save changes' }));

        expect(await screen.findByText('A project name is required.')).toBeInTheDocument();
        expect(screen.getByLabelText(/Project name/)).toHaveAttribute('aria-invalid', 'true');
        expect(onOpenChange).not.toHaveBeenCalled();
        expect(reload).not.toHaveBeenCalled();
    });

    it('surfaces a server failure with the controller message and error detail', async () => {
        request.mockResolvedValue(
            respondWith(500, { message: 'Failed to update project.', error: 'connection refused' }),
        );
        const onOpenChange = vi.fn();

        render(<EditProjectSheet project={project} open onOpenChange={onOpenChange} />);
        fireEvent.click(screen.getByRole('button', { name: 'Save changes' }));

        expect(await screen.findByTestId('edit-project-failure')).toHaveTextContent(
            'Failed to update project: Failed to update project.: connection refused',
        );
        expect(onOpenChange).not.toHaveBeenCalled();
        expect(reload).not.toHaveBeenCalled();
    });
});
