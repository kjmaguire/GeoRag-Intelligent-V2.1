#!/usr/bin/env python3
"""
Generate a test SEG-Y file with synthetic 2D seismic data.
Creates a 2D line with 100 traces and 500 samples per trace at 2ms sample interval.

Run with: python generate_test_segy.py
"""

import sys
import os

try:
    import segyio
    import numpy as np
except ImportError:
    print("ERROR: Required packages not installed.")
    print("Install with: pip install segyio numpy")
    sys.exit(1)


def generate_segy_file(output_path: str):
    """Generate a test SEG-Y file with synthetic 2D seismic data."""

    # Parameters
    n_traces = 100
    n_samples = 500
    sample_interval_us = 2000  # 2ms in microseconds
    record_length_ms = 1000  # 1 second

    # Create spec for 2D line
    spec = segyio.spec()
    spec.sorting = 2  # INLINE_SORTING for 2D
    spec.format = 1   # IBM floating point
    spec.samples = list(range(n_samples))
    spec.ilines = [1]
    spec.xlines = list(range(1, n_traces + 1))

    # Generate synthetic trace data: sine wave + noise
    t = np.arange(n_samples) * (sample_interval_us / 1e6)
    frequency = 50  # 50 Hz dominant frequency
    base_wave = np.sin(2 * np.pi * frequency * t)
    noise = np.random.normal(0, 0.05, n_samples)
    synthetic_trace = (base_wave + noise).astype(np.float32)

    # Create SEG-Y file
    with segyio.create(output_path, spec) as f:
        # Write textual header (3200 bytes)
        text_header = "GeoRAG test 2D seismic line -- synthetic data for pipeline validation" + " " * 3130
        f.text[0] = segyio.create_text_header({1: text_header})

        # Write binary reel header
        f.bin.update(
            tsort=segyio.TraceSortingFormat.INLINE_SORTING,
            hdt=sample_interval_us,
            hns=n_samples
        )

        # Write traces
        for i in range(n_traces):
            # Vary amplitude slightly per trace for realism
            amplitude = 1.0 + (i % 10) * 0.02
            trace = synthetic_trace * amplitude

            # Set trace header fields via f.header[i] (not f.trace.header)
            f.header[i] = {
                segyio.TraceField.TRACE_SEQUENCE_FILE: i + 1,
                segyio.TraceField.TRACE_SEQUENCE_LINE: i + 1,
                segyio.TraceField.SourceX: 494000 + i * 10,
                segyio.TraceField.SourceY: 6219000,
                segyio.TraceField.GroupX: 494000 + i * 10,
                segyio.TraceField.GroupY: 6219000,
                segyio.TraceField.offset: 0,
                segyio.TraceField.TRACE_SAMPLE_INTERVAL: sample_interval_us,
                segyio.TraceField.TRACE_SAMPLE_COUNT: n_samples,
            }

            # Write trace data
            f.trace[i] = trace

    # Get file size
    file_size = os.path.getsize(output_path)
    print(f"Generated {output_path}")
    print(f"Size: {file_size} bytes ({file_size / 1024:.2f} KB)")
    print(f"Content: 2D seismic line with {n_traces} traces, {n_samples} samples per trace")
    print(f"Sample interval: {sample_interval_us} µs, Record length: {record_length_ms} ms")


if __name__ == "__main__":
    output_path = os.path.join(os.path.dirname(__file__), "test_2D_line.sgy")
    generate_segy_file(output_path)
