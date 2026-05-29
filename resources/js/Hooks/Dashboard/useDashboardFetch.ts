import { useState, useEffect, useCallback, useRef } from 'react';
import type { DashboardResponse } from '@/Types/Dashboard';

interface UseDashboardFetchResult<T> {
    data: T | null;
    loading: boolean;
    error: string | null;
    generatedAt: string | null;
    retry: () => void;
}

export function useDashboardFetch<T>(url: string): UseDashboardFetchResult<T> {
    const [data, setData] = useState<T | null>(null);
    const [loading, setLoading] = useState(true);
    const [error, setError] = useState<string | null>(null);
    const [generatedAt, setGeneratedAt] = useState<string | null>(null);
    const controllerRef = useRef<AbortController | null>(null);

    const fetchData = useCallback(async () => {
        controllerRef.current?.abort();
        const controller = new AbortController();
        controllerRef.current = controller;

        setLoading(true);
        setError(null);

        try {
            const response = await fetch(url, {
                signal: controller.signal,
                credentials: 'same-origin',
                headers: {
                    Accept: 'application/json',
                    'X-Requested-With': 'XMLHttpRequest',
                },
            });

            if (!response.ok) {
                throw new Error(`HTTP ${response.status}`);
            }

            const envelope: DashboardResponse<T> = await response.json();
            setData(envelope.data);
            setGeneratedAt(envelope.generated_at);
        } catch (err) {
            if (err instanceof DOMException && err.name === 'AbortError') return;
            setError(err instanceof Error ? err.message : 'Fetch failed');
        } finally {
            if (!controller.signal.aborted) {
                setLoading(false);
            }
        }
    }, [url]);

    useEffect(() => {
        fetchData();
        return () => controllerRef.current?.abort();
    }, [fetchData]);

    return { data, loading, error, generatedAt, retry: fetchData };
}
