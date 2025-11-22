# Project Parent Report - Data Structure

## Overview
The new endpoint `/project-parents/{parent_id}/report` generates a comprehensive test report that includes ALL specimens (projects) under a project parent.

## Data Structure Available for Report Generation

```python
report_data = {
    'project_parent': {
        'id': int,
        'name': str  # e.g., "Hurricane Impact Window System - Series 2024A"
    },
    'specimens': [
        {
            'id': int,
            'name': str,  # e.g., "Specimen A", "Specimen B", "Specimen C"
            'inward_design_pressure': float,  # PSF
            'outward_design_pressure': float,  # PSF
            'device': {
                'id': int,
                'name': str,
                'turbo_mode': bool,
                'turbo_slave': bool
            },
            'static_tests': [
                {
                    'id': int,
                    'index': int,  # 0-5
                    'type': str,  # 'inward' or 'outward'
                    'pressure': float,  # PSF
                    'pressure_factor': str,  # e.g., 'Structural Pressure'
                    'duration': int,  # seconds
                    'finished': bool,
                    'trials': [
                        {
                            'trial_number': int,
                            'result': bool,  # True = Passed, False = Failed
                            'note': str,
                            'image_path': str,
                            'deflections': [
                                {
                                    'gauge': str,  # e.g., "Gauge 1", "Gauge 2"
                                    'max_deflection': float,  # inches
                                    'permanent_deflection': float,  # inches
                                    'recovery': float  # inches
                                }
                            ]
                        }
                    ]
                }
            ],
            'cyclic_tests': [
                {
                    'id': int,
                    'index': int,  # 0-7 (0-3 inward, 4-7 outward)
                    'type': str,  # 'inward' or 'outward'
                    'cycles': int,  # e.g., 3500, 300, 600, etc.
                    'low_pressure': float,  # PSF
                    'high_pressure': float,  # PSF
                    'current_cycle': int,
                    'finished': bool,
                    'trials': [
                        {
                            'trial_number': int,
                            'result': bool,
                            'note': str,
                            'image_path': str,
                            'deflections': [
                                {
                                    'gauge': str,
                                    'max_deflection': float,
                                    'permanent_deflection': float,
                                    'recovery': float
                                }
                            ]
                        }
                    ]
                }
            ],
            'infiltration_tests': [
                {
                    'id': int,
                    'type': str,  # e.g., "Water Infiltration", "Air Infiltration"
                    'pressure': float,  # PSF
                    'duration': float,  # minutes
                    'leakage': float  # cfm/ft²
                }
            ],
            'missile_impact_tests': [
                {
                    'id': int,
                    'missile': str,  # e.g., "2x4 Lumber", "Steel Ball"
                    'missile_weight': float,  # kg
                    'shots': [
                        {
                            'area': float,  # square meters
                            'velocity': float,  # m/s
                            'result': bool,  # True = Passed
                            'note': str
                        }
                    ]
                }
            ]
        }
    ]
}
```

## Test Sequence Mapping (Based on IFET Report Format)

### 1. Impact Test (ANSI 297.1)
- Uses: `missile_impact_tests[].shots[]`
- Result format: "Glass passed the impact test without hazardous breakage"

### 2. Air Leakage (ASTM E283/E2357-18)
- Uses: `infiltration_tests[]` where `type == "Air Infiltration"`
- Shows: pressure, measured flow (leakage), result

### 3. Static Pressure (TAS 202-94) - 1/2 Structural Pressure and Design Pressure
- Uses: `static_tests[]` filtered by index/pressure level
- Shows: Test, Pressure, Deflection Gauge (1,2,3), Deflection (in.), Permanent Set (in.)
- Indexes:
  - 0: 1/2 Structural Load (Inward) - 0.75x inward design pressure
  - 1: 1/2 Structural Load (Outward) - 0.75x outward design pressure  
  - 2: Design Pressure (Inward) - 1.0x inward design pressure
  - 3: Design Pressure (Outward) - 1.0x outward design pressure

### 4. Water Infiltration Test (ASTM E331)
- Uses: `infiltration_tests[]` where `type == "Water Infiltration"`
- Shows: Test Pressure, Test Duration, Water Leakage, Results

### 5. Structural Load (TAS 202) - 1.5x Design Pressure
- Uses: `static_tests[]` filtered by highest pressure
- Indexes:
  - 4: 1.5x Structural Load (Inward)
  - 5: 1.5x Structural Load (Outward)
- Shows: Test, Pressure, Deflection Gauge, Deflection, Permanent Set

### 6. Forced Entry Resistance Test (ASTM F588)
- Note: Not currently in data model - may need to add

### 7. Large/Small Missile Impact Test (TAS 201-94)
- Uses: `missile_impact_tests[]`
- Shows: Missile type, weight, Impact #, Velocity, Results, Notes

### 8. Cyclic Pressure Test (TAS 203-94)
- Uses: `cyclic_tests[]`
- Positive Pressure Range (Inward): indexes 0-3
  - 3500 cycles: 0.2 to 0.5 x design pressure
  - 300 cycles: 0.0 to 0.6 x design pressure
  - 600 cycles: 0.5 to 0.8 x design pressure
  - 100 cycles: 0.3 to 1.0 x design pressure
- Negative Pressure Range (Outward): indexes 4-7
  - 50 cycles: 0.3 to 1.0 x design pressure
  - 1050 cycles: 0.5 to 0.8 x design pressure
  - 50 cycles: 0.0 to 0.6 x design pressure
  - 3350 cycles: 0.2 to 0.5 x design pressure

## API Endpoints

### New Endpoint (Use this for reports)
```
GET /project-parents/{parent_id}/report
```
Returns: PDF file with comprehensive test report for all specimens

### Legacy Endpoint (Still available)
```
GET /projects/{project_id}/report
```
Returns: PDF file for a single specimen

## Next Steps

Once you provide the start and end pages of the report template, I'll:
1. Update `pdf_utils.py` to handle the new `report_data` structure
2. Format the report to match IFET-LAF-05 format
3. Generate proper headers, footers, and page structure
4. Include all specimens in a single comprehensive report

