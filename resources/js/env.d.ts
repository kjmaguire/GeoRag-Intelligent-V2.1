/// <reference types="vite/client" />

declare module 'react-plotly.js/factory' {
    import { ComponentType } from 'react';
    function createPlotlyComponent(plotly: unknown): ComponentType<Record<string, unknown>>;
    export default createPlotlyComponent;
}

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
