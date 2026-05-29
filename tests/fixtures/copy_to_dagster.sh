#!/bin/bash
# Copy generated test fixtures to Dagster test directory

set -e

PROJECT_ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
FIXTURES_SRC="$PROJECT_ROOT/tests/fixtures"
DAGSTER_FIXTURES="$PROJECT_ROOT/src/dagster/tests/fixtures"

echo "Copying test fixtures to Dagster..."
echo "Source: $FIXTURES_SRC"
echo "Destination: $DAGSTER_FIXTURES"
echo ""

# Create Dagster fixtures directory structure
mkdir -p "$DAGSTER_FIXTURES/excel"
mkdir -p "$DAGSTER_FIXTURES/seismic"
mkdir -p "$DAGSTER_FIXTURES/xyz"

# Copy fixtures
echo "Copying Excel fixture..."
if [ -f "$FIXTURES_SRC/excel/PLS_collars.xlsx" ]; then
  cp "$FIXTURES_SRC/excel/PLS_collars.xlsx" "$DAGSTER_FIXTURES/excel/"
  echo "✓ Copied PLS_collars.xlsx"
else
  echo "✗ Source not found: $FIXTURES_SRC/excel/PLS_collars.xlsx"
fi

echo "Copying SEG-Y fixture..."
if [ -f "$FIXTURES_SRC/seismic/test_2D_line.sgy" ]; then
  cp "$FIXTURES_SRC/seismic/test_2D_line.sgy" "$DAGSTER_FIXTURES/seismic/"
  echo "✓ Copied test_2D_line.sgy"
else
  echo "✗ Source not found: $FIXTURES_SRC/seismic/test_2D_line.sgy"
fi

echo "Copying XYZ fixture..."
if [ -f "$FIXTURES_SRC/xyz/PLS_magnetics.xyz" ]; then
  cp "$FIXTURES_SRC/xyz/PLS_magnetics.xyz" "$DAGSTER_FIXTURES/xyz/"
  echo "✓ Copied PLS_magnetics.xyz"
else
  echo "✗ Source not found: $FIXTURES_SRC/xyz/PLS_magnetics.xyz"
fi

echo ""
echo "Done! Dagster fixtures copied to:"
echo "  $DAGSTER_FIXTURES/"
ls -lh "$DAGSTER_FIXTURES"/*/*
