"""
PPT Generator for 정량적 제안서
Generates a PowerPoint matching the original template format.
"""
import os
from pptx import Presentation
from pptx.util import Inches, Pt, Emu
from pptx.dml.color import RGBColor
from pptx.enum.text import PP_ALIGN
from pptx.util import Inches, Pt
from pptx.oxml.ns import qn
from pptx.oxml import parse_xml
from lxml import etree
import copy

# Colors
COLOR_HEADER_BG = RGBColor(0x1F, 0x38, 0x64)   # Dark blue
COLOR_HEADER_FG = RGBColor(0xFF, 0xFF, 0xFF)   # White
COLOR_GRADE_BG = RGBColor(0xBD, 0xD7, 0xEE)    # Light blue
COLOR_SUMMARY_BG = RGBColor(0xF2, 0xF2, 0xF2)  # Light gray
COLOR_WHITE = RGBColor(0xFF, 0xFF, 0xFF)
COLOR_BLACK = RGBColor(0x00, 0x00, 0x00)
COLOR_BORDER = RGBColor(0x00, 0x00, 0x00)

FONT_NAME = '맑은 고딕'

SLIDE_W = Inches(10)
SLIDE_H = Inches(7.5)


def new_presentation():
    prs = Presentation()
    prs.slide_width = SLIDE_W
    prs.slide_height = SLIDE_H
    return prs


def blank_slide(prs):
    blank_layout = prs.slide_layouts[6]  # completely blank
    return prs.slides.add_slide(blank_layout)


def set_cell_bg(cell, color: RGBColor):
    """Set background color of a table cell."""
    tc = cell._tc
    tcPr = tc.get_or_add_tcPr()
    # Remove existing solidFill
    for existing in tcPr.findall(qn('a:solidFill')):
        tcPr.remove(existing)
    hex_val = str(color)  # RGBColor.__str__ returns hex like '1F3864'
    solidFill = parse_xml(
        f'<a:solidFill xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main">'
        f'<a:srgbClr val="{hex_val}"/>'
        f'</a:solidFill>'
    )
    tcPr.insert(0, solidFill)


def set_cell_border(cell, color: RGBColor = None, width_pt: float = 0.5):
    """Set all borders of a table cell."""
    if color is None:
        color = COLOR_BORDER
    tc = cell._tc
    tcPr = tc.get_or_add_tcPr()
    hex_color = f'{color[0]:02X}{color[1]:02X}{color[2]:02X}'
    w_emu = int(width_pt * 12700)
    border_xml = (
        f'<a:lnL xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main" w="{w_emu}" cap="flat" cmpd="sng">'
        f'<a:solidFill><a:srgbClr val="{hex_color}"/></a:solidFill></a:lnL>'
    )
    for tag in ['a:lnL', 'a:lnR', 'a:lnT', 'a:lnB']:
        for existing in tcPr.findall(qn(tag)):
            tcPr.remove(existing)
    for tag in ['a:lnL', 'a:lnR', 'a:lnT', 'a:lnB']:
        el = parse_xml(border_xml.replace('a:lnL', tag))
        tcPr.append(el)


def add_text_to_cell(cell, text, font_size=11, bold=False, color=None,
                     align=PP_ALIGN.CENTER):
    """Set text in a table cell with formatting."""
    tf = cell.text_frame
    tf.word_wrap = True
    # Clear existing paragraphs
    for para in tf.paragraphs:
        for run in para.runs:
            run.text = ''
    p = tf.paragraphs[0]
    p.alignment = align
    run = p.runs[0] if p.runs else p.add_run()
    run.text = str(text)
    run.font.name = FONT_NAME
    run.font.size = Pt(font_size)
    run.font.bold = bold
    if color:
        run.font.color.rgb = color
    else:
        run.font.color.rgb = COLOR_BLACK


def add_textbox(slide, text, left, top, width, height,
                font_size=14, bold=False, color=None,
                align=PP_ALIGN.CENTER, bg_color=None):
    """Add a text box to a slide."""
    txBox = slide.shapes.add_textbox(left, top, width, height)
    tf = txBox.text_frame
    tf.word_wrap = False
    p = tf.paragraphs[0]
    p.alignment = align
    run = p.add_run()
    run.text = text
    run.font.name = FONT_NAME
    run.font.size = Pt(font_size)
    run.font.bold = bold
    if color:
        run.font.color.rgb = color
    if bg_color:
        fill = txBox.fill
        fill.solid()
        fill.fore_color.rgb = bg_color
    return txBox


def add_table(slide, rows, cols, left, top, width, height):
    """Add a table and return the table shape."""
    shape = slide.shapes.add_table(rows, cols, left, top, width, height)
    return shape.table


def format_amount(amount):
    """Format integer amount with commas."""
    try:
        return f'{int(amount):,}'
    except (ValueError, TypeError):
        return str(amount)


# ─────────────────────────────────────────────
# Slide generators
# ─────────────────────────────────────────────

def make_cover_slide(prs, bid_info):
    """Slide 1: Cover"""
    slide = blank_slide(prs)
    w = SLIDE_W
    h = SLIDE_H

    # Background
    bg = slide.background
    fill = bg.fill
    fill.solid()
    fill.fore_color.rgb = RGBColor(0xFF, 0xFF, 0xFF)

    # Top blue bar
    bar = slide.shapes.add_shape(
        1,  # MSO_SHAPE_TYPE.RECTANGLE
        Inches(0), Inches(0), w, Inches(1.2)
    )
    bar.fill.solid()
    bar.fill.fore_color.rgb = COLOR_HEADER_BG
    bar.line.fill.background()

    # Title text
    add_textbox(slide, '정량적 평가 자기평가서',
                Inches(0.5), Inches(2.0), w - Inches(1), Inches(1.2),
                font_size=32, bold=True, color=COLOR_BLACK,
                align=PP_ALIGN.CENTER)

    # Subtitle: company name
    company = bid_info.get('company_name', '')
    add_textbox(slide, company,
                Inches(0.5), Inches(3.5), w - Inches(1), Inches(0.6),
                font_size=20, bold=False, color=COLOR_BLACK,
                align=PP_ALIGN.CENTER)

    # Date
    bid_date = bid_info.get('bid_date', '')
    add_textbox(slide, bid_date,
                Inches(0.5), Inches(4.2), w - Inches(1), Inches(0.5),
                font_size=16, bold=False, color=RGBColor(0x44, 0x44, 0x44),
                align=PP_ALIGN.CENTER)

    # Bottom label
    add_textbox(slide, '정량적 평가 분야',
                Inches(0.5), Inches(5.2), w - Inches(1), Inches(0.6),
                font_size=18, bold=True, color=COLOR_HEADER_BG,
                align=PP_ALIGN.CENTER)


def make_summary_slide(prs, bid_data):
    """Slide 2: Summary table"""
    slide = blank_slide(prs)
    w = SLIDE_W

    add_textbox(slide, '평가항목 종합',
                Inches(0.3), Inches(0.2), w - Inches(0.6), Inches(0.5),
                font_size=16, bold=True, color=COLOR_HEADER_BG,
                align=PP_ALIGN.LEFT)

    mgmt = bid_data['management']
    exp = bid_data['experience']
    tech = bid_data['technician']
    rep = bid_data['reputation']
    extras = bid_data.get('extra_items', [])
    total = bid_data['total_score']

    # Build rows
    rows_data = [
        ('경영실태', f"{mgmt['max_score']:.1f}점", f"{mgmt['score']:.2f}점"),
        ('수행경험', f"{exp['max_score']:.1f}점", f"{exp['score']:.2f}점"),
        ('기술인력', f"{tech['max_score']:.1f}점", f"{tech['total_score']:.2f}점"),
        ('신인도', f"{rep['max_score']:.1f}점", f"{rep['score']:.2f}점"),
    ]
    for item in extras:
        rows_data.append((item['name'], f"{item['max_score']:.1f}점", f"{item['actual_score']:.2f}점"))

    max_total = (mgmt['max_score'] + exp['max_score'] + tech['max_score'] +
                 rep['max_score'] + sum(i['max_score'] for i in extras))
    rows_data.append(('합계', f"{max_total:.1f}점", f"{total:.2f}점"))

    n_rows = len(rows_data) + 1  # +1 header
    tbl = add_table(slide, n_rows, 3,
                    Inches(1.5), Inches(0.9),
                    Inches(7), Inches(0.45 * n_rows))

    # Column widths
    tbl.columns[0].width = Inches(3)
    tbl.columns[1].width = Inches(2)
    tbl.columns[2].width = Inches(2)

    # Header row
    headers = ['평가항목', '배점', '취득점수']
    for ci, h_text in enumerate(headers):
        cell = tbl.cell(0, ci)
        set_cell_bg(cell, COLOR_HEADER_BG)
        add_text_to_cell(cell, h_text, font_size=11, bold=True,
                         color=COLOR_HEADER_FG)
        set_cell_border(cell)

    for ri, (name, max_s, score) in enumerate(rows_data):
        row_idx = ri + 1
        is_total = (ri == len(rows_data) - 1)
        bg = COLOR_SUMMARY_BG if is_total else COLOR_WHITE
        values = [name, max_s, score]
        for ci, val in enumerate(values):
            cell = tbl.cell(row_idx, ci)
            set_cell_bg(cell, bg)
            add_text_to_cell(cell, val, font_size=11, bold=is_total,
                             color=COLOR_BLACK)
            set_cell_border(cell)


def make_separator_slide(prs, title_text, number):
    """Section separator slide"""
    slide = blank_slide(prs)
    w = SLIDE_W
    h = SLIDE_H

    # Blue rectangle on left
    rect = slide.shapes.add_shape(1, Inches(0), Inches(0), Inches(0.4), h)
    rect.fill.solid()
    rect.fill.fore_color.rgb = COLOR_HEADER_BG
    rect.line.fill.background()

    add_textbox(slide, f'{number}. {title_text}',
                Inches(1.0), Inches(3.0), w - Inches(1.5), Inches(1.0),
                font_size=28, bold=True, color=COLOR_HEADER_BG,
                align=PP_ALIGN.LEFT)


def make_management_slide(prs, mgmt_data):
    """경영실태 slide"""
    slide = blank_slide(prs)
    w = SLIDE_W

    add_textbox(slide, '1. 경영실태 (신용평가등급)',
                Inches(0.3), Inches(0.2), w - Inches(0.6), Inches(0.5),
                font_size=16, bold=True, color=COLOR_HEADER_BG,
                align=PP_ALIGN.LEFT)

    # Table: 3 cols x 3 rows
    tbl = add_table(slide, 3, 3,
                    Inches(1.0), Inches(0.9),
                    Inches(8), Inches(1.5))

    tbl.columns[0].width = Inches(2.5)
    tbl.columns[1].width = Inches(2.5)
    tbl.columns[2].width = Inches(3.0)

    # Header
    headers = ['평가항목', '신용평가등급', '취득점수']
    for ci, h_text in enumerate(headers):
        cell = tbl.cell(0, ci)
        set_cell_bg(cell, COLOR_HEADER_BG)
        add_text_to_cell(cell, h_text, font_size=11, bold=True,
                         color=COLOR_HEADER_FG)
        set_cell_border(cell)

    # Labels row
    labels = ['경영실태', '배점', f"{mgmt_data['max_score']:.1f}점"]
    for ci, val in enumerate(labels):
        cell = tbl.cell(1, ci)
        set_cell_bg(cell, COLOR_SUMMARY_BG)
        add_text_to_cell(cell, val, font_size=11, bold=True)
        set_cell_border(cell)

    # Values row
    values = ['신용평가등급', mgmt_data.get('rating', ''), f"{mgmt_data['score']:.2f}점"]
    for ci, val in enumerate(values):
        cell = tbl.cell(2, ci)
        set_cell_bg(cell, COLOR_WHITE)
        add_text_to_cell(cell, val, font_size=11)
        set_cell_border(cell)


def make_experience_slide(prs, exp_data):
    """수행경험 slide"""
    slide = blank_slide(prs)
    w = SLIDE_W

    add_textbox(slide, '2. 수행경험',
                Inches(0.3), Inches(0.2), w - Inches(0.6), Inches(0.5),
                font_size=16, bold=True, color=COLOR_HEADER_BG,
                align=PP_ALIGN.LEFT)

    projects = exp_data.get('projects', [])
    n_data_rows = max(len(projects), 1)
    n_rows = n_data_rows + 3  # header + summary rows

    tbl = add_table(slide, n_rows, 5,
                    Inches(0.3), Inches(0.9),
                    Inches(9.4), Inches(min(0.4 * n_rows, 5.5)))

    tbl.columns[0].width = Inches(3.0)
    tbl.columns[1].width = Inches(1.8)
    tbl.columns[2].width = Inches(2.0)
    tbl.columns[3].width = Inches(1.3)
    tbl.columns[4].width = Inches(1.3)

    headers = ['사업명', '발주처', '계약금액(VAT포함)', '계약일', '완료일']
    for ci, h_text in enumerate(headers):
        cell = tbl.cell(0, ci)
        set_cell_bg(cell, COLOR_HEADER_BG)
        add_text_to_cell(cell, h_text, font_size=10, bold=True,
                         color=COLOR_HEADER_FG)
        set_cell_border(cell)

    for ri, proj in enumerate(projects):
        row_idx = ri + 1
        vals = [
            proj.get('name', ''),
            proj.get('client', ''),
            format_amount(proj.get('amount', 0)),
            proj.get('contract_date', ''),
            proj.get('completion_date', ''),
        ]
        for ci, val in enumerate(vals):
            cell = tbl.cell(row_idx, ci)
            set_cell_bg(cell, COLOR_WHITE)
            add_text_to_cell(cell, val, font_size=10)
            set_cell_border(cell)

    # Fill empty rows if no projects
    if not projects:
        for ci in range(5):
            cell = tbl.cell(1, ci)
            set_cell_bg(cell, COLOR_WHITE)
            add_text_to_cell(cell, '', font_size=10)
            set_cell_border(cell)

    # Total row
    total_row = n_data_rows + 1
    total_amount = exp_data.get('total_amount', 0)
    total_vals = ['총 실적금액', '', format_amount(total_amount), '', '']
    for ci, val in enumerate(total_vals):
        cell = tbl.cell(total_row, ci)
        set_cell_bg(cell, COLOR_SUMMARY_BG)
        add_text_to_cell(cell, val, font_size=10, bold=True)
        set_cell_border(cell)

    # Score row
    score_row = n_data_rows + 2
    pct = exp_data.get('percentage', 0.0)
    score = exp_data.get('score', 0.0)
    max_s = exp_data.get('max_score', 6.0)
    score_vals = ['평가점수', f'{pct:.1f}%', f'{score:.2f}점 / {max_s:.1f}점', '', '']
    for ci, val in enumerate(score_vals):
        cell = tbl.cell(score_row, ci)
        set_cell_bg(cell, COLOR_GRADE_BG)
        add_text_to_cell(cell, val, font_size=10, bold=True)
        set_cell_border(cell)


def make_technician_overview_slide(prs, tech_data):
    """기술인력 overview table slide"""
    slide = blank_slide(prs)
    w = SLIDE_W

    add_textbox(slide, '3. 기술인력',
                Inches(0.3), Inches(0.2), w - Inches(0.6), Inches(0.5),
                font_size=16, bold=True, color=COLOR_HEADER_BG,
                align=PP_ALIGN.LEFT)

    # Overview table 3 rows x 4 cols (보유/투입 × 등급)
    tbl = add_table(slide, 4, 5,
                    Inches(1.0), Inches(0.9),
                    Inches(8), Inches(1.8))

    tbl.columns[0].width = Inches(1.5)
    tbl.columns[1].width = Inches(1.5)
    tbl.columns[2].width = Inches(1.5)
    tbl.columns[3].width = Inches(1.5)
    tbl.columns[4].width = Inches(2.0)

    # Headers
    for ci, h_text in enumerate(['구분', '특급', '고급', '중급/초급', '합계점수']):
        cell = tbl.cell(0, ci)
        set_cell_bg(cell, COLOR_HEADER_BG)
        add_text_to_cell(cell, h_text, font_size=11, bold=True,
                         color=COLOR_HEADER_FG)
        set_cell_border(cell)

    ret_list = tech_data.get('retention_list', [])
    dep_list = tech_data.get('deployment_list', [])

    def count_grade(lst, grade):
        return sum(1 for t in lst if t.get('grade') == grade)

    def count_mid_low(lst):
        return sum(1 for t in lst if t.get('grade') in ('중급', '초급'))

    # Retention row
    ret_raw = tech_data.get('retention_raw', 0)
    ret_score = tech_data.get('retention_score', 0)
    max_half = tech_data.get('max_score', 6.0) / 2
    ret_vals = ['보유인력',
                str(count_grade(ret_list, '특급')),
                str(count_grade(ret_list, '고급')),
                str(count_mid_low(ret_list)),
                f'{ret_raw:.0f}점 → {ret_score:.2f}점/{max_half:.1f}점']
    for ci, val in enumerate(ret_vals):
        cell = tbl.cell(1, ci)
        set_cell_bg(cell, COLOR_WHITE)
        add_text_to_cell(cell, val, font_size=10)
        set_cell_border(cell)

    # Deployment row
    dep_raw = tech_data.get('deployment_raw', 0)
    dep_score = tech_data.get('deployment_score', 0)
    dep_vals = ['투입인력',
                str(count_grade(dep_list, '특급')),
                str(count_grade(dep_list, '고급')),
                str(count_mid_low(dep_list)),
                f'{dep_raw:.0f}점 → {dep_score:.2f}점/{max_half:.1f}점']
    for ci, val in enumerate(dep_vals):
        cell = tbl.cell(2, ci)
        set_cell_bg(cell, COLOR_WHITE)
        add_text_to_cell(cell, val, font_size=10)
        set_cell_border(cell)

    # Total row
    total_score = tech_data.get('total_score', 0)
    max_total = tech_data.get('max_score', 6.0)
    total_vals = ['합계', '', '', '',
                  f'합계 {total_score:.2f}점 / {max_total:.1f}점']
    for ci, val in enumerate(total_vals):
        cell = tbl.cell(3, ci)
        set_cell_bg(cell, COLOR_SUMMARY_BG)
        add_text_to_cell(cell, val, font_size=10, bold=True)
        set_cell_border(cell)


def make_technician_list_slides(prs, label, tech_list, slide_title_prefix):
    """
    Generate one or more slides for a technician list (보유인력 or 투입인력).
    Splits across slides if more than 25 rows.
    """
    MAX_ROWS_PER_SLIDE = 25
    headers = ['No.', '성명', '자격기준', '등급', '점수', '입사일']
    col_widths = [Inches(0.5), Inches(1.5), Inches(2.5), Inches(1.0), Inches(1.0), Inches(1.5)]

    GRADE_POINTS = {'특급': 4.0, '고급': 3.0, '중급': 2.0, '초급': 1.0}

    # Group by grade
    grade_order = ['특급', '고급', '중급', '초급']
    grouped = {g: [t for t in tech_list if t.get('grade') == g] for g in grade_order}

    # Build flat list with grade separators
    rows_flat = []
    for grade in grade_order:
        members = grouped[grade]
        if members:
            rows_flat.append(('_grade_sep_', grade, '', '', '', ''))
            for i, t in enumerate(members):
                pts = GRADE_POINTS.get(t.get('grade', '초급'), 1.0)
                rows_flat.append((
                    str(i + 1),
                    t.get('name', ''),
                    t.get('qualification', '정보통신기술자'),
                    t.get('grade', ''),
                    f'{pts:.1f}',
                    t.get('hire_date', ''),
                ))

    # Paginate
    pages = []
    current_page = []
    for row in rows_flat:
        if len(current_page) >= MAX_ROWS_PER_SLIDE and row[0] != '_grade_sep_':
            pages.append(current_page)
            current_page = []
        current_page.append(row)
    if current_page:
        pages.append(current_page)

    if not pages:
        pages = [[]]

    for page_idx, page_rows in enumerate(pages):
        slide = blank_slide(prs)
        w = SLIDE_W
        title_suffix = f' ({page_idx + 1}/{len(pages)})' if len(pages) > 1 else ''
        add_textbox(slide, f'{slide_title_prefix} {label} 기술자 목록{title_suffix}',
                    Inches(0.3), Inches(0.2), w - Inches(0.6), Inches(0.5),
                    font_size=14, bold=True, color=COLOR_HEADER_BG,
                    align=PP_ALIGN.LEFT)

        n_rows = len(page_rows) + 1  # +1 for header
        if n_rows < 2:
            n_rows = 2

        row_h = min(0.28 * n_rows + 0.4, 6.5)
        tbl = add_table(slide, n_rows, 6,
                        Inches(0.2), Inches(0.85),
                        Inches(9.6), Inches(row_h))

        for ci, cw in enumerate(col_widths):
            tbl.columns[ci].width = cw

        # Header
        for ci, h_text in enumerate(headers):
            cell = tbl.cell(0, ci)
            set_cell_bg(cell, COLOR_HEADER_BG)
            add_text_to_cell(cell, h_text, font_size=10, bold=True,
                             color=COLOR_HEADER_FG)
            set_cell_border(cell)

        for ri, row_data in enumerate(page_rows):
            row_idx = ri + 1
            is_sep = row_data[0] == '_grade_sep_'
            if is_sep:
                grade_name = row_data[1]
                merged_text = f'[{grade_name}]'
                for ci in range(6):
                    cell = tbl.cell(row_idx, ci)
                    set_cell_bg(cell, COLOR_GRADE_BG)
                    if ci == 0:
                        add_text_to_cell(cell, merged_text, font_size=10, bold=True)
                    else:
                        add_text_to_cell(cell, '', font_size=10, bold=True)
                    set_cell_border(cell)
            else:
                for ci, val in enumerate(row_data):
                    cell = tbl.cell(row_idx, ci)
                    set_cell_bg(cell, COLOR_WHITE)
                    add_text_to_cell(cell, val, font_size=10)
                    set_cell_border(cell)


def make_reputation_slide(prs, rep_data):
    """신인도 slide"""
    slide = blank_slide(prs)
    w = SLIDE_W

    add_textbox(slide, '4. 신인도',
                Inches(0.3), Inches(0.2), w - Inches(0.6), Inches(0.5),
                font_size=16, bold=True, color=COLOR_HEADER_BG,
                align=PP_ALIGN.LEFT)

    status = rep_data.get('status', 'none')
    score = rep_data.get('score', 0.0)
    max_s = rep_data.get('max_score', 2.0)

    status_labels = {
        'none': '입찰참가자격 제한사실 없음',
        'restricted': '입찰참가자격 제한사실 있음',
        'no_docs': '증빙자료 미제출',
    }
    status_text = status_labels.get(status, '알 수 없음')

    tbl = add_table(slide, 3, 3,
                    Inches(1.0), Inches(0.9),
                    Inches(8), Inches(1.5))

    tbl.columns[0].width = Inches(2.5)
    tbl.columns[1].width = Inches(3.0)
    tbl.columns[2].width = Inches(2.5)

    for ci, h_text in enumerate(['평가항목', '현황', '취득점수']):
        cell = tbl.cell(0, ci)
        set_cell_bg(cell, COLOR_HEADER_BG)
        add_text_to_cell(cell, h_text, font_size=11, bold=True,
                         color=COLOR_HEADER_FG)
        set_cell_border(cell)

    for ci, val in enumerate(['신인도', '배점', f'{max_s:.1f}점']):
        cell = tbl.cell(1, ci)
        set_cell_bg(cell, COLOR_SUMMARY_BG)
        add_text_to_cell(cell, val, font_size=11, bold=True)
        set_cell_border(cell)

    for ci, val in enumerate(['입찰참가자격 제한 현황', status_text, f'{score:.2f}점']):
        cell = tbl.cell(2, ci)
        set_cell_bg(cell, COLOR_WHITE)
        add_text_to_cell(cell, val, font_size=11)
        set_cell_border(cell)


def make_extra_item_slide(prs, item, number):
    """Extra item detail slide"""
    slide = blank_slide(prs)
    w = SLIDE_W

    add_textbox(slide, f'{number}. {item["name"]}',
                Inches(0.3), Inches(0.2), w - Inches(0.6), Inches(0.5),
                font_size=16, bold=True, color=COLOR_HEADER_BG,
                align=PP_ALIGN.LEFT)

    tbl = add_table(slide, 3, 3,
                    Inches(1.0), Inches(0.9),
                    Inches(8), Inches(1.5))

    tbl.columns[0].width = Inches(2.5)
    tbl.columns[1].width = Inches(2.5)
    tbl.columns[2].width = Inches(3.0)

    for ci, h_text in enumerate(['항목명', '배점', '취득점수']):
        cell = tbl.cell(0, ci)
        set_cell_bg(cell, COLOR_HEADER_BG)
        add_text_to_cell(cell, h_text, font_size=11, bold=True,
                         color=COLOR_HEADER_FG)
        set_cell_border(cell)

    for ci, val in enumerate([item['name'], f"{item['max_score']:.1f}점",
                               f"{item['actual_score']:.2f}점"]):
        cell = tbl.cell(1, ci)
        set_cell_bg(cell, COLOR_SUMMARY_BG)
        add_text_to_cell(cell, val, font_size=11, bold=True)
        set_cell_border(cell)

    desc = item.get('description', '')
    for ci, val in enumerate(['설명', desc, '']):
        cell = tbl.cell(2, ci)
        set_cell_bg(cell, COLOR_WHITE)
        add_text_to_cell(cell, val, font_size=11)
        set_cell_border(cell)


# ─────────────────────────────────────────────
# Main generate function
# ─────────────────────────────────────────────

def generate_ppt(bid_data: dict, output_path: str):
    """
    Generate the full PPT file.

    Args:
        bid_data: dict containing bid_info, management, experience, technician,
                  reputation, extra_items, total_score
        output_path: file path to save the .pptx file
    """
    prs = new_presentation()

    bid_info = bid_data.get('bid_info', {})
    mgmt = bid_data.get('management', {})
    exp = bid_data.get('experience', {})
    tech = bid_data.get('technician', {})
    rep = bid_data.get('reputation', {})
    extras = bid_data.get('extra_items', [])

    # Slide 1: Cover
    make_cover_slide(prs, bid_info)

    # Slide 2: Summary
    make_summary_slide(prs, bid_data)

    # Slide 3: 경영실태 separator
    make_separator_slide(prs, '경영실태', '1')

    # Slide 4: 경영실태 table
    make_management_slide(prs, mgmt)

    # Slide 5: 수행경험 separator
    make_separator_slide(prs, '수행경험', '2')

    # Slide 6: 수행경험 table
    make_experience_slide(prs, exp)

    # Slide 7: 기술인력 separator
    make_separator_slide(prs, '기술인력', '3')

    # Slide 8: 기술인력 overview
    make_technician_overview_slide(prs, tech)

    # Slides 9+: 보유인력 list
    make_technician_list_slides(prs, '보유인력',
                                tech.get('retention_list', []),
                                '3.')

    # Slides: 투입인력 list
    make_technician_list_slides(prs, '투입인력',
                                tech.get('deployment_list', []),
                                '3.')

    # 신인도 separator
    make_separator_slide(prs, '신인도', '4')

    # 신인도 table
    make_reputation_slide(prs, rep)

    # Extra items
    for idx, item in enumerate(extras):
        num = idx + 5
        make_separator_slide(prs, item['name'], str(num))
        make_extra_item_slide(prs, item, str(num))

    os.makedirs(os.path.dirname(os.path.abspath(output_path)), exist_ok=True)
    prs.save(output_path)
    return output_path
