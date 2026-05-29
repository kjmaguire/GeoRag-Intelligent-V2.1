#!/usr/bin/env python3
"""
Inline Excel fixture generator - creates PLS_collars.xlsx directly.
No external dependencies beyond openpyxl.
"""

import sys
import os
from datetime import datetime, timedelta

# Ensure openpyxl is available
try:
    from openpyxl import Workbook
except ImportError:
    print("ERROR: openpyxl not installed", file=sys.stderr)
    sys.exit(1)

def main():
    output_path = os.path.join(
        os.path.dirname(__file__),
        "excel",
        "PLS_collars.xlsx"
    )

    # Create workbook
    wb = Workbook()
    ws = wb.active
    ws.title = "Collars"

    # Write headers
    headers = ["HoleID", "Easting", "Northing", "Elevation", "TotalDepth",
               "Azimuth", "Dip", "HoleType", "DrillDate", "Status"]
    ws.append(headers)

    # Generate 10 drill holes
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
            hole_id,
            easting,
            northing,
            elevation,
            total_depth,
            azimuth,
            dip,
            hole_type,
            drill_date,
            status
        ])

    # Adjust column widths
    for col in ['A', 'B', 'C', 'D', 'E', 'I', 'J']:
        ws.column_dimensions[col].width = 12
    for col in ['F', 'G']:
        ws.column_dimensions[col].width = 10
    ws.column_dimensions['H'].width = 12

    # Save
    wb.save(output_path)

    # Report
    size = os.path.getsize(output_path)
    print(f"Created: {output_path}")
    print(f"Size: {size} bytes ({size/1024:.2f} KB)")
    print(f"Rows: 11 (1 header + 10 data)")

if __name__ == "__main__":
    main()
