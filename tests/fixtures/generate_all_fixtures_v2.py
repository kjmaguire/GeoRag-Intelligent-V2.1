#!/usr/bin/env python3
"""
Comprehensive test fixture generator for GeoRAG.
Generates all three binary fixtures with proper error handling.

Usage:
    python generate_all_fixtures_v2.py

Requirements:
    - openpyxl (for Excel)
    - segyio, numpy (for SEG-Y)
    - XYZ generator has no external dependencies

Install requirements with:
    pip install openpyxl segyio numpy
"""

import sys
import os
from datetime import datetime, timedelta
import math


def generate_excel_fixture():
    """Generate Excel fixture: PLS_collars.xlsx"""
    try:
        from openpyxl import Workbook
    except ImportError:
        print("ERROR: openpyxl required for Excel fixture")
        print("  Install with: pip install openpyxl")
        return False

    try:
        output_path = os.path.join(
            os.path.dirname(__file__),
            "excel",
            "PLS_collars.xlsx"
        )

        # Create directory if needed
        os.makedirs(os.path.dirname(output_path), exist_ok=True)

        wb = Workbook()
        ws = wb.active
        ws.title = "Collars"

        # Headers
        headers = ["HoleID", "Easting", "Northing", "Elevation", "TotalDepth",
                   "Azimuth", "Dip", "HoleType", "DrillDate", "Status"]
        ws.append(headers)

        # Data
        base_date = datetime(2024, 1, 15)
        base_easting = 497000
        base_northing = 6219000

        for i in range(1, 11):
            hole_id = f"XLS-24-{i:02d}"
            easting = base_easting + (i - 1) * 100
            northing = base_northing + (i - 1) * 100
            elevation = 420 + (i % 5) * 2
            total_depth = 280 + (i % 8) * 25
            azimuth = 75 + (i % 3) * 10
            dip = -82 - (i % 7)
            hole_type = "Diamond"
            drill_date = (base_date + timedelta(days=(i-1) * 14)).strftime("%Y-%m-%d")
            status = "Completed"

            ws.append([
                hole_id, easting, northing, elevation, total_depth,
                azimuth, dip, hole_type, drill_date, status
            ])

        # Column widths
        for col in ['A', 'B', 'C', 'D', 'E', 'I', 'J']:
            ws.column_dimensions[col].width = 12
        for col in ['F', 'G']:
            ws.column_dimensions[col].width = 10
        ws.column_dimensions['H'].width = 12

        # Save
        wb.save(output_path)

        size = os.path.getsize(output_path)
        print(f"✓ Excel fixture: {output_path}")
        print(f"  Size: {size:,} bytes ({size/1024:.2f} KB)")
        return True

    except Exception as e:
        print(f"✗ Excel fixture generation failed: {e}")
        return False


def generate_segy_fixture():
    """Generate SEG-Y fixture: test_2D_line.sgy"""
    try:
        import segyio
        import numpy as np
    except ImportError:
        print("ERROR: segyio and numpy required for SEG-Y fixture")
        print("  Install with: pip install segyio numpy")
        return False

    try:
        output_path = os.path.join(
            os.path.dirname(__file__),
            "seismic",
            "test_2D_line.sgy"
        )

        # Create directory if needed
        os.makedirs(os.path.dirname(output_path), exist_ok=True)

        # Parameters
        n_traces = 100
        n_samples = 500
        sample_interval_us = 2000

        # Spec
        spec = segyio.spec()
        spec.sorting = 2
        spec.format = 1
        spec.samples = list(range(n_samples))
        spec.ilines = [1]
        spec.xlines = list(range(1, n_traces + 1))

        # Synthetic data
        t = np.arange(n_samples) * (sample_interval_us / 1e6)
        frequency = 50
        base_wave = np.sin(2 * np.pi * frequency * t)
        noise = np.random.normal(0, 0.05, n_samples)
        synthetic_trace = (base_wave + noise).astype(np.float32)

        # Create file
        with segyio.create(output_path, spec) as f:
            text_header = "GeoRAG test 2D seismic line -- synthetic data for pipeline validation" + " " * 3130
            f.text[0] = segyio.create_text_header({1: text_header})

            f.bin.update(
                tsort=segyio.TraceSortingFormat.INLINE_SORTING,
                hdt=sample_interval_us,
                hns=n_samples
            )

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

        size = os.path.getsize(output_path)
        print(f"✓ SEG-Y fixture: {output_path}")
        print(f"  Size: {size:,} bytes ({size/1024:.2f} KB)")
        print(f"  Traces: {n_traces}, Samples: {n_samples}")
        return True

    except Exception as e:
        print(f"✗ SEG-Y fixture generation failed: {e}")
        return False


def generate_xyz_fixture():
    """Generate XYZ fixture: PLS_magnetics.xyz"""
    try:
        output_path = os.path.join(
            os.path.dirname(__file__),
            "xyz",
            "PLS_magnetics.xyz"
        )

        # Create directory if needed
        os.makedirs(os.path.dirname(output_path), exist_ok=True)

        lines = [
            "/ GeoRAG test XYZ -- synthetic airborne magnetic survey",
            "/ Source: Patterson Lake South Project",
            "/ CRS: UTM Zone 13N (EPSG:32613)",
            "/ Date: 2024-03-15",
            "/",
            "/ X            Y            LINE    MAG_TMI     MAG_RESID     ALT_RADAR",
        ]

        flight_lines = [
            {"line_id": 1010, "start_y": 6219000},
            {"line_id": 1020, "start_y": 6219200},
            {"line_id": 1030, "start_y": 6219400},
            {"line_id": 1040, "start_y": 6219600},
        ]

        base_easting = 494000
        points_per_line = 50
        easting_increment = 10
        y_increment = 0.5

        for flight_line in flight_lines:
            line_id = flight_line["line_id"]
            start_y = flight_line["start_y"]

            for i in range(points_per_line):
                x = base_easting + i * easting_increment
                y = start_y + i * y_increment

                mag_tmi = 55200 + 400 * math.sin(i / 10) + (i % 7) * 30
                mag_resid = 15 * math.cos(i / 5) - (i % 5)
                alt_radar = 85 + 2 * math.sin(i / 8)

                line = f"  {x:12.1f}   {y:12.1f}    {line_id:4d}    {mag_tmi:10.1f}     {mag_resid:8.1f}         {alt_radar:5.1f}"
                lines.append(line)

        with open(output_path, 'w') as f:
            f.write('\n'.join(lines) + '\n')

        size = os.path.getsize(output_path)
        print(f"✓ XYZ fixture: {output_path}")
        print(f"  Size: {size:,} bytes ({size/1024:.2f} KB)")
        print(f"  Flight lines: 4, Points: {4 * points_per_line}")
        return True

    except Exception as e:
        print(f"✗ XYZ fixture generation failed: {e}")
        return False


def main():
    print("=" * 70)
    print("GeoRAG Test Fixture Generator")
    print("=" * 70)
    print()

    results = {
        "Excel": generate_excel_fixture(),
        "SEG-Y": generate_segy_fixture(),
        "XYZ": generate_xyz_fixture(),
    }

    print()
    print("=" * 70)
    print("Summary")
    print("=" * 70)

    all_success = all(results.values())
    for name, success in results.items():
        status = "✓ PASS" if success else "✗ FAIL"
        print(f"{status}: {name} fixture")

    print()
    if all_success:
        print("All fixtures generated successfully!")
        return 0
    else:
        print("Some fixtures failed. Check error messages above.")
        return 1


if __name__ == "__main__":
    sys.exit(main())
