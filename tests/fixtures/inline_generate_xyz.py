#!/usr/bin/env python3
"""
Inline XYZ fixture generator - creates PLS_magnetics.xyz directly.
No external dependencies.
"""

import os
import math

def main():
    output_path = os.path.join(
        os.path.dirname(__file__),
        "xyz",
        "PLS_magnetics.xyz"
    )

    # Header
    lines = [
        "/ GeoRAG test XYZ -- synthetic airborne magnetic survey",
        "/ Source: Patterson Lake South Project",
        "/ CRS: UTM Zone 13N (EPSG:32613)",
        "/ Date: 2024-03-15",
        "/",
        "/ X            Y            LINE    MAG_TMI     MAG_RESID     ALT_RADAR",
    ]

    # Generate 4 flight lines
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

    # Write file
    with open(output_path, 'w') as f:
        f.write('\n'.join(lines) + '\n')

    # Report
    size = os.path.getsize(output_path)
    print(f"Created: {output_path}")
    print(f"Size: {size} bytes ({size/1024:.2f} KB)")
    print(f"Flight lines: 4 (1010, 1020, 1030, 1040)")
    print(f"Points per line: {points_per_line}")
    print(f"Total data points: {4 * points_per_line}")

if __name__ == "__main__":
    main()
