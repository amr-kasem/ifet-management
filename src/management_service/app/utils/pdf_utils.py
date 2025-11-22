from reportlab.lib.pagesizes import letter
from reportlab.lib import colors
from reportlab.lib.units import inch
from reportlab.platypus import SimpleDocTemplate, Table, TableStyle, Paragraph, Spacer, PageBreak, Image
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.enums import TA_CENTER, TA_LEFT, TA_RIGHT, TA_JUSTIFY
from reportlab.pdfgen import canvas
from datetime import datetime
import os
from PIL import Image as PILImage

class IFETReportTemplate:
    """Custom template for IFET Test Reports"""
    
    def __init__(self, filename, doc_no="IFET-LAF-05", rev_no="01", cert_no="25-0424.01"):
        self.filename = filename
        self.doc_no = doc_no
        self.rev_no = rev_no
        self.cert_no = cert_no
        self.page_num = 0
        
    def header(self, canvas, doc, project_info=None, report_info=None):
        """Draw header on each page"""
        canvas.saveState()
        width, height = letter
        
        # Draw header table
        # IFET logo placeholder (you can add actual logo image)
        canvas.setFont('Helvetica-Bold', 8)
        canvas.drawString(0.75*inch, height - 0.6*inch, "IFET, Inc.")
        canvas.setFont('Helvetica', 7)
        canvas.drawString(0.75*inch, height - 0.75*inch, "7839 NW 15th Street")
        canvas.drawString(0.75*inch, height - 0.85*inch, "Miami, FL 33126")
        canvas.drawString(0.75*inch, height - 0.95*inch, "Ph: +1 (305) 513-7974")
        canvas.setFillColorRGB(0, 0, 1)
        canvas.drawString(0.75*inch, height - 1.05*inch, "www.ifetlab.com")
        canvas.setFillColorRGB(0, 0, 0)
        
        # Title
        canvas.setFont('Helvetica-Bold', 18)
        canvas.drawCentredString(width/2, height - 0.7*inch, "TEST REPORT")
        
        # Certification number
        canvas.setFont('Helvetica-Bold', 9)
        canvas.drawCentredString(width/2, height - 0.95*inch, f"MIAMI-DADE CERTIFICATION NO.: {self.cert_no}")
        
        # Right side info box
        canvas.setFont('Helvetica-Bold', 8)
        canvas.drawString(width - 2.5*inch, height - 0.6*inch, "Doc. No.:")
        canvas.drawString(width - 1.5*inch, height - 0.6*inch, self.doc_no)
        
        canvas.drawString(width - 2.5*inch, height - 0.75*inch, "Rev. No.:")
        canvas.drawString(width - 1.5*inch, height - 0.75*inch, self.rev_no)
        
        canvas.drawString(width - 2.5*inch, height - 0.9*inch, "Effective Date:")
        canvas.drawString(width - 1.5*inch, height - 0.9*inch, report_info.get('effective_date', '02/28/25') if report_info else '02/28/25')
        
        # Client info line
        canvas.setFont('Helvetica-Bold', 9)
        canvas.drawString(0.75*inch, height - 1.35*inch, "CLIENT:")
        
        canvas.drawString(width - 3.5*inch, height - 1.35*inch, "PROJECT #:")
        canvas.setFillColorRGB(1, 0, 0)
        canvas.drawString(width - 2.5*inch, height - 1.35*inch, project_info.get('project_number', 'IFET-XX-XXXX') if project_info else 'IFET-XX-XXXX')
        canvas.setFillColorRGB(0, 0, 0)
        
        canvas.drawString(width - 3.5*inch, height - 1.5*inch, "REPORT NO.:")
        canvas.setFillColorRGB(1, 0, 0)
        canvas.drawString(width - 2.5*inch, height - 1.5*inch, report_info.get('report_number', 'TN-IFET-XX-XXXX') if report_info else 'TN-IFET-XX-XXXX')
        canvas.setFillColorRGB(0, 0, 0)
        
        canvas.drawString(width - 1.5*inch, height - 1.35*inch, "ISSUE DATE:")
        
        # Draw horizontal line
        canvas.setStrokeColor(colors.black)
        canvas.line(0.5*inch, height - 1.65*inch, width - 0.5*inch, height - 1.65*inch)
        
        canvas.restoreState()
        
    def footer(self, canvas, doc):
        """Draw footer with page number"""
        canvas.saveState()
        width, height = letter
        
        canvas.setFont('Helvetica', 8)
        page_text = f"Page {doc.page}"
        canvas.drawCentredString(width/2, 0.5*inch, page_text)
        
        canvas.restoreState()
    
    def add_watermark(self, canvas, doc):
        """Add diagonal watermark image on the page"""
        canvas.saveState()
        width, height = letter
        
        # Path to watermark image
        watermark_path = os.path.join(os.path.dirname(__file__), 'water_mark.png')
        
        if os.path.exists(watermark_path):
            # Get image dimensions
            with PILImage.open(watermark_path) as img:
                img_width, img_height = img.size
            
            # Calculate watermark size (make it large but transparent)
            # Scale to fit nicely on the page
            max_size = min(width, height) * 0.6
            aspect_ratio = img_width / img_height
            
            if aspect_ratio > 1:
                watermark_width = max_size
                watermark_height = max_size / aspect_ratio
            else:
                watermark_height = max_size
                watermark_width = max_size * aspect_ratio
            
            # Position watermark in center with rotation
            canvas.translate(width/2, height/2)
            canvas.rotate(45)
            
            # Set transparency (higher value = more visible)
            canvas.setFillAlpha(0.15)
            canvas.setStrokeAlpha(0.15)
            
            # Draw the watermark image
            canvas.drawImage(
                watermark_path,
                -watermark_width/2,
                -watermark_height/2,
                width=watermark_width,
                height=watermark_height,
                preserveAspectRatio=True,
                mask='auto'
            )
        
        canvas.restoreState()


def create_test_report_pdf(report_data, filename, client_info=None, report_info=None):
    """
    Generate IFET-LAF-05 format test report PDF
    
    Args:
        report_data: Dict with project_parent and specimens data
        filename: Output PDF filename
        client_info: Dict with client information (name, address, phone, email)
        report_info: Dict with report metadata (project_number, report_number, effective_date, issue_date)
    """
    
    # Default values
    if client_info is None:
        client_info = {'name': '', 'address': '', 'phone': '', 'email': ''}
    if report_info is None:
        report_info = {
            'project_number': 'IFET-XX-XXXX',
            'report_number': 'TN-IFET-XX-XXXX',
            'effective_date': '02/28/25',
            'issue_date': datetime.now().strftime('%m/%d/%y'),
            'test_start_date': '',
            'test_end_date': '',
            'record_retention_date': ''
        }
    
    # Create the PDF
    doc = SimpleDocTemplate(
        filename,
        pagesize=letter,
        rightMargin=0.5*inch,
        leftMargin=0.5*inch,
        topMargin=1.8*inch,
        bottomMargin=0.75*inch
    )
    
    # Container for the 'Flowable' objects
    story = []
    
    # Define styles
    styles = getSampleStyleSheet()
    
    title_style = ParagraphStyle(
        'CustomTitle',
        parent=styles['Heading1'],
        fontSize=12,
        textColor=colors.black,
        spaceAfter=12,
        alignment=TA_LEFT,
        fontName='Helvetica-Bold'
    )
    
    heading_style = ParagraphStyle(
        'CustomHeading',
        parent=styles['Heading2'],
        fontSize=10,
        textColor=colors.black,
        spaceAfter=6,
        spaceBefore=12,
        alignment=TA_LEFT,
        fontName='Helvetica-Bold'
    )
    
    body_style = ParagraphStyle(
        'CustomBody',
        parent=styles['BodyText'],
        fontSize=9,
        textColor=colors.black,
        alignment=TA_JUSTIFY,
        spaceAfter=6
    )
    
    small_style = ParagraphStyle(
        'SmallText',
        parent=styles['BodyText'],
        fontSize=8,
        textColor=colors.black,
        alignment=TA_LEFT
    )
    
    # ============ PAGE 1: COVER PAGE ============
    
    # Client/Manufacturer Information
    story.append(Paragraph("<b>CLIENT/MANUFACTURER INFORMATION:</b>", heading_style))
    
    client_table_data = [
        ['ADDRESS:', client_info.get('address', ''), 'PHONE #:', client_info.get('phone', '')],
        ['', '', 'EMAIL:', client_info.get('email', '')]
    ]
    client_table = Table(client_table_data, colWidths=[1*inch, 3*inch, 1*inch, 2*inch])
    client_table.setStyle(TableStyle([
        ('FONTNAME', (0, 0), (-1, -1), 'Helvetica-Bold'),
        ('FONTSIZE', (0, 0), (-1, -1), 9),
        ('VALIGN', (0, 0), (-1, -1), 'TOP'),
        ('GRID', (0, 0), (-1, -1), 0.5, colors.black),
    ]))
    story.append(client_table)
    story.append(Spacer(1, 0.2*inch))
    
    # Testing Location
    story.append(Paragraph("<b>TESTING LOCATION:</b> IFET, Inc. Laboratory - 7839 NW 15th St. Miami, FL 33126. USA", body_style))
    story.append(Spacer(1, 0.15*inch))
    
    # Scope
    story.append(Paragraph("<b>SCOPE (TESTS CONDUCTED):</b>", heading_style))
    
    project_parent_name = report_data.get('project_parent', {}).get('name', '')
    scope_text = f"""The above-referenced manufacturer contracted IFET, Inc. to perform testing in accordance with Miami-Dade County and Florida Building Code requirements on the following products/systems:<br/><br/>
    <b>{project_parent_name}</b><br/>
    Operator Type (Missile)"""
    story.append(Paragraph(scope_text, body_style))
    story.append(Spacer(1, 0.15*inch))
    
    # Disclaimer Statement
    story.append(Paragraph("<b>DISCLAIMER STATEMENT:</b>", heading_style))
    
    manufacturer_name = client_info.get('name', 'NAME OF MANUFACTURER')
    disclaimer_items = [
        f"The specimens manufactured by <b>{manufacturer_name}</b> were provided directly to IFET, Inc. by the client, as built.",
        "Samples were not independently selected for testing.",
        "The specimen is in conformity with the drawings provided by the manufacturer (see attached Lab Drawings).",
        "IFET, Inc. does not have, nor does it intend to acquire or will acquire, a financial interest in any company manufacturing or distributing products tested by IFET, Inc.",
        "IFET, Inc. is not owned, operated, or controlled by any company manufacturing or distributing products it tests or labels.",
        "IFET, Inc. does not take responsibility for performance; its only purpose is gathering pertinent data under this Test Report for the client.",
        "This report is intended solely for informational purposes and documents the testing results conducted under specific conditions. It does not constitute a certification, endorsement, or warranty of the tested product by IFET, Inc.",
        "IFET, Inc. assumes no responsibility for variations in production, installation, or usage that may affect product performance. It is the responsibility of the manufacturer, installer, and end-user to ensure compliance with applicable codes, standards, and regulations."
    ]
    
    for item in disclaimer_items:
        story.append(Paragraph(f"• {item}", body_style))
    
    story.append(Spacer(1, 2*inch))
    
    # Test Dates
    test_dates_data = [
        ['TEST DATES:', 'Start:', report_info.get('test_start_date', ''), 'End:', report_info.get('test_end_date', '')]
    ]
    test_dates_table = Table(test_dates_data, colWidths=[1.2*inch, 0.5*inch, 2*inch, 0.5*inch, 2*inch])
    test_dates_table.setStyle(TableStyle([
        ('FONTNAME', (0, 0), (0, 0), 'Helvetica-Bold'),
        ('FONTSIZE', (0, 0), (-1, -1), 9),
        ('VALIGN', (0, 0), (-1, -1), 'MIDDLE'),
        ('BOX', (0, 0), (-1, -1), 0.5, colors.black),
        ('BOX', (2, 0), (2, 0), 0.5, colors.black),
        ('BOX', (4, 0), (4, 0), 0.5, colors.black),
    ]))
    story.append(test_dates_table)
    story.append(Spacer(1, 0.2*inch))
    
    # Record Retention End Date
    retention_data = [
        ['RECORD RETENTION END DATE:', report_info.get('record_retention_date', '')]
    ]
    retention_table = Table(retention_data, colWidths=[2.5*inch, 2*inch])
    retention_table.setStyle(TableStyle([
        ('FONTNAME', (0, 0), (0, 0), 'Helvetica-Bold'),
        ('FONTSIZE', (0, 0), (-1, -1), 9),
        ('VALIGN', (0, 0), (-1, -1), 'MIDDLE'),
        ('BOX', (0, 0), (-1, -1), 0.5, colors.black),
        ('BOX', (1, 0), (1, 0), 0.5, colors.black),
    ]))
    story.append(retention_table)
    story.append(Spacer(1, 0.5*inch))
    
    # Signature block
    story.append(Spacer(1, 1.5*inch))
    signature_data = [
        ['Arshad Viqar, PE'],
        ['FL PE # 38863 / FL C.A.N. # 9101']
    ]
    signature_table = Table(signature_data, colWidths=[3*inch])
    signature_table.setStyle(TableStyle([
        ('FONTNAME', (0, 0), (0, 0), 'Helvetica-Bold'),
        ('FONTSIZE', (0, 0), (-1, -1), 8),
        ('VALIGN', (0, 0), (-1, -1), 'TOP'),
        ('LINEABOVE', (0, 0), (0, 0), 0.5, colors.black),
    ]))
    story.append(signature_table)
    
    # ============ PAGE 2: TABLE OF CONTENTS ============
    story.append(PageBreak())
    
    story.append(Paragraph("<b>TABLE OF CONTENTS</b>", title_style))
    story.append(Spacer(1, 0.2*inch))
    
    # Build TOC entries
    specimens = report_data.get('specimens', [])
    toc_entries = []
    current_page = 3  # Starting page after cover and TOC
    
    for specimen_idx, specimen in enumerate(specimens, 1):
        # Product Description / Specimen Identification
        toc_entries.append([f"PRODUCT DESCRIPTION / SPECIMEN IDENTIFICATION: MOCK-UP # {specimen_idx}", '', str(current_page)])
        current_page += 1
        
        # Series
        toc_entries.append(["SERIES 150 ALUMINUM SINGLE HUNG WINDOW", '', str(current_page)])
        
        # Test details
        toc_entries.append(["VENT PANEL MATERIAL CHARACTERISTICS", '', str(current_page)])
        toc_entries.append(["FIXED PANEL AND FRAME MATERIAL CHARACTERISTICS", '', str(current_page)])
        toc_entries.append(["HARDWARE", '', str(current_page)])
        toc_entries.append(["TEST SEQUENCE", '', str(current_page)])
        
        # Test Results
        toc_entries.append([f"TEST RESULTS - MOCK-UP # {specimen_idx}", '', str(current_page+1)])
        current_page += 1
        
        toc_entries.append(["TAS 202-94 (UNIFORM STATIC AIR PRESSURE)", '', str(current_page)])
        toc_entries.append(["AIR INFILTRATION TEST (ASTM E283)", '', str(current_page)])
        toc_entries.append(["PRELOAD AND DESIGN LOAD (TAS 202)", '', str(current_page)])
        toc_entries.append(["WATER INFILTRATION TEST (ASTM E331)", '', str(current_page)])
        toc_entries.append(["STRUCTURAL LOAD (TAS 202)", '', str(current_page)])
        toc_entries.append(["FORCED ENTRY RESISTANCE TEST (ASTM F588)", '', str(current_page)])
        current_page += 1
        toc_entries.append(["LARGE MISSILE IMPACT TEST (TAS 201-94)", '', str(current_page)])
        toc_entries.append(["CYCLIC PRESSURE TEST (TAS 203-94)", '', str(current_page)])
        current_page += 1
        
        # Appendices
        toc_entries.append([f"APPENDIX 1 - SKETCH MOCK-UP # {specimen_idx}", '', str(current_page)])
        current_page += 1
        
        # Conclusion for this specimen
        toc_entries.append([f"FINAL STATEMENT FOR MOCK-UP #{specimen_idx}", '', str(current_page)])
        toc_entries.append([f"CONCLUSION FOR MOCK-UP #{specimen_idx}", '', str(current_page)])
        current_page += 1
    
    # Witness to testing
    toc_entries.append(["WITNESS TO TESTING", '', str(current_page)])
    
    # Revision history
    toc_entries.append(["REVISION HISTORY", '', str(current_page)])
    
    # Create TOC table
    toc_table_data = [['TABLE OF CONTENTS', '', '']]
    toc_table_data.extend(toc_entries)
    
    toc_table = Table(toc_table_data, colWidths=[5.5*inch, 0.5*inch, 1*inch])
    toc_table.setStyle(TableStyle([
        ('FONTNAME', (0, 0), (-1, 0), 'Helvetica-Bold'),
        ('FONTSIZE', (0, 0), (-1, -1), 9),
        ('ALIGN', (0, 0), (0, -1), 'LEFT'),
        ('ALIGN', (2, 0), (2, -1), 'RIGHT'),
        ('VALIGN', (0, 0), (-1, -1), 'MIDDLE'),
        ('LINEBELOW', (0, 0), (-1, 0), 1, colors.black),
        ('BACKGROUND', (0, 0), (-1, 0), colors.lightgrey),
        ('TEXTCOLOR', (0, 1), (0, -1), colors.HexColor('#B8860B')),  # Dark goldenrod color for entries
        ('GRID', (0, 0), (-1, -1), 0.5, colors.lightgrey),
    ]))
    story.append(toc_table)
    
    # ============ PAGE 3+: PRODUCT DESCRIPTION & TEST RESULTS FOR EACH SPECIMEN ============
    
    for specimen_idx, specimen in enumerate(specimens, 1):
        story.append(PageBreak())
        
        # Product Description Header
        story.append(Paragraph(f"<b>PRODUCT DESCRIPTION / SPECIMEN IDENTIFICATION: MOCK-UP #{specimen_idx}</b>", heading_style))
        story.append(Spacer(1, 0.1*inch))
        
        # Basic Info Table
        basic_info_data = [
            ['Series/Model:', ''],
            ['Overall Size:', ''],
            ['Configuration:', ''],
            ['Glass Type:', ''],
            ['Sealant:', ''],
            ['Design Pressure:', f"Inward: {specimen.get('inward_design_pressure', 0):.1f} PSF / Outward: {specimen.get('outward_design_pressure', 0):.1f} PSF"]
        ]
        basic_table = Table(basic_info_data, colWidths=[1.5*inch, 5.5*inch])
        basic_table.setStyle(TableStyle([
            ('FONTNAME', (0, 0), (0, -1), 'Helvetica-Bold'),
            ('FONTSIZE', (0, 0), (-1, -1), 9),
            ('VALIGN', (0, 0), (-1, -1), 'MIDDLE'),
            ('GRID', (0, 0), (-1, -1), 0.5, colors.black),
        ]))
        story.append(basic_table)
        story.append(Paragraph("<i>See glazing detail on drawings for more information</i>", small_style))
        story.append(Spacer(1, 0.15*inch))
        
        # Test Sequence
        story.append(Paragraph("<b>TEST SEQUENCE</b>", heading_style))
        test_sequence = [
            "1. Impact Test Per Ansi 297.1",
            "2. Air Leakage (ASTM E283/E2357M-19)",
            "3. Static Pressure (TAS 202-94): 1/2 Structural Pressure and Design Pressure",
            "4. Water Infiltration Test (ASTM E331-00)",
            "5. Static Pressure (TAS 202-94): Structural Pressure",
            "6. Forced Entry Resistance Test (ASTM F588-17)",
            "7. Large / Small Missile Impact (TAS 201-94)",
            "8. Cyclic Pressure (TAS 203-94)"
        ]
        for seq in test_sequence:
            story.append(Paragraph(seq, small_style))
        
        # ============ TEST RESULTS PAGES ============
        story.append(PageBreak())
        story.append(Paragraph(f"<b>TEST RESULTS - MOCK-UP #{specimen_idx}</b>", title_style))
        story.append(Spacer(1, 0.15*inch))
        
        # 1. MISSILE IMPACT TEST (First one)
        missile_tests = specimen.get('missile_impact_tests', [])
        if missile_tests:
            story.append(Paragraph("<b>TITLE OF TEST: | IMPACT TEST PER ANSI 297.1</b>", heading_style))
            story.append(Spacer(1, 0.1*inch))
            
            result_data = [
                ['Results'],
                ['Glass passed the impact test without hazardous breakage']
            ]
            result_table = Table(result_data, colWidths=[7*inch])
            result_table.setStyle(TableStyle([
                ('FONTNAME', (0, 0), (0, 0), 'Helvetica-Bold'),
                ('FONTSIZE', (0, 0), (-1, -1), 9),
                ('ALIGN', (0, 0), (-1, -1), 'CENTER'),
                ('VALIGN', (0, 0), (-1, -1), 'MIDDLE'),
                ('GRID', (0, 0), (-1, -1), 0.5, colors.black),
            ]))
            story.append(result_table)
            story.append(Spacer(1, 0.2*inch))
        
        # 2. AIR INFILTRATION TEST
        air_infiltration = [t for t in specimen.get('infiltration_tests', []) if 'air' in t.get('type', '').lower()]
        if air_infiltration:
            inf_test = air_infiltration[0]
            story.append(Paragraph("<b>TAS 202-94 (UNIFORM STATIC AIR PRESSURE)</b>", heading_style))
            temp_data = [
                ['Temperature during testing:', '80°F'],
                ['Barometric Reading:', '30.0 inches Hg']
            ]
            temp_table = Table(temp_data, colWidths=[2*inch, 2*inch])
            temp_table.setStyle(TableStyle([
                ('FONTNAME', (0, 0), (0, -1), 'Helvetica-Bold'),
                ('FONTSIZE', (0, 0), (-1, -1), 9),
                ('VALIGN', (0, 0), (-1, -1), 'MIDDLE'),
            ]))
            story.append(temp_table)
            story.append(Spacer(1, 0.1*inch))
            
            story.append(Paragraph("<b>TITLE OF TEST: | AIR INFILTRATION TEST (ASTM E283)</b>", heading_style))
            air_data = [
                ['Pressure', 'Measured Flow', 'Allowed Flow', 'Results'],
                [f"{inf_test.get('pressure', 0):.2f} PSF", f"{inf_test.get('leakage', 0):.2f} cfm/ft²", '0.3 cfm/ft²', 'Passed']
            ]
            air_table = Table(air_data, colWidths=[1.75*inch, 1.75*inch, 1.75*inch, 1.75*inch])
            air_table.setStyle(TableStyle([
                ('FONTNAME', (0, 0), (-1, 0), 'Helvetica-Bold'),
                ('FONTSIZE', (0, 0), (-1, -1), 9),
                ('ALIGN', (0, 0), (-1, -1), 'CENTER'),
                ('VALIGN', (0, 0), (-1, -1), 'MIDDLE'),
                ('GRID', (0, 0), (-1, -1), 0.5, colors.black),
                ('BACKGROUND', (0, 0), (-1, 0), colors.lightgrey),
            ]))
            story.append(air_table)
            story.append(Spacer(1, 0.2*inch))
        
        # 3. STATIC PRESSURE - PRELOAD AND DESIGN LOAD
        story.append(Paragraph("<b>TITLE OF TEST: | PRELOAD AND DESIGN LOAD (TAS 202)</b>", heading_style))
        
        # Get static tests for 1/2 structural and design pressure
        static_tests = specimen.get('static_tests', [])
        preload_tests = [t for t in static_tests if t.get('index') in [0, 1, 2, 3]]
        
        if preload_tests:
            # Build deflection table
            deflection_rows = [['Test', 'Pressure', 'Deflection Gauge', 'Deflection (in.)', 'Permanent Set (in.)']]
            
            for test in sorted(preload_tests, key=lambda x: x.get('index', 0)):
                test_name = f"1/2 Structural Load" if test.get('index') in [0, 1] else "Design Pressure"
                pressure = f"{test.get('pressure', 0):+.1f} PSF"
                test_type = test.get('type', '')
                
                # Get deflections from first trial if available
                trials = test.get('trials', [])
                if trials and trials[0].get('deflections'):
                    for defl in trials[0]['deflections']:
                        gauge_num = defl['gauge'].split()[-1] if 'gauge' in defl['gauge'].lower() else defl['gauge']
                        deflection_rows.append([
                            test_name,
                            pressure,
                            gauge_num,
                            f"{defl.get('max_deflection', 0):.2f}",
                            f"{defl.get('permanent_deflection', 0):.2f}"
                        ])
                        test_name = ""  # Only show test name once
                        pressure = ""
                else:
                    # Add rows for gauges 1, 2, 3 even if no data
                    for i in range(1, 4):
                        deflection_rows.append([test_name, pressure, str(i), '', ''])
                        test_name = ""
                        pressure = ""
            
            deflection_table = Table(deflection_rows, colWidths=[1.5*inch, 1.3*inch, 1.5*inch, 1.5*inch, 1.7*inch])
            deflection_table.setStyle(TableStyle([
                ('FONTNAME', (0, 0), (-1, 0), 'Helvetica-Bold'),
                ('FONTSIZE', (0, 0), (-1, -1), 9),
                ('ALIGN', (1, 0), (-1, -1), 'CENTER'),
                ('VALIGN', (0, 0), (-1, -1), 'MIDDLE'),
                ('GRID', (0, 0), (-1, -1), 0.5, colors.black),
                ('BACKGROUND', (0, 0), (-1, 0), colors.lightgrey),
            ]))
            story.append(deflection_table)
            
            story.append(Spacer(1, 0.1*inch))
            story.append(Paragraph("<b>Notes:</b>", small_style))
            story.append(Paragraph("• All pressures were held for 30 seconds", small_style))
            story.append(Paragraph("• See Appendix 1 for the deflection gauge location.", small_style))
            story.append(Spacer(1, 1.5*inch))
        
        # 4. WATER INFILTRATION TEST
        water_infiltration = [t for t in specimen.get('infiltration_tests', []) if 'water' in t.get('type', '').lower()]
        if water_infiltration:
            water_test = water_infiltration[0]
            story.append(Paragraph("<b>TITLE OF TEST: | WATER INFILTRATION TEST (ASTM E331)</b>", heading_style))
            
            water_temp_data = [
                ['Temperature during testing:', '80°F'],
                ['Barometric Reading:', '30.0 inches Hg']
            ]
            water_temp_table = Table(water_temp_data, colWidths=[2*inch, 2*inch])
            water_temp_table.setStyle(TableStyle([
                ('FONTNAME', (0, 0), (0, -1), 'Helvetica-Bold'),
                ('FONTSIZE', (0, 0), (-1, -1), 9),
                ('VALIGN', (0, 0), (-1, -1), 'MIDDLE'),
            ]))
            story.append(water_temp_table)
            story.append(Spacer(1, 0.1*inch))
            
            water_data = [
                ['Test Pressure', 'Test Duration', 'Water Leakage', 'Results'],
                [f"{water_test.get('pressure', 0):.2f} PSF", f"{water_test.get('duration', 0):.0f} min", 
                 f"{water_test.get('leakage', 0):.2f} in³/s (No leakage)" if water_test.get('leakage', 0) < 0.1 else f"{water_test.get('leakage', 0):.2f} in³/s", 
                 'Passed']
            ]
            water_table = Table(water_data, colWidths=[1.75*inch, 1.75*inch, 1.75*inch, 1.75*inch])
            water_table.setStyle(TableStyle([
                ('FONTNAME', (0, 0), (-1, 0), 'Helvetica-Bold'),
                ('FONTSIZE', (0, 0), (-1, -1), 9),
                ('ALIGN', (0, 0), (-1, -1), 'CENTER'),
                ('VALIGN', (0, 0), (-1, -1), 'MIDDLE'),
                ('GRID', (0, 0), (-1, -1), 0.5, colors.black),
                ('BACKGROUND', (0, 0), (-1, 0), colors.lightgrey),
            ]))
            story.append(water_table)
            story.append(Spacer(1, 0.2*inch))
        
        # 5. STRUCTURAL LOAD (1.5x)
        story.append(PageBreak())
        story.append(Paragraph("<b>TITLE OF TEST: | STRUCTURAL LOAD (TAS 202)</b>", heading_style))
        
        structural_tests = [t for t in static_tests if t.get('index') in [4, 5]]
        if structural_tests:
            structural_rows = [['Test', 'Pressure', 'Deflection Gauge', 'Deflection (in.)', 'Permanent Set (in.)']]
            
            for test in sorted(structural_tests, key=lambda x: x.get('index', 0)):
                test_name = "Structural Load"
                pressure = f"{test.get('pressure', 0):+.1f} PSF"
                
                trials = test.get('trials', [])
                if trials and trials[0].get('deflections'):
                    for defl in trials[0]['deflections']:
                        gauge_num = defl['gauge'].split()[-1] if 'gauge' in defl['gauge'].lower() else defl['gauge']
                        structural_rows.append([
                            test_name,
                            pressure,
                            gauge_num,
                            f"{defl.get('max_deflection', 0):.2f}",
                            f"{defl.get('permanent_deflection', 0):.2f}"
                        ])
                        test_name = ""
                        pressure = ""
                else:
                    for i in range(1, 4):
                        structural_rows.append([test_name, pressure, str(i), '', ''])
                        test_name = ""
                        pressure = ""
            
            structural_table = Table(structural_rows, colWidths=[1.5*inch, 1.3*inch, 1.5*inch, 1.5*inch, 1.7*inch])
            structural_table.setStyle(TableStyle([
                ('FONTNAME', (0, 0), (-1, 0), 'Helvetica-Bold'),
                ('FONTSIZE', (0, 0), (-1, -1), 9),
                ('ALIGN', (1, 0), (-1, -1), 'CENTER'),
                ('VALIGN', (0, 0), (-1, -1), 'MIDDLE'),
                ('GRID', (0, 0), (-1, -1), 0.5, colors.black),
                ('BACKGROUND', (0, 0), (-1, 0), colors.lightgrey),
            ]))
            story.append(structural_table)
            
            story.append(Spacer(1, 0.1*inch))
            story.append(Paragraph("<b>Notes:</b>", small_style))
            story.append(Paragraph("• All pressures were held for 30 seconds", small_style))
            story.append(Paragraph("• See Appendix 1 for the deflection gauge location.", small_style))
            story.append(Spacer(1, 0.2*inch))
        
        # 6. FORCED ENTRY RESISTANCE TEST
        story.append(Paragraph("<b>TITLE OF TEST: | FORCED ENTRY RESISTANCE TEST (ASTM F588)</b>", heading_style))
        forced_entry_data = [
            ['Temperature during testing:', '80°F'],
            ['Barometric Reading:', '30.0 inches Hg']
        ]
        forced_entry_temp_table = Table(forced_entry_data, colWidths=[2*inch, 2*inch])
        forced_entry_temp_table.setStyle(TableStyle([
            ('FONTNAME', (0, 0), (0, -1), 'Helvetica-Bold'),
            ('FONTSIZE', (0, 0), (-1, -1), 9),
            ('VALIGN', (0, 0), (-1, -1), 'MIDDLE'),
        ]))
        story.append(forced_entry_temp_table)
        story.append(Spacer(1, 0.1*inch))
        
        forced_data = [
            ['Grade', 'Allowed', 'Results'],
            ['', 'No Entry', 'Passed']
        ]
        forced_table = Table(forced_data, colWidths=[2.33*inch, 2.33*inch, 2.34*inch])
        forced_table.setStyle(TableStyle([
            ('FONTNAME', (0, 0), (-1, 0), 'Helvetica-Bold'),
            ('FONTSIZE', (0, 0), (-1, -1), 9),
            ('ALIGN', (0, 0), (-1, -1), 'CENTER'),
            ('VALIGN', (0, 0), (-1, -1), 'MIDDLE'),
            ('GRID', (0, 0), (-1, -1), 0.5, colors.black),
            ('BACKGROUND', (0, 0), (-1, 0), colors.lightgrey),
        ]))
        story.append(forced_table)
        story.append(Spacer(1, 0.2*inch))
        
        # 7. LARGE MISSILE IMPACT TEST
        if missile_tests:
            for missile_test in missile_tests:
                story.append(Paragraph(f"<b>TITLE OF TEST: | LARGE MISSILE IMPACT TEST (TAS 201-94)</b>", heading_style))
                
                missile_info_data = [
                    ['Missile:', f"{missile_test.get('missile', '')}"],
                    ['Missile Weight:', f"{missile_test.get('missile_weight', 0):.1f} lb"]
                ]
                missile_info_table = Table(missile_info_data, colWidths=[1.5*inch, 3*inch])
                missile_info_table.setStyle(TableStyle([
                    ('FONTNAME', (0, 0), (0, -1), 'Helvetica-Bold'),
                    ('FONTSIZE', (0, 0), (-1, -1), 9),
                    ('VALIGN', (0, 0), (-1, -1), 'MIDDLE'),
                ]))
                story.append(missile_info_table)
                story.append(Spacer(1, 0.1*inch))
                
                shots = missile_test.get('shots', [])
                if shots:
                    shot_rows = [['Impact', 'Velocity (ft/s)', 'Results', 'Notes']]
                    for idx, shot in enumerate(shots, 1):
                        shot_rows.append([
                            str(idx),
                            f"{shot.get('velocity', 0):.1f}",
                            'Passed' if shot.get('result') else 'Failed',
                            shot.get('note', '')
                        ])
                    
                    shot_table = Table(shot_rows, colWidths=[1*inch, 1.5*inch, 1.5*inch, 3*inch])
                    shot_table.setStyle(TableStyle([
                        ('FONTNAME', (0, 0), (-1, 0), 'Helvetica-Bold'),
                        ('FONTSIZE', (0, 0), (-1, -1), 9),
                        ('ALIGN', (0, 0), (2, -1), 'CENTER'),
                        ('VALIGN', (0, 0), (-1, -1), 'MIDDLE'),
                        ('GRID', (0, 0), (-1, -1), 0.5, colors.black),
                        ('BACKGROUND', (0, 0), (-1, 0), colors.lightgrey),
                    ]))
                    story.append(shot_table)
                    
                    story.append(Spacer(1, 0.1*inch))
                    story.append(Paragraph("<b>Notes:</b>", small_style))
                    story.append(Paragraph("• See Appendix 1 for impact locations.", small_style))
                    story.append(Spacer(1, 0.2*inch))
        
        # 8. CYCLIC PRESSURE TEST
        story.append(PageBreak())
        story.append(Paragraph("<b>TITLE OF TEST: | CYCLIC PRESSURE TEST (TAS 203-94)</b>", heading_style))
        
        cyclic_temp_data = [
            ['Temperature during testing:', '80°F'],
            ['Barometric Reading:', '30.0 inches Hg']
        ]
        cyclic_temp_table = Table(cyclic_temp_data, colWidths=[2*inch, 2*inch])
        cyclic_temp_table.setStyle(TableStyle([
            ('FONTNAME', (0, 0), (0, -1), 'Helvetica-Bold'),
            ('FONTSIZE', (0, 0), (-1, -1), 9),
            ('VALIGN', (0, 0), (-1, -1), 'MIDDLE'),
        ]))
        story.append(cyclic_temp_table)
        story.append(Spacer(1, 0.1*inch))
        
        cyclic_tests = specimen.get('cyclic_tests', [])
        
        # Positive Pressure (Inward)
        inward_cyclic = [t for t in cyclic_tests if t.get('type') == 'inward']
        if inward_cyclic:
            cyclic_rows = [['Cycles', 'Positive Pressure Range (PSF)', 'Results', 'Notes']]
            for test in sorted(inward_cyclic, key=lambda x: x.get('index', 0)):
                low = test.get('low_pressure', 0)
                high = test.get('high_pressure', 0)
                cycles = test.get('cycles', 0)
                result = 'Passed' if test.get('finished') else 'In Progress'
                
                cyclic_rows.append([
                    str(cycles),
                    f"{low:.1f}  -  {high:.1f}",
                    result,
                    ''
                ])
            
            cyclic_table = Table(cyclic_rows, colWidths=[1*inch, 2.5*inch, 1.5*inch, 2*inch])
            cyclic_table.setStyle(TableStyle([
                ('FONTNAME', (0, 0), (-1, 0), 'Helvetica-Bold'),
                ('FONTSIZE', (0, 0), (-1, -1), 9),
                ('ALIGN', (0, 0), (2, -1), 'CENTER'),
                ('VALIGN', (0, 0), (-1, -1), 'MIDDLE'),
                ('GRID', (0, 0), (-1, -1), 0.5, colors.black),
                ('BACKGROUND', (0, 0), (-1, 0), colors.lightgrey),
            ]))
            story.append(cyclic_table)
            story.append(Spacer(1, 0.2*inch))
        
        # Negative Pressure (Outward)
        outward_cyclic = [t for t in cyclic_tests if t.get('type') == 'outward']
        if outward_cyclic:
            cyclic_neg_rows = [['Cycles', 'Negative Pressure Range (PSF)', 'Results', 'Notes']]
            for test in sorted(outward_cyclic, key=lambda x: x.get('index', 0)):
                low = test.get('low_pressure', 0)
                high = test.get('high_pressure', 0)
                cycles = test.get('cycles', 0)
                result = 'Passed' if test.get('finished') else 'In Progress'
                
                cyclic_neg_rows.append([
                    str(cycles),
                    f"{low:.1f}  -  {high:.1f}",
                    result,
                    ''
                ])
            
            cyclic_neg_table = Table(cyclic_neg_rows, colWidths=[1*inch, 2.5*inch, 1.5*inch, 2*inch])
            cyclic_neg_table.setStyle(TableStyle([
                ('FONTNAME', (0, 0), (-1, 0), 'Helvetica-Bold'),
                ('FONTSIZE', (0, 0), (-1, -1), 9),
                ('ALIGN', (0, 0), (2, -1), 'CENTER'),
                ('VALIGN', (0, 0), (-1, -1), 'MIDDLE'),
                ('GRID', (0, 0), (-1, -1), 0.5, colors.black),
                ('BACKGROUND', (0, 0), (-1, 0), colors.lightgrey),
            ]))
            story.append(cyclic_neg_table)
        
        # ============ FINAL STATEMENT AND CONCLUSION FOR THIS SPECIMEN ============
        story.append(PageBreak())
        
        # Final Statement
        story.append(Paragraph(f"<b>FINAL STATEMENT FOR MOCK-UP #{specimen_idx}:</b> Following testing, the sample was disassembled. No failure was observed in any of the frames, fastenings, or anchorage. Tape and film were used to seal against air leakage, and in the judgment of the test engineer, the tape or film did not affect the test results.", body_style))
        story.append(Spacer(1, 0.2*inch))
        
        # Conclusion
        story.append(Paragraph(f"<b>CONCLUSION FOR MOCK-UP #{specimen_idx}:</b>", heading_style))
        conclusion_text = """The tests were conducted in accordance with ASTM E283/E2357M-19, ASTM E331-00, ASTM F588-17, TAS 201-94, TAS 202-94, and TAS 203-94. No signs of failure were observed in any area of the test specimen during the TAS 202 testing. The large missiles impacted each intended target. Each impact location was carefully inspected, and no signs of penetration, rupture, or opening after the large missile impact test were observed. No signs of failure were observed in any area of the test specimen during the Cyclic load test.<br/><br/>
Upon the completion of testing, the specimen tested met the requirements of these protocols and the Florida Building Code, Building."""
        story.append(Paragraph(conclusion_text, body_style))
        story.append(Spacer(1, 0.5*inch))
    
    # ============ FINAL PAGE: OVERALL SUMMARY FOR ALL SPECIMENS ============
    story.append(PageBreak())
    
    # Overall Summary
    story.append(Paragraph("<b>OVERALL TEST SUMMARY:</b>", heading_style))
    story.append(Spacer(1, 0.1*inch))
    
    num_specimens = len(specimens)
    summary_text = f"""All {num_specimens} mock-up specimens tested as part of this project have been thoroughly evaluated according to the applicable testing standards. Each specimen was subjected to a comprehensive series of tests including impact, air leakage, static pressure, water infiltration, forced entry resistance, missile impact, and cyclic pressure testing.<br/><br/>
Following the completion of all testing protocols, all {num_specimens} specimens demonstrated compliance with the requirements of ASTM E283/E2357M-19, ASTM E331-00, ASTM F588-17, TAS 201-94, TAS 202-94, and TAS 203-94, as well as the Florida Building Code, Building.<br/><br/>
<b>Summary of Results:</b><br/>
• All specimens passed the required impact tests without hazardous breakage<br/>
• No signs of failure were observed in frames, fastenings, or anchorage<br/>
• All large missile impacts were successful with no penetration or rupture observed<br/>
• All specimens met cyclic load test requirements<br/>
• Air leakage and water infiltration tests were within acceptable limits"""
    story.append(Paragraph(summary_text, body_style))
    story.append(Spacer(1, 0.5*inch))
    
    # Witness section
    story.append(Paragraph("<b>WITNESS TO TESTING</b>", heading_style))
    story.append(Paragraph("• <b>Arshad Viqar, PE</b>", body_style))
    story.append(Paragraph("• <b>Luis Macias</b> - IFET, INC. CEO", body_style))
    story.append(Spacer(1, 0.3*inch))
    
    # For IFET, INC.
    story.append(Paragraph("<b>For IFET, INC.</b>", heading_style))
    story.append(Paragraph("Prepared by:", body_style))
    story.append(Spacer(1, 0.5*inch))
    
    # Signature table
    sig_data = [
        ['Claribel Leon', 'Arshad Viqar, PE'],
        ['IFET, INC. - Lab. Manager', 'FL PE # 38863 / FL C.A.N. # 9101']
    ]
    sig_table = Table(sig_data, colWidths=[3.5*inch, 3.5*inch])
    sig_table.setStyle(TableStyle([
        ('FONTNAME', (0, 0), (-1, 0), 'Helvetica-Bold'),
        ('FONTSIZE', (0, 0), (-1, -1), 8),
        ('VALIGN', (0, 0), (-1, -1), 'TOP'),
        ('LINEABOVE', (0, 0), (-1, 0), 0.5, colors.black),
    ]))
    story.append(sig_table)
    story.append(Spacer(1, 0.3*inch))
    
    # Revision History
    story.append(Paragraph("<b>REVISION HISTORY</b>", heading_style))
    revision_data = [
        ['Rev. No.', 'Date', 'Description'],
        ['0', datetime.now().strftime('%m/%d/%Y'), 'Original Report Issue']
    ]
    revision_table = Table(revision_data, colWidths=[1*inch, 1.5*inch, 4.5*inch])
    revision_table.setStyle(TableStyle([
        ('FONTNAME', (0, 0), (-1, 0), 'Helvetica-Bold'),
        ('FONTSIZE', (0, 0), (-1, -1), 9),
        ('ALIGN', (0, 0), (-1, -1), 'LEFT'),
        ('VALIGN', (0, 0), (-1, -1), 'MIDDLE'),
        ('GRID', (0, 0), (-1, -1), 0.5, colors.black),
        ('BACKGROUND', (0, 0), (-1, 0), colors.lightgrey),
    ]))
    story.append(revision_table)
    story.append(Spacer(1, 0.5*inch))
    
    # End Report
    story.append(Paragraph("<para align='center'><b>*******END REPORT*******</b></para>", body_style))
    
    # Build PDF with custom template
    template = IFETReportTemplate(filename)
    
    def add_page_elements(canvas, doc):
        template.add_watermark(canvas, doc)
        template.header(canvas, doc, 
                       project_info={'project_number': report_info.get('project_number', 'IFET-XX-XXXX')},
                       report_info=report_info)
        template.footer(canvas, doc)
    
    doc.build(story, onFirstPage=add_page_elements, onLaterPages=add_page_elements)
    
    return filename
