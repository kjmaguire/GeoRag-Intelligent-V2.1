#!/usr/bin/env python3
"""
Inline SEG-Y fixture generator - creates test_2D_line.sgy directly.
"""

import sys
import os

try:
    import segyio
    import numpy as np
except ImportError:
    print("ERROR: segyio or numpy not installed", file=sys.stderr)
    print("Install with: pip install segyio numpy", file=sys.stderr)
    sys.exit(1)

def main():
    output_path = os.path.join(
        os.path.dirname(__file__),
        "seismic",
        "test_2D_line.sgy"
    )

    # Parameters
    n_traces = 100
    n_samples = 500
    sample_interval_us = 2000
    record_length_ms = 1000

    # Create spec for 2D line
    spec = segyio.spec()
    spec.sorting = 2
    spec.format = 1
    spec.samples = list(range(n_samples))
    spec.ilines = [1]
    spec.xlines = list(range(1, n_traces + 1))

    # Generate synthetic trace
    t = np.arange(n_samples) * (sample_interval_us / 1e6)
    frequency = 50
    base_wave = np.sin(2 * np.pi * frequency * t)
    noise = np.random.normal(0, 0.05, n_samples)
    synthetic_trace = (base_wave + noise).astype(np.float32)

    # Create SEG-Y file
    with segyio.create(output_path, spec) as f:
        # Textual header
        text_header = "GeoRAG test 2D seismic line -- synthetic data for pipeline validation" + " " * 3130
        f.text[0] = segyio.create_text_header({1: text_header})

        # Binary header
        f.bin.update(
            tsort=segyio.TraceSortingFormat.INLINE_SORTING,
            hdt=sample_interval_us,
            hns=n_samples
        )

        # Write traces
        for i in range(n_traces):
            amplitude = 1.0 + (i % 10) * 0.02
            trace = synthetic_trace * amplitude

            f.trace.header[i] = {
                segyio.TraceField.TRACE_SEQUENCE_FILE: i + 1,
                segyio.TraceField.TRACE_SEQUENCE_LINE: i + 1,
                segyio.TraceField.FFID: 1,
                segyio.TraceField.TRACF: i + 1,
                segyio.TraceField.SOURCE_X: 494000 + i * 10,
                segyio.TraceField.SOURCE_Y: 6219000,
                segyio.TraceField.GroupX: 494000 + i * 10,
                segyio.TraceField.GroupY: 6219000,
                segyio.TraceField.offset: 0,
                segyio.TraceField.SAMPLE_INTERVAL: sample_interval_us,
                segyio.TraceField.ns: n_samples,
            }

            f.trace[i] = trace

    # Report
    size = os.path.getsize(output_path)
    print(f"Created: {output_path}")
    print(f"Size: {size} bytes ({size/1024:.2f} KB)")
    print(f"Traces: {n_traces}, Samples/trace: {n_samples}")
    print(f"Sample interval: {sample_interval_us} µs, Record length: {record_length_ms} ms")

if __name__ == "__main__":
    main()
