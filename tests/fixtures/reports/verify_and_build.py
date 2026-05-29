#!/usr/bin/env python3
"""
Verify dependencies and build the PDF report.
"""
import sys
import subprocess
import os

print(f"Python version: {sys.version}")
print(f"Current working directory: {os.getcwd()}")

# Try importing required modules
missing = []
for module in ['reportlab', 'PyPDF2']:
    try:
        __import__(module)
        print(f"✓ {module} available")
    except ImportError:
        print(f"✗ {module} missing — installing...")
        missing.append(module)

if missing:
    print(f"\nInstalling: {', '.join(missing)}")
    subprocess.check_call([sys.executable, '-m', 'pip', 'install', '--quiet'] + missing)
    print("Installation complete.\n")

# Now import and run the generator
from generate_test_report import generate_report

print("Building PDF...\n")
generate_report()
