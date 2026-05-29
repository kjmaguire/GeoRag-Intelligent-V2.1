#!/bin/bash
# Build the NI 43-101 test report PDF
set -e

cd "$(dirname "$0")"

echo "Installing reportlab..."
pip install --quiet reportlab PyPDF2

echo "Generating Patterson Lake South NI 43-101 technical report..."
python generate_test_report.py

echo ""
echo "Report generation complete."
