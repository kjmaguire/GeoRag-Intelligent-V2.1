#!/usr/bin/env python3
"""
Generate a realistic LAS 2.0 well log file for test fixture PLS-22-08.
Contains three curves (DEPTH, GR, RHOB) with high-grade uranium intercept at 398-401m.
"""

import math

def generate_las_file(output_path: str, well_id: str = "PLS-22-08"):
    """Generate a test LAS 2.0 file with realistic well log data."""

    start_depth = 0.0
    stop_depth = 510.0
    step = 0.1
    null_value = -999.25

    # Generate depth samples and curves
    depths = []
    gr_values = []
    rhob_values = []

    depth = start_depth
    while depth <= stop_depth:
        depths.append(depth)

        # Gamma Ray (GR) in API units
        # Baseline 50-100 API, with high-grade uranium spike at 398-401m
        if 398 <= depth <= 401:
            # High-grade uranium intercept: spike to 2500-3200 API
            gr = 2500 + 350 * math.sin((depth - 398) * math.pi / 3)
        else:
            # Baseline with noise
            gr = 70 + 20 * math.sin(depth / 50) + (hash(depth) % 20 - 10) / 5

        gr_values.append(max(0, gr))

        # Bulk Density (RHOB) in g/cc
        # Typical basement rocks: 2.4-2.7, slight increase with depth
        rhob = 2.45 + 0.15 * (depth / 500) + (hash(depth * 1000) % 10 - 5) / 100
        rhob_values.append(rhob)

        depth += step

    # Build LAS file content
    las_content = f"""~V
VERS.              2.0:   CWLS Log ASCII Standard - VERSION 2.0
WRAP.               NO:   One Line per Depth Step

~W
STRT.M           {start_depth:>8.4f}:  Start Depth
STOP.M         {stop_depth:>8.4f}:  Stop Depth
STEP.M           {step:>8.4f}:  Step
NULL.          {null_value:>8.4f}:  Null Value
COMP.   Fission Uranium Corp:   Company
WELL.   {well_id}:   Well Name
FLD .   Patterson Lake South:   Field
LOC .   Athabasca Basin:   Location
PROV.   Saskatchewan:   Province
CTRY.   Canada:   Country
DATE.   2022-02-14:   Date

~C
DEPT.M      : Depth
GR  .GAPI   : Gamma Ray
RHOB.G/CC   : Bulk Density

~A
"""

    # Append ASCII data section
    for i, depth in enumerate(depths):
        gr = gr_values[i]
        rhob = rhob_values[i]
        las_content += f"{depth:10.4f} {gr:10.2f} {rhob:10.4f}\n"

    # Write to file
    with open(output_path, 'w') as f:
        f.write(las_content)

    print(f"Generated {output_path}: {len(depths)} samples, depth range {start_depth}-{stop_depth}m")

if __name__ == "__main__":
    import os
    output_path = os.path.join(os.path.dirname(__file__), "PLS-22-08_gamma_resistivity.las")
    generate_las_file(output_path)
