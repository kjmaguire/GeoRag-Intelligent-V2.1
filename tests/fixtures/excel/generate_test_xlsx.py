#!/usr/bin/env python3
"""
Generate a test Excel file with drill collar data for XLS-24- project.
Uses openpyxl to create a workbook with realistic Athabasca Basin coordinates.

Run with: python generate_test_xlsx.py
"""

import sys
import os

try:
    from openpyxl import Workbook
except ImportError:
    print("ERROR: openpyxl not installed. Install with: pip install openpyxl")
    sys.exit(1)

from datetime import datetime, timedelta


def generate_xlsx_file(output_path: str):
    """Generate a test XLSX file with drill collar data."""

    wb = Workbook()
    ws = wb.active
    ws.title = "Collars"

    # Write header row
    headers = ["HoleID", "Easting", "Northing", "Elevation", "TotalDepth",
               "Azimuth", "Dip", "HoleType", "DrillDate", "Status"]
    ws.append(headers)

    # Generate 10 drill holes with XLS-24- prefix
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

    # Adjust column widths for readability
    ws.column_dimensions['A'].width = 12
    ws.column_dimensions['B'].width = 12
    ws.column_dimensions['C'].width = 12
    ws.column_dimensions['D'].width = 12
    ws.column_dimensions['E'].width = 12
    ws.column_dimensions['F'].width = 10
    ws.column_dimensions['G'].width = 10
    ws.column_dimensions['H'].width = 12
    ws.column_dimensions['I'].width = 12
    ws.column_dimensions['J'].width = 12

    # Save the workbook
    wb.save(output_path)

    # Get file size
    file_size = os.path.getsize(output_path)
    print(f"Generated {output_path}")
    print(f"Size: {file_size} bytes ({file_size / 1024:.2f} KB)")
    print(f"Content: Collars sheet with 10 drill holes (XLS-24-01 to XLS-24-10)")


if __name__ == "__main__":
    output_path = os.path.join(os.path.dirname(__file__), "PLS_collars.xlsx")
    generate_xlsx_file(output_path)
