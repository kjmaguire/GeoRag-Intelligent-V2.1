#!/usr/bin/env python3
"""
Generate a test Geosoft XYZ file with synthetic airborne magnetic survey data.
Creates 4 flight lines with 50 points each across the Athabasca Basin.

Run with: python generate_test_xyz.py
"""

import os
import sys
import math


def generate_xyz_file(output_path: str):
    """Generate a test XYZ file with synthetic airborne magnetic survey data."""

    # Header
    lines = [
        "/ GeoRAG test XYZ -- synthetic airborne magnetic survey",
        "/ Source: Patterson Lake South Project",
        "/ CRS: UTM Zone 13N (EPSG:32613)",
        "/ Date: 2024-03-15",
        "/",
        "/ X            Y            LINE    MAG_TMI     MAG_RESID     ALT_RADAR",
    ]

    # Generate 4 flight lines with 50 points each
    flight_lines = [
        {"line_id": 1010, "start_y": 6219000},
        {"line_id": 1020, "start_y": 6219200},
        {"line_id": 1030, "start_y": 6219400},
        {"line_id": 1040, "start_y": 6219600},
    ]

    base_easting = 494000
    points_per_line = 50
    easting_increment = 10  # 10m spacing along each line
    y_increment = 0.5  # Gentle N-NE variation

    for flight_line in flight_lines:
        line_id = flight_line["line_id"]
        start_y = flight_line["start_y"]

        for i in range(points_per_line):
            x = base_easting + i * easting_increment
            y = start_y + i * y_increment

            # MAG_TMI: realistic values around 55000-56000 nT with variation
            mag_tmi = 55200 + 400 * math.sin(i / 10) + (i % 7) * 30

            # MAG_RESID: small residual values -20 to +20 nT
            mag_resid = 15 * math.cos(i / 5) - (i % 5)

            # ALT_RADAR: altitude 80-90m with slight variation
            alt_radar = 85 + 2 * math.sin(i / 8)

            # Format as whitespace-delimited data
            line = f"  {x:12.1f}   {y:12.1f}    {line_id:4d}    {mag_tmi:10.1f}     {mag_resid:8.1f}         {alt_radar:5.1f}"
            lines.append(line)

    # Write to file
    with open(output_path, 'w') as f:
        f.write('\n'.join(lines) + '\n')

    # Get file size
    file_size = os.path.getsize(output_path)
    print(f"Generated {output_path}")
    print(f"Size: {file_size} bytes ({file_size / 1024:.2f} KB)")
    print(f"Content: 4 flight lines (1010, 1020, 1030, 1040) with {points_per_line} points each")
    print(f"Total data points: {4 * points_per_line}")


if __name__ == "__main__":
    output_path = os.path.join(os.path.dirname(__file__), "PLS_magnetics.xyz")
    generate_xyz_file(output_path)
