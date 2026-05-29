import { useState, useEffect } from 'react';

const STALENESS_MULTIPLIER = 2;
const CHECK_INTERVAL_MS = 30_000;

interface FreshnessResult {
    isStale: boolean;
    minutesAgo: number;
}

export function useFreshnessWarning(
    generatedAt: string | null,
    expectedRefreshMinutes: number,
): FreshnessResult {
    const [result, setResult] = useState<FreshnessResult>({ isStale: false, minutesAgo: 0 });

    useEffect(() => {
        function compute() {
            if (!generatedAt) {
                setResult({ isStale: false, minutesAgo: 0 });
                return;
            }
            const diffMs = Date.now() - new Date(generatedAt).getTime();
            const minutesAgo = Math.floor(diffMs / 60_000);
            const thresholdMs = STALENESS_MULTIPLIER * expectedRefreshMinutes * 60_000;
            setResult({ isStale: diffMs > thresholdMs, minutesAgo });
        }

        compute();
        const interval = setInterval(compute, CHECK_INTERVAL_MS);
        return () => clearInterval(interval);
    }, [generatedAt, expectedRefreshMinutes]);

    return result;
}
