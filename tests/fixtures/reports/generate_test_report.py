#!/usr/bin/env python3
"""
Generate a synthetic NI 43-101 technical report PDF for Patterson Lake South.

This script creates a realistic technical report fixture for testing the
GeoRAG document ingestion pipeline. Uses reportlab to generate PDF with
proper section structure, page numbering, and geological content.

Usage:
    python generate_test_report.py

Output:
    tests/fixtures/reports/PLS-2024-Technical-Report.pdf
"""

from datetime import datetime
from reportlab.lib.pagesizes import letter
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.units import inch
from reportlab.lib.enums import TA_CENTER, TA_LEFT, TA_JUSTIFY
from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, PageBreak, Table, TableStyle
from reportlab.pdfgen import canvas
from reportlab.lib import colors


def create_title_page_style():
    """Create custom styles for the title page."""
    styles = getSampleStyleSheet()
    title_style = ParagraphStyle(
        'CustomTitle',
        parent=styles['Heading1'],
        fontSize=24,
        textColor=colors.HexColor('#1a1a1a'),
        spaceAfter=12,
        alignment=TA_CENTER,
        fontName='Helvetica-Bold',
    )
    subtitle_style = ParagraphStyle(
        'CustomSubtitle',
        parent=styles['Normal'],
        fontSize=14,
        textColor=colors.HexColor('#333333'),
        spaceAfter=6,
        alignment=TA_CENTER,
        fontName='Helvetica',
    )
    info_style = ParagraphStyle(
        'CustomInfo',
        parent=styles['Normal'],
        fontSize=11,
        textColor=colors.HexColor('#444444'),
        spaceAfter=4,
        alignment=TA_CENTER,
        fontName='Helvetica',
    )
    return title_style, subtitle_style, info_style


def create_section_style():
    """Create styles for section content."""
    styles = getSampleStyleSheet()
    heading_style = ParagraphStyle(
        'SectionHeading',
        parent=styles['Heading2'],
        fontSize=13,
        textColor=colors.HexColor('#1a1a1a'),
        spaceAfter=10,
        spaceBefore=12,
        fontName='Helvetica-Bold',
        keepWithNext=True,
    )
    body_style = ParagraphStyle(
        'SectionBody',
        parent=styles['BodyText'],
        fontSize=10,
        textColor=colors.HexColor('#2a2a2a'),
        spaceAfter=12,
        alignment=TA_JUSTIFY,
        fontName='Times-Roman',
        leading=14,
    )
    return heading_style, body_style


class NumberedCanvas(canvas.Canvas):
    """Custom canvas class to add page numbers and footer."""

    def __init__(self, *args, **kwargs):
        canvas.Canvas.__init__(self, *args, **kwargs)
        self._saved_state = None

    def showPage(self):
        self._add_footer()
        canvas.Canvas.showPage(self)

    def _add_footer(self):
        """Add page number to footer."""
        self.saveState()
        self.setFont('Helvetica', 9)
        self.setFillColor(colors.HexColor('#666666'))
        page_num = self._pageNumber
        self.drawString(
            letter[0] / 2 - 10,
            0.5 * inch,
            f'Page {page_num}',
        )
        self.restoreState()


def generate_report():
    """Generate the full NI 43-101 technical report PDF."""
    import os

    # Determine output path relative to script location
    script_dir = os.path.dirname(os.path.abspath(__file__))
    output_path = os.path.join(script_dir, 'PLS-2024-Technical-Report.pdf')

    doc = SimpleDocTemplate(
        output_path,
        pagesize=letter,
        rightMargin=0.75 * inch,
        leftMargin=0.75 * inch,
        topMargin=0.75 * inch,
        bottomMargin=0.75 * inch,
    )
    doc.canvasmaker = NumberedCanvas

    story = []

    # Title Page
    title_style, subtitle_style, info_style = create_title_page_style()

    story.append(Spacer(1, 1.5 * inch))
    story.append(Paragraph('NI 43-101 Technical Report', title_style))
    story.append(Spacer(1, 0.3 * inch))
    story.append(Paragraph('on the', subtitle_style))
    story.append(Spacer(1, 0.1 * inch))
    story.append(Paragraph('Patterson Lake South Property', title_style))
    story.append(Spacer(1, 0.2 * inch))
    story.append(Paragraph('Athabasca Basin, Saskatchewan, Canada', subtitle_style))
    story.append(Spacer(1, 0.5 * inch))
    story.append(Paragraph('Prepared for: Fission Uranium Corp.', info_style))
    story.append(Paragraph('Effective Date: June 15, 2024', info_style))
    story.append(Paragraph('Report Date: August 30, 2024', info_style))
    story.append(Spacer(1, 0.4 * inch))
    story.append(Paragraph('Qualified Persons:', subtitle_style))
    story.append(Paragraph('Dr. Sarah Thompson, P.Geo.', info_style))
    story.append(Paragraph('Dr. James Chen, P.Eng.', info_style))
    story.append(PageBreak())

    # Section Styles
    heading_style, body_style = create_section_style()

    # Section 1: Summary
    story.append(Paragraph('1. Summary', heading_style))
    summary_text = (
        'Patterson Lake South (PLS) is an advanced-stage uranium exploration project '
        'located in the Athabasca Basin of northern Saskatchewan, Canada. The property is '
        'host to the Triple R deposit, which contains significant high-grade uranium '
        'mineralization at the unconformity between Athabasca Group sandstones and '
        'Paleoproterozoic basement rocks. Current mineral resource estimates include '
        'Indicated resources of 5.2 million tonnes at 1.52% U<sub>3</sub>O<sub>8</sub>, '
        'containing approximately 174 million pounds of uranium. Inferred resources are '
        '3.8 million tonnes at 0.85% U<sub>3</sub>O<sub>8</sub>, containing approximately '
        '71 million pounds of uranium. Mineralization is associated with structurally '
        'controlled alteration zones in basement rocks, with observed grades exceeding '
        '50,000 ppm U<sub>3</sub>O<sub>8</sub> in select intervals. The deposit shows '
        'characteristics comparable to other world-class unconformity-type uranium systems '
        'in the Athabasca Basin.'
    )
    story.append(Paragraph(summary_text, body_style))
    story.append(Spacer(1, 0.2 * inch))

    # Section 2: Introduction
    story.append(Paragraph('2. Introduction', heading_style))
    intro_text = (
        'This technical report has been prepared in accordance with the Canadian Securities '
        'Administrators\' National Instrument 43-101 Standards of Disclosure for Mineral '
        'Projects (NI 43-101) and Form 43-101F1. The report summarizes exploration activities, '
        'drilling results, and mineral resource estimates for the Patterson Lake South property. '
        'A site visit to the PLS property was conducted during May 2024 by both Qualified Persons. '
        'Data sources include 47 diamond and reverse-circulation drill holes completed between '
        '2013 and 2024, historical and recent airborne geophysical surveys, surface geochemical '
        'programs, and detailed geological mapping. All historical data was reviewed and verified '
        'against original source documents.'
    )
    story.append(Paragraph(intro_text, body_style))
    story.append(Spacer(1, 0.2 * inch))

    # Section 3: Reliance on Other Experts
    story.append(Paragraph('3. Reliance on Other Experts', heading_style))
    reliance_text = (
        'This technical report has been prepared by the Qualified Persons listed herein. '
        'Information regarding the mineral tenure, claims ownership, and regulatory status of the '
        'property was provided by Fission Uranium Corp. legal counsel and has not been '
        'independently verified by the Qualified Persons. The Qualified Persons have relied upon '
        'these representations and do not assume responsibility for errors or omissions in tenure '
        'information. All other technical and geological interpretations are based on the '
        'Qualified Persons\' direct review of assay data, drill logs, and core photography.'
    )
    story.append(Paragraph(reliance_text, body_style))
    story.append(Spacer(1, 0.2 * inch))

    # Section 4: Property Description and Location
    story.append(Paragraph('4. Property Description and Location', heading_style))
    property_text = (
        'The Patterson Lake South property comprises 31,039 hectares of contiguous mineral claims '
        'distributed across 17 staked claims in the Athabasca Basin of northern Saskatchewan. '
        'The property is centered at approximately UTM Zone 13N coordinates 495,000 easting and '
        '6,220,000 northing (NAD83). Access to the project is achieved via gravel road from the '
        'community of Points North Landing, approximately 45 kilometres to the southwest. The '
        'property is situated in a subarctic continental climate zone at elevations ranging from '
        '420 to 450 metres above sea level. The claims are held in good standing with no known '
        'encumbrances, liabilities, or third-party royalties, except for a standard 3% net smelter '
        'return (NSR) royalty retained by the Crown. The property is accessible by road during the '
        'winter months via ice road to Stony Rapids, the nearest year-round settlement, located '
        'approximately 140 kilometres to the north.'
    )
    story.append(Paragraph(property_text, body_style))
    story.append(Spacer(1, 0.2 * inch))

    # Section 5: Accessibility, Climate, Local Resources, Infrastructure and Physiography
    story.append(Paragraph('5. Accessibility, Climate, Local Resources, Infrastructure and Physiography', heading_style))
    access_text = (
        'The Patterson Lake South property is situated in a remote wilderness area of northern Saskatchewan. '
        'The region is characterized by subarctic continental climate with long, cold winters and short summers. '
        'The field season is restricted to June through September due to seasonal ice road access and permafrost '
        'constraints. The property is covered by mature boreal forest dominated by black spruce and jack pine with '
        'thick moss and lichen ground cover. Surface topography is relatively subdued with numerous lakes and muskeg. '
        'The nearest permanent community is Stony Rapids, accessible year-round by air charter, with winter road access '
        'via seasonal ice roads. A gravel airstrip capable of handling Twin Otter and larger aircraft is located at Points '
        'North Landing, 45 kilometres from the property. Electricity is generated by diesel at the property, and potable '
        'water is obtained from local lakes. The region has limited infrastructure, necessitating self-sufficiency in '
        'project operations including accommodation, food supply, and emergency medical response.'
    )
    story.append(Paragraph(access_text, body_style))
    story.append(Spacer(1, 0.2 * inch))

    if len(story) > 20:
        story.append(PageBreak())

    # Section 6: History
    story.append(Paragraph('6. History', heading_style))
    history_text = (
        'Initial exploration for uranium on the Patterson Lake South property was conducted by SMDC '
        '(Saskatchewan Mineral Development Company) during the 1970s following regional airborne geophysical surveys. '
        'Limited exploration activity occurred until 2013 when Fission Uranium Corp. discovered significant uranium '
        'mineralization in drill hole PLS13-022 at the unconformity. This discovery prompted a systematic delineation '
        'program conducted between 2013 and 2018, resulting in the definition of the Triple R deposit. Major drilling and '
        'resource definition activities were conducted from 2014 through 2018. The current exploration program, initiated '
        'in 2018, has focused on upgrading the mineral resource classification from inferred to indicated through infill '
        'drilling and detailed geological characterization. A total of 47 drill holes have been completed to date on the '
        'property, with cumulative drilling exceeding 15,000 metres.'
    )
    story.append(Paragraph(history_text, body_style))
    story.append(Spacer(1, 0.2 * inch))

    # Section 7: Geological Setting and Mineralization
    story.append(Paragraph('7. Geological Setting and Mineralization', heading_style))
    geology_text = (
        'The Patterson Lake South property is host to unconformity-type uranium mineralization characteristic of the '
        'Athabasca Basin. Basement lithologies are predominantly Paleoproterozoic pelitic and semipelitic gneisses intruded '
        'by graphitic metapelite units. These basement rocks are unconformably overlain by Athabasca Group sandstones at '
        'depths ranging from 50 to 150 metres. Mineralization is associated with structurally controlled alteration zones '
        'developed in the basement rocks adjacent to and below the unconformity surface. Key geological controls on '
        'mineralization include: (1) proximity to conductive graphitic basement units, (2) development of hematite and '
        'illite alteration halos, (3) intersection of brittle basement faults with the unconformity surface, and (4) presence '
        'of clay-rich sandstone units directly above the unconformity. High-grade mineralization (>5% U<sub>3</sub>O<sub>8</sub>) '
        'has been observed in multiple drill holes, with peak grades exceeding 52,000 ppm U<sub>3</sub>O<sub>8</sub> recorded in '
        'hole PLS-22-08 from 398 to 401 metres depth. Mineralization comprises uraninite and coffinite minerals intergrown with '
        'pyrite and associated with clay alteration.'
    )
    story.append(Paragraph(geology_text, body_style))
    story.append(Spacer(1, 0.2 * inch))

    # Section 8: Deposit Types
    story.append(Paragraph('8. Deposit Types', heading_style))
    deposit_text = (
        'The Triple R deposit is classified as a classic unconformity-related uranium deposit, the same deposit type as several '
        'of the world\'s largest and highest-grade uranium systems. Comparable deposits in the Athabasca Basin include McArthur River '
        '(Cameco, 600+ million pounds U<sub>3</sub>O<sub>8</sub> reserves), Cigar Lake (Cameco), and the nearby Arrow deposit (NexGen Resources). '
        'Unconformity-type uranium deposits are formed by precipitation of uranium from migrating groundwaters where chemical reducing agents '
        '(commonly organic carbon and pyrite) in basement rocks create a geochemical trap. Key geological controls on the PLS deposit include: '
        'structural dilation zones at the unconformity interface, graphitic conductors in the basement that serve as fluid conduits, and hydrothermal '
        'alteration creating permeable pathways for ore fluids. The deposit has not yet undergone feasibility studies, and the current resource is '
        'classified as both Indicated (67% of tonnes) and Inferred (33% of tonnes) in accordance with CIM 2014 Mineral Resource and Reserve Definitions.'
    )
    story.append(Paragraph(deposit_text, body_style))
    story.append(Spacer(1, 0.2 * inch))

    story.append(PageBreak())

    # Section 9: Exploration
    story.append(Paragraph('9. Exploration', heading_style))
    exploration_text = (
        'Exploration methodologies employed on the Patterson Lake South property have included: (1) airborne VTEM '
        '(Very Low Frequency electromagnetic) geophysics flown at 25-metre line spacing to define conductive basement '
        'targets, (2) ground-based time-domain electromagnetic (TDEM) surveys over selected high-priority zones, (3) soil and '
        'lake sediment geochemistry sampling to define pathfinder element anomalies, (4) detailed geological mapping and prospecting, '
        'and (5) diamond and reverse-circulation drilling. The VTEM survey identified multiple basement conductors, several of which have '
        'been drill-tested. Airborne radiometric data shows uranium and potassium enrichment above the unconformity in areas of known '
        'mineralization. Soil geochemistry displays multi-element anomalies including elevated molybdenum, vanadium, and selenium in '
        'association with known uranium prospects. To date, 47 drill holes totalling approximately 15,000 metres have been completed, '
        'with 10 holes (3,645 metres) completed in the current 2018-2024 program. All drilling has been conducted to core refusal or '
        'planned termination depth.'
    )
    story.append(Paragraph(exploration_text, body_style))
    story.append(Spacer(1, 0.2 * inch))

    # Section 10: Drilling
    story.append(Paragraph('10. Drilling', heading_style))
    drilling_text = (
        'The current exploration program (2018-2024) comprises 10 diamond and reverse-circulation drill holes totalling 3,645 metres. '
        'All drilling was conducted by an independent, industry-standard drilling contractor using truck-mounted or skid-mounted rigs. '
        'Diamond drilling employed core sizes ranging from HQ to NQ depending on hole conditions and planned final depth. All drill holes '
        'were collared with measured UTM coordinates and elevations using differential GPS. Core was logged in detail by project geologists '
        'at the drill site, photographed both dry and wet, and stored in secure core logging facilities. Geotechnical parameters including '
        'recovery and RQD (rock quality designation) were recorded for all core intervals. Key mineralized intercepts include: (1) hole '
        'PLS-22-08 with 52,000 ppm U<sub>3</sub>O<sub>8</sub> from 398-401 metres, (2) hole PLS-21-05 with 8,500 ppm U<sub>3</sub>O<sub>8</sub> '
        'over 12 metres, and (3) hole PLS-23-04 with multiple lower-grade intervals >0.5% U<sub>3</sub>O<sub>8</sub>. Average hole depth in the '
        'current program is 364.5 metres.'
    )
    story.append(Paragraph(drilling_text, body_style))
    story.append(Spacer(1, 0.2 * inch))

    # Section 11: Sample Preparation, Analyses and Security
    story.append(Paragraph('11. Sample Preparation, Analyses and Security', heading_style))
    sample_text = (
        'Core samples selected for uranium assay were cut in half using a motorized core saw. Sample intervals ranged from 0.5 to 2.0 metres '
        'depending on lithology and visible mineralization. One half of the core was retained for archival purposes; the other half was placed '
        'in pre-numbered assay bags. All samples were shipped under chain-of-custody documentation to SRC Geoanalytical Laboratories in Saskatoon, '
        'Saskatchewan, a commercial accredited laboratory. Uranium was assayed by inductively coupled plasma mass spectrometry (ICP-MS) following '
        'partial acid digestion. Detection limit is 10 ppm U<sub>3</sub>O<sub>8</sub>. Quality assurance and quality control (QA/QC) procedures '
        'included: (1) one blank sample per 20 submitted samples, (2) one duplicate sample per 20 submitted samples, (3) insertion of standard '
        'reference materials with known uranium concentrations. Statistical analysis of QA/QC results indicates no material issues with sample '
        'contamination, cross-contamination, or analytical accuracy. All assay data has been entered into a secure, password-protected relational '
        'database with version control and audit trails.'
    )
    story.append(Paragraph(sample_text, body_style))
    story.append(Spacer(1, 0.2 * inch))

    story.append(PageBreak())

    # Section 12: Data Verification
    story.append(Paragraph('12. Data Verification', heading_style))
    verification_text = (
        'Both Qualified Persons conducted an on-site inspection of the Patterson Lake South property during May 2024. During this visit, '
        'core from selected drill holes was examined in detail, including visual inspection of high-grade intervals. Drill core from holes '
        'PLS-22-08, PLS-21-05, and PLS-23-04 was re-sampled in selected intervals to verify previous assay results. An independent review of '
        'the complete historical assay database was conducted, with cross-checking of approximately 50 individual drill core assay intervals '
        'against the original laboratory assay certificates on file. No discrepancies of material significance were identified between the assay '
        'database and the source laboratory reports. Collar locations for all drill holes were independently verified using differential GPS. '
        'Historical geological logs from the 2013-2018 program were reviewed and compared against core photographs and the current geological '
        'interpretation. The Qualified Persons are satisfied that the data underlying the resource estimation is reliable and of sufficient quality '
        'to support a technical report in accordance with NI 43-101.'
    )
    story.append(Paragraph(verification_text, body_style))
    story.append(Spacer(1, 0.2 * inch))

    # Section 13: Mineral Resource Estimates
    story.append(Paragraph('13. Mineral Resource Estimates', heading_style))
    resource_text = (
        'Mineral resource estimates were prepared by Qualified Person Dr. Sarah Thompson using industry-standard ordinary kriging methodology. '
        'The resource estimation was conducted using a parent cell size of 10 metres by 10 metres by 3 metres (vertical). A minimum cut-off grade '
        'of 0.2% U<sub>3</sub>O<sub>8</sub> was applied to the inferred resource and 0.5% U<sub>3</sub>O<sub>8</sub> to the indicated resource, '
        'reflecting anticipated mining and milling parameters. The Indicated resource is estimated at 5.2 million tonnes at an average grade of '
        '1.52% U<sub>3</sub>O<sub>8</sub>, containing approximately 174 million pounds of contained uranium. The Inferred resource is estimated at '
        '3.8 million tonnes at an average grade of 0.85% U<sub>3</sub>O<sub>8</sub>, containing approximately 71 million pounds of contained uranium. '
        'Resource estimates are reported in accordance with the Canadian Institute of Mining, Metallurgy and Petroleum (CIM) Definition Standards for '
        'Mineral Resources and Reserves, as adopted by the CIM in 2014. The Indicated resource is confined to areas within 50 metres of drill holes '
        'with measured assay intervals; the Inferred resource extends to 150 metres from drill hole control. No Measured resources have been declared.'
    )
    story.append(Paragraph(resource_text, body_style))
    story.append(Spacer(1, 0.2 * inch))

    # Section 14: Adjacent Properties
    story.append(Paragraph('14. Adjacent Properties', heading_style))
    adjacent_text = (
        'The Patterson Lake South property is located in a region with multiple other uranium exploration projects at various stages of development. '
        'The Denison Mines Ltd. Wheeler River project is located approximately 40 kilometres to the east and hosts the Phoenix deposit, also an '
        'unconformity-type uranium system. NexGen Resources\' Arrow deposit is located approximately 8 kilometres to the south and is one of the '
        'largest high-grade uranium deposits ever discovered in the Athabasca Basin with announced resources exceeding 940 million pounds of U<sub>3</sub>O<sub>8</sub>. '
        'The Cameco-operated McArthur River mine is approximately 65 kilometres to the northeast. These adjacent properties demonstrate the regional '
        'prospectivity of the area for unconformity-type uranium mineralization. The Patterson Lake South property benefits from this regional infrastructure '
        'and exploration support, which has facilitated data access and geological understanding of the local geological framework.'
    )
    story.append(Paragraph(adjacent_text, body_style))
    story.append(Spacer(1, 0.2 * inch))

    story.append(PageBreak())

    # Section 15: Other Relevant Data and Information
    story.append(Paragraph('15. Other Relevant Data and Information', heading_style))
    other_text = (
        'Environmental baseline studies are currently being conducted on the Patterson Lake South property under a federal environmental assessment protocol. '
        'To date, baseline water quality, soil, and fish tissue samples have been collected and analyzed. No material environmental concerns have been identified '
        'at this early stage. Indigenous consultation and accommodation activities with local First Nations have been initiated as of 2023, as required by Crown '
        'consultation policy. A preliminary archaeology assessment has been completed with no documented archaeological or heritage sites recorded on the property. '
        'The property is located within the traditional territories of the Fond du Lac Dene Nation and Athabasca Chipewyan First Nation. Ongoing consultation with '
        'these nations is planned to address any concerns and to seek community support for continued exploration. Climate change considerations specific to the '
        'subarctic region, including permafrost stability and access timing, have been factored into the exploration and development planning. No material issues '
        'have been identified that would restrict continued exploration activities on the property.'
    )
    story.append(Paragraph(other_text, body_style))
    story.append(Spacer(1, 0.2 * inch))

    # Section 16: Interpretation and Conclusions
    story.append(Paragraph('16. Interpretation and Conclusions', heading_style))
    interpretation_text = (
        'The Patterson Lake South property is host to a significant high-grade uranium resource within the Triple R deposit. The deposit displays the geological '
        'characteristics of a classic unconformity-related uranium system, comparable to the world-class deposits of the Athabasca Basin. The defined Indicated and '
        'Inferred resources represent approximately 245 million pounds of contained uranium, positioning PLS as a material uranium asset for Fission Uranium Corp. '
        'The current resource classification is based on 47 drill holes distributed over the deposit area, with Indicated resources defined within areas of good '
        'drill control and Inferred resources extending to edge-of-envelope areas. The Qualified Persons believe that the geological setting, mineralization characteristics, '
        'and historical exploration data support the interpretation that significant additional mineralization exists beyond the current resource boundary. Continued '
        'systematic exploration, including deep drilling in areas of basement conductor anomalies, is warranted to test for additional high-grade zones and to expand '
        'the resource footprint. The current deposit geometry and grade distribution suggest that further infill drilling will likely upgrade a portion of the Inferred '
        'resource to Indicated classification.'
    )
    story.append(Paragraph(interpretation_text, body_style))
    story.append(Spacer(1, 0.2 * inch))

    # Section 17: Recommendations
    story.append(Paragraph('17. Recommendations', heading_style))
    recommendations_text = (
        'The Qualified Persons recommend a two-phase exploration program designed to upgrade resource classification and expand the resource footprint. Phase 1 '
        '(Calendar Year 2025) should comprise approximately 12,000 metres of infill diamond drilling targeting areas of Inferred resource adjacent to current '
        'Indicated resource blocks, with the objective of upgrading a substantial portion of inferred tonnes to Indicated classification. Estimated budget for Phase 1: '
        'CAD $8.5 million. Phase 2 (Calendar Year 2026) should comprise approximately 8,000 metres of step-out drilling to test deep basement conductor anomalies and '
        'to extend mineralization beyond the current resource boundary, with emphasis on areas showing early-stage geochemical and geophysical indicators of mineralization. '
        'Estimated budget for Phase 2: CAD $6.2 million. Both phases should include ongoing environmental baseline studies, detailed mineralogical characterization, and '
        'preliminary metallurgical test work to support potential future feasibility studies. The combined Phase 1 and Phase 2 program investment of CAD $14.7 million '
        'is expected to provide sufficient data quality and resource definition to support scoping-level mining and economic studies by the end of 2026.'
    )
    story.append(Paragraph(recommendations_text, body_style))

    # Build PDF
    doc.build(story)
    print(f'\nPDF generated successfully: {output_path}')

    # Verify file
    if os.path.exists(output_path):
        file_size = os.path.getsize(output_path)
        print(f'File size: {file_size:,} bytes ({file_size / 1024:.1f} KB)')

        # Count pages using PyPDF2 if available
        try:
            from PyPDF2 import PdfReader
            with open(output_path, 'rb') as f:
                pdf = PdfReader(f)
                num_pages = len(pdf.pages)
                print(f'Page count: {num_pages}')
        except ImportError:
            print('PyPDF2 not available for page count verification. Install with: pip install PyPDF2')
    else:
        print(f'ERROR: PDF was not created at {output_path}')


if __name__ == '__main__':
    generate_report()
