import { useDashboardFetch } from '@/Hooks/Dashboard/useDashboardFetch';
import type { JurisdictionsPayload } from '@/Types/PublicGeoscience';

/**
 * Fetches the Canadian jurisdictions (+ future countries) registry from
 * Laravel, grouped by country_code with nested sources.
 *
 * Endpoint: GET /api/v1/public-geoscience/jurisdictions  (auth:sanctum)
 */
export function useJurisdictions() {
    return useDashboardFetch<JurisdictionsPayload>(
        '/api/v1/public-geoscience/jurisdictions',
    );
}
