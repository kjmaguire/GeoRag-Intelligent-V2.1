#!/bin/bash
# Generate binary fixtures for GeoRAG tests

set -e

PROJECT_ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
FIXTURES_DIR="$PROJECT_ROOT/tests/fixtures"

echo "Generating test fixtures..."
echo "Project root: $PROJECT_ROOT"

# Generate Excel fixture
echo ""
echo "1. Generating Excel fixture (PLS_collars.xlsx)..."
docker run --rm -v "$PROJECT_ROOT:/work" -w /work/tests/fixtures/excel \
  python:3.13-slim sh -c "pip install -q openpyxl && python generate_test_xlsx.py"

# Generate SEG-Y fixture
echo ""
echo "2. Generating SEG-Y fixture (test_2D_line.sgy)..."
docker run --rm -v "$PROJECT_ROOT:/work" -w /work/tests/fixtures/seismic \
  python:3.13-slim sh -c "pip install -q segyio numpy && python generate_test_segy.py"

# XYZ is already created as a text file, but verify
echo ""
echo "3. Verifying XYZ fixture (PLS_magnetics.xyz)..."
if [ -f "$FIXTURES_DIR/xyz/PLS_magnetics.xyz" ]; then
  SIZE=$(wc -c < "$FIXTURES_DIR/xyz/PLS_magnetics.xyz")
  LINES=$(wc -l < "$FIXTURES_DIR/xyz/PLS_magnetics.xyz")
  echo "File exists: $FIXTURES_DIR/xyz/PLS_magnetics.xyz"
  echo "Size: $SIZE bytes, Lines: $LINES"
fi

echo ""
echo "Summary:"
echo "--------"

# Report all fixture sizes
for fixture in \
  "$FIXTURES_DIR/excel/PLS_collars.xlsx" \
  "$FIXTURES_DIR/seismic/test_2D_line.sgy" \
  "$FIXTURES_DIR/xyz/PLS_magnetics.xyz"; do

  if [ -f "$fixture" ]; then
    SIZE=$(wc -c < "$fixture")
    echo "✓ $(basename "$fixture"): $SIZE bytes"
  else
    echo "✗ $(basename "$fixture"): NOT FOUND"
  fi
done

echo ""
echo "Fixtures generated successfully!"
