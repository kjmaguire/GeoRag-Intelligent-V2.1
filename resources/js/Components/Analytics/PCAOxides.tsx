import { useMemo } from 'react';
import GeoPlot from '../GeoPlot';

interface GeochemRow {
    hole_id: string;
    from_depth: number;
    to_depth: number;
    sio2_wt_pct: number | null;
    al2o3_wt_pct: number | null;
    fe2o3_wt_pct: number | null;
    mgo_wt_pct: number | null;
    cao_wt_pct: number | null;
    na2o_wt_pct: number | null;
    k2o_wt_pct: number | null;
}

interface Props { rows: GeochemRow[]; }

/**
 * Principal-component scatter of 7 major oxides — an unsupervised view
 * of where samples cluster in multivariate oxide space. Each point is
 * one geochem sample; colour is the originating hole. PC1 + PC2 eat
 * most of the variance and usually separate felsic ↔ mafic lithologies
 * on PC1 and alkali-vs-alkaline-earth on PC2.
 *
 * Implementation notes:
 *   - Standardise (z-score) each column before PCA so high-variance
 *     oxides like SiO₂ don't dominate.
 *   - Compute the covariance matrix then its eigendecomposition via
 *     Jacobi rotation. For 7 × 7 this converges in a few dozen sweeps.
 *   - No external linalg lib — vendored a minimal implementation below
 *     to keep the bundle tight.
 */

const OXIDES = [
    'sio2_wt_pct',
    'al2o3_wt_pct',
    'fe2o3_wt_pct',
    'mgo_wt_pct',
    'cao_wt_pct',
    'na2o_wt_pct',
    'k2o_wt_pct',
] as const;
const OXIDE_LABELS: Record<string, string> = {
    sio2_wt_pct:  'SiO₂',
    al2o3_wt_pct: 'Al₂O₃',
    fe2o3_wt_pct: 'Fe₂O₃',
    mgo_wt_pct:   'MgO',
    cao_wt_pct:   'CaO',
    na2o_wt_pct:  'Na₂O',
    k2o_wt_pct:   'K₂O',
};

function mean(xs: number[]): number { return xs.reduce((s, v) => s + v, 0) / xs.length; }
function std(xs: number[], m: number): number {
    if (xs.length < 2) return 1;
    const v = xs.reduce((s, x) => s + (x - m) ** 2, 0) / (xs.length - 1);
    return Math.sqrt(v) || 1;
}

/**
 * Symmetric-matrix eigendecomposition by Jacobi rotation.
 * Returns eigenvalues in descending order and the corresponding
 * eigenvectors as rows. Sufficient for the 7 × 7 oxide covariance.
 */
function jacobiEigen(A: number[][]): { values: number[]; vectors: number[][] } {
    const n = A.length;
    const M = A.map((row) => row.slice());
    const V: number[][] = Array.from({ length: n }, (_, i) =>
        Array.from({ length: n }, (_, j) => (i === j ? 1 : 0)),
    );
    const MAX_SWEEPS = 80;
    for (let sweep = 0; sweep < MAX_SWEEPS; sweep++) {
        let off = 0;
        for (let p = 0; p < n - 1; p++) for (let q = p + 1; q < n; q++) off += M[p][q] * M[p][q];
        if (off < 1e-12) break;
        for (let p = 0; p < n - 1; p++) {
            for (let q = p + 1; q < n; q++) {
                const apq = M[p][q];
                if (Math.abs(apq) < 1e-14) continue;
                const app = M[p][p];
                const aqq = M[q][q];
                const theta = (aqq - app) / (2 * apq);
                const t = theta >= 0
                    ? 1 / (theta + Math.sqrt(1 + theta * theta))
                    : 1 / (theta - Math.sqrt(1 + theta * theta));
                const c = 1 / Math.sqrt(1 + t * t);
                const s = t * c;
                // Rotate rows/cols p,q.
                for (let i = 0; i < n; i++) {
                    const mip = M[i][p];
                    const miq = M[i][q];
                    M[i][p] = c * mip - s * miq;
                    M[i][q] = s * mip + c * miq;
                }
                for (let j = 0; j < n; j++) {
                    const mpj = M[p][j];
                    const mqj = M[q][j];
                    M[p][j] = c * mpj - s * mqj;
                    M[q][j] = s * mpj + c * mqj;
                }
                for (let i = 0; i < n; i++) {
                    const vip = V[i][p];
                    const viq = V[i][q];
                    V[i][p] = c * vip - s * viq;
                    V[i][q] = s * vip + c * viq;
                }
            }
        }
    }
    const eigen = Array.from({ length: n }, (_, i) => ({
        value: M[i][i],
        vector: V.map((row) => row[i]),
    })).sort((a, b) => b.value - a.value);
    return {
        values: eigen.map((e) => e.value),
        vectors: eigen.map((e) => e.vector),
    };
}

const HOLE_PALETTE = [
    '#38bdf8', '#a855f7', '#22c55e', '#eab308', '#f97316',
    '#ec4899', '#14b8a6', '#f43f5e', '#3b82f6', '#84cc16',
];

export default function PCAOxides({ rows }: Props) {
    const { trace, layout, explained, loadings, n } = useMemo(() => {
        // Extract the oxide matrix, dropping any row with a missing oxide.
        const matrix: number[][] = [];
        const holes: string[] = [];
        const depths: number[] = [];
        for (const r of rows) {
            const vec = OXIDES.map((k) => r[k]);
            if (vec.some((v) => v === null || !Number.isFinite(v as number))) continue;
            matrix.push(vec as number[]);
            holes.push(r.hole_id);
            depths.push(0.5 * (r.from_depth + r.to_depth));
        }
        if (matrix.length < 3) {
            return { trace: null, layout: {}, explained: null, loadings: null, n: matrix.length };
        }

        const d = OXIDES.length;
        const N = matrix.length;

        // Standardise (z-score).
        const means = Array.from({ length: d }, (_, j) => mean(matrix.map((row) => row[j])));
        const stds  = Array.from({ length: d }, (_, j) => std(matrix.map((row) => row[j]), means[j]));
        const Z = matrix.map((row) => row.map((v, j) => (v - means[j]) / stds[j]));

        // Covariance of Z ≡ correlation of original (since standardised).
        const cov: number[][] = Array.from({ length: d }, () => Array(d).fill(0));
        for (let i = 0; i < N; i++) {
            for (let a = 0; a < d; a++) for (let b = 0; b < d; b++) cov[a][b] += Z[i][a] * Z[i][b];
        }
        for (let a = 0; a < d; a++) for (let b = 0; b < d; b++) cov[a][b] /= N - 1;

        const { values, vectors } = jacobiEigen(cov);
        const totalVar = values.reduce((s, v) => s + Math.max(v, 0), 0) || 1;
        const explained = values.map((v) => (Math.max(v, 0) / totalVar) * 100);

        // Project each standardised row onto PC1 + PC2.
        const pc1 = vectors[0];
        const pc2 = vectors[1];
        const scoresX = Z.map((row) => row.reduce((s, v, j) => s + v * pc1[j], 0));
        const scoresY = Z.map((row) => row.reduce((s, v, j) => s + v * pc2[j], 0));

        // Colour by originating hole (first 10 holes get distinct colours;
        // rest fall through to slate-gray).
        const uniqueHoles = Array.from(new Set(holes));
        const holeColor: Record<string, string> = {};
        uniqueHoles.forEach((h, i) => {
            holeColor[h] = HOLE_PALETTE[i % HOLE_PALETTE.length];
        });

        const traces: Record<string, unknown>[] = [];
        for (const hole of uniqueHoles) {
            const idxs = holes.map((h, i) => (h === hole ? i : -1)).filter((i) => i >= 0);
            traces.push({
                type: 'scatter',
                mode: 'markers',
                x: idxs.map((i) => scoresX[i]),
                y: idxs.map((i) => scoresY[i]),
                marker: { color: holeColor[hole], size: 7, opacity: 0.9, line: { color: 'rgba(0,0,0,0.3)', width: 0.5 } },
                text: idxs.map((i) => `${hole} · ${depths[i].toFixed(1)} m`),
                hovertemplate: '%{text}<br>PC1: %{x:.2f}<br>PC2: %{y:.2f}<extra></extra>',
                name: hole,
            });
        }

        return {
            trace: traces,
            layout: {
                paper_bgcolor: 'rgba(0,0,0,0)', plot_bgcolor: 'rgba(0,0,0,0)',
                font: { color: '#94a3b8', size: 11 },
                margin: { l: 52, r: 12, t: 6, b: 40 },
                xaxis: {
                    title: { text: `PC1 (${explained[0].toFixed(1)}%)`, font: { color: '#cbd5e1' } },
                    gridcolor: 'rgba(148,163,184,0.18)', zerolinecolor: 'rgba(148,163,184,0.4)', color: '#94a3b8',
                },
                yaxis: {
                    title: { text: `PC2 (${explained[1].toFixed(1)}%)`, font: { color: '#cbd5e1' } },
                    gridcolor: 'rgba(148,163,184,0.18)', zerolinecolor: 'rgba(148,163,184,0.4)', color: '#94a3b8',
                },
                legend: { font: { color: '#cbd5e1', size: 10 } },
            },
            explained,
            loadings: { pc1, pc2 },
            n: N,
        };
    }, [rows]);

    if (!trace || !explained || !loadings) {
        return (
            <div className="h-[340px] flex items-center justify-center text-sm text-gray-500">
                Need at least 3 samples with all 7 oxides to compute PCA. (have {n})
            </div>
        );
    }

    const loadingsTable = OXIDES.map((k, j) => ({
        oxide: OXIDE_LABELS[k],
        pc1: loadings.pc1[j],
        pc2: loadings.pc2[j],
    }));

    return (
        <div className="grid grid-cols-1 lg:grid-cols-[2fr_1fr] gap-4">
            <div className="bg-gray-900/40 rounded border border-gray-800 p-3 h-[380px]">
                <div className="text-xs text-gray-400 mb-1">
                    PC1 + PC2 scores · {n} samples · {(explained[0] + explained[1]).toFixed(1)}% total variance
                </div>
                <div className="h-[340px]">
                    <GeoPlot data={trace as Record<string, unknown>[]} layout={layout as Record<string, unknown>} />
                </div>
            </div>
            <div className="bg-gray-900/40 rounded border border-gray-800 p-3 text-xs">
                <div className="text-gray-300 font-medium mb-2">Component loadings</div>
                <table className="w-full text-[11px]">
                    <thead>
                        <tr className="text-gray-500">
                            <th className="text-left py-1">Oxide</th>
                            <th className="text-right py-1">PC1</th>
                            <th className="text-right py-1">PC2</th>
                        </tr>
                    </thead>
                    <tbody className="font-mono">
                        {loadingsTable.map((r) => (
                            <tr key={r.oxide} className="border-t border-gray-800">
                                <td className="py-1 text-gray-300">{r.oxide}</td>
                                <td className={`text-right py-1 ${Math.abs(r.pc1) > 0.4 ? 'text-amber-300' : 'text-gray-400'}`}>
                                    {r.pc1.toFixed(3)}
                                </td>
                                <td className={`text-right py-1 ${Math.abs(r.pc2) > 0.4 ? 'text-amber-300' : 'text-gray-400'}`}>
                                    {r.pc2.toFixed(3)}
                                </td>
                            </tr>
                        ))}
                    </tbody>
                </table>
                <div className="mt-3 text-[10px] text-gray-500 leading-relaxed">
                    Loadings ≳ |0.4| are highlighted. Positive PC1 usually tracks felsic oxides (SiO₂, K₂O);
                    negative tracks mafic (MgO, Fe₂O₃, CaO). PC2 separates alkalis from alkaline earths.
                </div>
            </div>
        </div>
    );
}
