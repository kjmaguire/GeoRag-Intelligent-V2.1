#!/usr/bin/env python3
"""
Master fixture generator script - runs all fixture generators.
This script can be executed independently and will generate all required test fixtures.
"""

import sys
import os
import subprocess

def run_generator(script_path, name):
    """Run a generator script and report results."""
    if not os.path.exists(script_path):
        print(f"ERROR: {name} generator not found at {script_path}")
        return False

    print(f"\n{'='*60}")
    print(f"Generating {name}...")
    print(f"{'='*60}")

    try:
        result = subprocess.run(
            [sys.executable, script_path],
            capture_output=False,
            check=True
        )
        print(f"✓ {name} generated successfully")
        return True
    except subprocess.CalledProcessError as e:
        print(f"✗ {name} generation failed: {e}")
        return False

def main():
    fixtures_dir = os.path.dirname(os.path.abspath(__file__))

    generators = [
        (os.path.join(fixtures_dir, "excel", "generate_test_xlsx.py"), "Excel fixture"),
        (os.path.join(fixtures_dir, "seismic", "generate_test_segy.py"), "SEG-Y fixture"),
        (os.path.join(fixtures_dir, "xyz", "generate_test_xyz.py"), "XYZ fixture"),
    ]

    results = []
    for script_path, name in generators:
        results.append(run_generator(script_path, name))

    # Summary
    print(f"\n{'='*60}")
    print("SUMMARY")
    print(f"{'='*60}")

    fixture_files = [
        os.path.join(fixtures_dir, "excel", "PLS_collars.xlsx"),
        os.path.join(fixtures_dir, "seismic", "test_2D_line.sgy"),
        os.path.join(fixtures_dir, "xyz", "PLS_magnetics.xyz"),
    ]

    for fixture_file in fixture_files:
        if os.path.exists(fixture_file):
            size = os.path.getsize(fixture_file)
            print(f"✓ {os.path.basename(fixture_file):30s} {size:12,d} bytes")
        else:
            print(f"✗ {os.path.basename(fixture_file):30s} NOT FOUND")

    all_success = all(results)
    return 0 if all_success else 1

if __name__ == "__main__":
    sys.exit(main())
