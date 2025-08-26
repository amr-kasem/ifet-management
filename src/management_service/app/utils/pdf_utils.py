from PyPDF2 import PdfReader, PdfWriter
from reportlab.pdfgen import canvas
from reportlab.lib.pagesizes import letter
from io import BytesIO
from datetime import datetime
import os

def create_test_report_pdf(project, filename, client_info=None, test_location=None):
    """
    Generate a comprehensive test report PDF based on project data and test results.
    
    Args:
        project: SQLAlchemy Project object with related test data
        filename: Output PDF filename (can be temporary file path)
        client_info: Dict with client information (name, address, phone, email)
        test_location: String with testing location details
        
    Returns:
        str: Path to the generated PDF file
        
    Example Usage:
        client_info = {
            'name': 'ABC Manufacturing Corp',
            'address': '123 Industrial Way, Miami, FL 33126',
            'phone': '(305) 555-0123',
            'email': 'contact@abcmfg.com'
        }

        test_location = "IFET, Inc. Laboratory - 7839 NW 15th St. Miami, FL 33126. USA."

        # Using with temporary file
        with tempfile.NamedTemporaryFile(delete=False, suffix=".pdf") as temp_file:
            pdf_path = create_test_report_pdf(db_project, temp_file.name)
    """
    
    # Set default values
    if client_info is None:
        client_info = {'name': '', 'address': '', 'phone': '', 'email': ''}
    if test_location is None:
        test_location = "IFET, Inc. Laboratory - 7839 NW 15th St. Miami, FL 33126. USA."
    
    # Template PDF path (assuming it's in the same directory)
    template_path = "./app/template.pdf"
    
    if not os.path.exists(template_path):
        raise FileNotFoundError(f"Template PDF not found: {template_path}")
    
    # Read the template PDF
    reader = PdfReader(template_path)
    writer = PdfWriter()
    
    # Create overlay with data
    packet = BytesIO()
    can = canvas.Canvas(packet, pagesize=letter)
    
    # Helper function to safely get values
    def safe_get(obj, attr, default=''):
        try:
            return getattr(obj, attr, default) or default
        except:
            return default
    
    # Fill in client information on first page
    can.setFont("Helvetica", 10)
    
    # Client name
    can.drawString(100, 750, safe_get(client_info, 'name', client_info.get('name', '')))
    
    # Phone
    can.drawString(450, 770, safe_get(client_info, 'phone', client_info.get('phone', '')))
    
    # Address
    can.drawString(100, 730, safe_get(client_info, 'address', client_info.get('address', '')))
    
    # Email
    can.drawString(450, 730, safe_get(client_info, 'email', client_info.get('email', '')))
    
    # Project information
    can.drawString(100, 680, f"Project: {safe_get(project, 'name', '')}")
    can.drawString(100, 660, f"Project ID: {safe_get(project, 'id', '')}")
    
    # Design pressures
    can.drawString(100, 640, f"Inward Design Pressure: {safe_get(project, 'inward_design_pressure', '')} PSF")
    can.drawString(100, 620, f"Outward Design Pressure: {safe_get(project, 'outward_design_pressure', '')} PSF")
    
    # Device information
    device = safe_get(project, 'device', None)
    if device:
        can.drawString(100, 600, f"Device: {safe_get(device, 'name', '')}")
        can.drawString(100, 580, f"Turbo Mode: {safe_get(device, 'turbo_mode', '')}")
        can.drawString(100, 560, f"Turbo Slave: {safe_get(device, 'turbo_slave', '')}")
    
    # Static Test Results
    y_position = 520
    static_tests = safe_get(project, 'static_tests', [])
    if static_tests:
        can.drawString(100, y_position, "STATIC TEST RESULTS:")
        y_position -= 20
        
        for test in static_tests:
            can.setFont("Helvetica", 9)
            can.drawString(120, y_position, f"Test {safe_get(test, 'index', '')}: {safe_get(test, 'pressure_factor', '')} - {safe_get(test, 'pressure', '')} PSF")
            y_position -= 15
            can.drawString(140, y_position, f"Duration: {safe_get(test, 'duration', '')}s, Type: {safe_get(test, 'type', '')}")
            y_position -= 15
            
            # Test results/trials
            trials = safe_get(test, 'trials', [])
            for trial in trials:
                can.drawString(160, y_position, f"Trial {safe_get(trial, 'trial_number', '')}: {'PASS' if safe_get(trial, 'result', False) else 'FAIL'}")
                y_position -= 12
                
                # Deflections
                deflections = safe_get(trial, 'deflections', [])
                for deflection in deflections:
                    can.drawString(180, y_position, f"Gauge {safe_get(deflection, 'deflection_gauge', '')}: Max={safe_get(deflection, 'max_deflection', '')}in, Perm={safe_get(deflection, 'permanent_deflection', '')}in")
                    y_position -= 10
            
            y_position -= 10
            if y_position < 100:  # Start new page if needed
                can.showPage()
                y_position = 750
    
    # Infiltration Test Results
    if y_position < 200:
        can.showPage()
        y_position = 750
    
    infiltration_tests = safe_get(project, 'infiltration_tests', [])
    if infiltration_tests:
        can.setFont("Helvetica", 10)
        can.drawString(100, y_position, "INFILTRATION TEST RESULTS:")
        y_position -= 20
        
        for test in infiltration_tests:
            can.setFont("Helvetica", 9)
            can.drawString(120, y_position, f"Type: {safe_get(test, 'type', '')}")
            y_position -= 15
            can.drawString(120, y_position, f"Pressure: {safe_get(test, 'pressure', '')} PSF")
            y_position -= 15
            can.drawString(120, y_position, f"Duration: {safe_get(test, 'duration', '')} min")
            y_position -= 15
            can.drawString(120, y_position, f"Leakage: {safe_get(test, 'leakage', '')} cfm/ft²")
            y_position -= 20
    
    # Missile Impact Test Results
    if y_position < 200:
        can.showPage()
        y_position = 750
    
    missile_tests = safe_get(project, 'missile_impact_tests', [])
    if missile_tests:
        can.setFont("Helvetica", 10)
        can.drawString(100, y_position, "MISSILE IMPACT TEST RESULTS:")
        y_position -= 20
        
        for test in missile_tests:
            can.setFont("Helvetica", 9)
            can.drawString(120, y_position, f"Missile: {safe_get(test, 'missile', '')}")
            y_position -= 15
            can.drawString(120, y_position, f"Weight: {safe_get(test, 'missile_weight', '')} lb")
            y_position -= 20
            
            shots = safe_get(test, 'shots', [])
            for i, shot in enumerate(shots, 1):
                can.drawString(140, y_position, f"Impact {i}: {safe_get(shot, 'velocity', '')} ft/s - {'PASS' if safe_get(shot, 'result', False) else 'FAIL'}")
                y_position -= 12
                if safe_get(shot, 'note', ''):
                    can.drawString(160, y_position, f"Note: {safe_get(shot, 'note', '')}")
                    y_position -= 12
            y_position -= 10
    
    # Cyclic Test Results
    if y_position < 200:
        can.showPage()
        y_position = 750
    
    cyclic_tests = safe_get(project, 'cyclic_tests', [])
    if cyclic_tests:
        can.setFont("Helvetica", 10)
        can.drawString(100, y_position, "CYCLIC TEST RESULTS:")
        y_position -= 20
        
        for test in cyclic_tests:
            can.setFont("Helvetica", 9)
            can.drawString(120, y_position, f"Test {safe_get(test, 'index', '')}: {safe_get(test, 'type', '')}")
            y_position -= 15
            can.drawString(120, y_position, f"Cycles: {safe_get(test, 'cycles', '')} (Current: {safe_get(test, 'current_cycle', '')})")
            y_position -= 15
            can.drawString(120, y_position, f"Pressure Range: {safe_get(test, 'low_pressure', '')} to {safe_get(test, 'high_pressure', '')} PSF")
            y_position -= 15
            can.drawString(120, y_position, f"Status: {'Finished' if safe_get(test, 'finished', False) else 'In Progress'}")
            y_position -= 20
    
    # Add timestamp and engineer info
    can.setFont("Helvetica", 8)
    can.drawString(50, 50, f"Report Generated: {datetime.now().strftime('%m/%d/%Y %H:%M')}")
    can.drawString(50, 35, "Arshad Viqar, PE - FL PE # 38863 / FL C.A.N. # 9101")
    
    can.save()
    
    # Move to the beginning of the StringIO buffer
    packet.seek(0)
    overlay_pdf = PdfReader(packet)
    
    # Merge the overlay with each page of the template
    for page_num in range(len(reader.pages)):
        page = reader.pages[page_num]
        
        # Add overlay to first page (main data)
        if page_num == 0 and len(overlay_pdf.pages) > 0:
            page.merge_page(overlay_pdf.pages[0])
        
        writer.add_page(page)
    
    # Write the final PDF
    with open(filename, 'wb') as output_file:
        writer.write(output_file)
    
    return filename