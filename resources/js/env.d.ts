/// <reference types="vite/client" />

declare module 'plotly.js-dist-min' {
    const Plotly: unknown;
    export default Plotly;
    export type Data = Record<string, unknown>;
    export type Layout = Record<string, unknown>;
}

declare module 'proj4' {
    function proj4(from: string, to: string, point: [number, number]): [number, number];
    function proj4(from: string, point: [number, number]): [number, number];
    namespace proj4 {
        function defs(name: string, projection: string): void;
    }
    export default proj4;
}
