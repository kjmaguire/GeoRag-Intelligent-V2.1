import { useState, useEffect } from 'react';
import { router, usePage } from '@inertiajs/react';
import type { Project } from '@/types';

interface ProjectSelectorProps {
    /**
     * Optional callback. Receives the slug of the newly-selected project
     * before navigation happens. Most pages don't need to override; the
     * default behaviour is to Inertia-visit the same sub-route on the
     * new project (e.g. /projects/{old}/workspace → /projects/{new}/workspace).
     */
    onProjectChange?: (slug: string) => void;
}

/**
 * Project sub-pages that exist for every project (parameter-free routes in
 * routes/web.php). Anything else under /projects/{slug}/ names a record.
 */
const PROJECT_PAGES = new Set([
    'attribute-tables',
    'chat',
    'compare',
    'corpus',
    'imports/quality',
    'ingestion-runs',
    'map',
    'rasters',
    'reports',
    'sources',
    'workspace',
]);

/**
 * Where to go when the user picks another project.
 *
 * Carries over the project PAGE (/workspace, /chat, /reports …) but never a
 * record: /reports/{uuid} or /holes/{collarId}/detail name something in the
 * OLD project and 404 on the new one (FE-14). A record page falls back to the
 * page it lives under, or the overview. The query string is dropped for the
 * same reason.
 */
export function projectSwitchUrl(currentUrl: string, slug: string): string {
    const m = currentUrl.match(/^\/projects\/[^/?#]+\/([^?#]*)/);
    const rest = (m?.[1] ?? '').replace(/\/+$/, '');
    if (PROJECT_PAGES.has(rest)) return `/projects/${slug}/${rest}`;
    const first = rest.split('/')[0];
    if (PROJECT_PAGES.has(first)) return `/projects/${slug}/${first}`;
    return `/projects/${slug}`;
}

export default function ProjectSelector({ onProjectChange }: ProjectSelectorProps) {
    const [projects, setProjects] = useState<Project[]>([]);
    const [selectedSlug, setSelectedSlug] = useState<string>('');
    const [loading, setLoading] = useState<boolean>(true);
    const [error, setError] = useState<string | null>(null);
    const [reloadKey, setReloadKey] = useState(0);
    const { url } = usePage();

    // Sync dropdown to current URL's project slug whenever the page changes.
    useEffect(() => {
        const m = url.match(/^\/projects\/([^/?#]+)/);
        if (m && m[1] !== 'new') {
            setSelectedSlug(m[1]);
        }
    }, [url]);

    useEffect(() => {
        let cancelled = false;

        async function fetchProjects() {
            try {
                setLoading(true);
                setError(null);

                const response = await fetch('/api/v1/projects', {
                    credentials: 'same-origin',
                    headers: {
                        Accept: 'application/json',
                        'X-Requested-With': 'XMLHttpRequest',
                    },
                });

                if (!response.ok) {
                    throw new Error(`HTTP ${response.status}`);
                }

                const data = await response.json();
                const list: Project[] = data.data ?? data;

                if (!cancelled) {
                    setProjects(list);
                    // If the URL didn't include a slug, default to the first project.
                    const urlMatch = url.match(/^\/projects\/([^/?#]+)/);
                    if (!urlMatch && list.length > 0 && list[0].slug) {
                        setSelectedSlug(list[0].slug);
                    }
                }
            } catch {
                if (!cancelled) {
                    setError('Projects unavailable');
                }
            } finally {
                if (!cancelled) {
                    setLoading(false);
                }
            }
        }

        fetchProjects();

        return () => {
            cancelled = true;
        };
        // eslint-disable-next-line react-hooks/exhaustive-deps
    }, [reloadKey]);

    function handleChange(e: React.ChangeEvent<HTMLSelectElement>) {
        const slug = e.target.value;
        if (!slug || slug === selectedSlug) return;
        setSelectedSlug(slug);

        if (onProjectChange) {
            onProjectChange(slug);
            return;
        }

        router.visit(projectSwitchUrl(url, slug));
    }

    if (loading) {
        return (
            <div className="flex items-center gap-2 text-sm text-gray-400">
                <div className="w-3 h-3 rounded-full border-2 border-gray-600 border-t-amber-400 animate-spin" />
                <span>Loading projects…</span>
            </div>
        );
    }

    if (error) {
        return (
            <div className="flex items-center gap-2">
                <span className="text-xs text-gray-500" title={error}>
                    Projects unavailable
                </span>
                <button
                    type="button"
                    // Bumping reloadKey is what refetches; clearing the
                    // error alone left "Loading projects…" up forever (FE-14).
                    onClick={() => {
                        setError(null);
                        setLoading(true);
                        setReloadKey((k) => k + 1);
                    }}
                    className="text-xs text-amber-400 hover:text-amber-300 underline"
                >
                    Retry
                </button>
            </div>
        );
    }

    if (projects.length === 0) {
        return (
            <a
                href="/foundry/projects/new"
                className="text-xs text-amber-400 hover:text-amber-300 border border-amber-800/50 rounded px-3 py-1.5 bg-amber-950/30 hover:bg-amber-950/50 transition-colors"
            >
                + Create your first project
            </a>
        );
    }

    return (
        <div className="flex items-center gap-2">
            <label htmlFor="project-select" className="text-xs text-gray-400 uppercase tracking-wider">
                Project
            </label>
            <select
                id="project-select"
                value={selectedSlug}
                onChange={handleChange}
                className="
                    bg-gray-800 text-gray-100 text-sm
                    border border-gray-700 rounded
                    px-3 py-1.5
                    focus:outline-none focus:ring-2 focus:ring-amber-500 focus:border-transparent
                    cursor-pointer
                    min-w-[200px]
                "
            >
                {projects.map((project) => (
                    <option key={project.project_id} value={project.slug ?? ''}>
                        {project.project_name}
                    </option>
                ))}
            </select>
        </div>
    );
}
