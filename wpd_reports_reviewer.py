"""Reviewer-first Word presentation for WPD verifier.

Verification data and statuses are not changed here. The report prioritises
actual findings and independently calibrated CSV/source overlays, while keeping
technical diagnostics in records.json. A TAR-derived preview never counts as
Level 2 evidence and is deliberately excluded from these Word reports.
"""
from __future__ import annotations

from collections import Counter, defaultdict
from pathlib import Path
from typing import Any
import re

from docx import Document
from docx.enum.table import WD_TABLE_ALIGNMENT, WD_CELL_VERTICAL_ALIGNMENT
from docx.enum.text import WD_ALIGN_PARAGRAPH
from docx.oxml import OxmlElement
from docx.oxml.ns import qn
from docx.shared import Inches, Pt, RGBColor
from PIL import Image

import wpd_reports as base
from wpd_report_findings import collect_findings, marker_reason, axis_setting_rows, plain_requirement_comment, plain_file_comment

NAVY = '243E56'
INK = '162C3C'
SOFT = '607386'
GREEN = 'E6F2E9'
YELLOW = 'FFF1CB'
RED = 'F9E2E2'
WHITE = 'FFFFFF'
BORDER = 'C6D2DB'
FAMILIES = ('CC', 'OC', 'TC')
STATUS_COLORS = {'PASS': GREEN, 'FAIL': RED, 'REVIEW': YELLOW, 'OK': GREEN,
                 'WARNING': YELLOW, 'ABSENT': YELLOW, 'PRESENT': GREEN}


def get(o, k, default=None):
    return base._get(o, k, default)


def status(o, key='status'):
    return base._status(o, key)


def short(value, length=94):
    s = ' '.join(str(value or '').split())
    return s if len(s) <= length else s[:length - 3].rstrip() + '...'


def paras(doc, string, *, bold=False, small=False, keep=False, color=INK):
    p = doc.add_paragraph()
    p.paragraph_format.space_after = Pt(4 if not small else 3)
    p.paragraph_format.keep_with_next = keep
    r = p.add_run(str(string))
    r.font.name = 'Arial'; r.font.size = Pt(9.4 if not small else 8.3)
    r.bold = bold; r.font.color.rgb = RGBColor.from_string(color)
    return p


def label(doc, title, value, *, small=True, keep=False):
    p = doc.add_paragraph()
    p.paragraph_format.space_after = Pt(3)
    p.paragraph_format.keep_with_next = keep
    a = p.add_run(title + '  '); a.bold = True
    a.font.size = Pt(8.4 if small else 9.5)
    b = p.add_run(str(value)); b.font.size = a.font.size
    return p


def heading(doc, string, level=1):
    p = doc.add_heading(string, level)
    p.paragraph_format.keep_with_next = True
    p.paragraph_format.space_before = Pt(8 if level == 1 else 6)
    p.paragraph_format.space_after = Pt(5)
    return p


def shade(cell, fill):
    tc_pr = cell._tc.get_or_add_tcPr()
    shd = OxmlElement('w:shd'); shd.set(qn('w:fill'), fill); tc_pr.append(shd)


def set_borders(table):
    pr = table._tbl.tblPr
    tb = OxmlElement('w:tblBorders')
    for edge in ('top', 'left', 'bottom', 'right', 'insideH', 'insideV'):
        node = OxmlElement('w:' + edge)
        node.set(qn('w:val'), 'single')
        node.set(qn('w:sz'), '3')
        node.set(qn('w:color'), BORDER)
        tb.append(node)
    pr.append(tb)


def table(doc, columns, rows, widths=None, font=8.0):
    rows = list(rows)
    t = doc.add_table(rows=1, cols=len(columns))
    t.alignment = WD_TABLE_ALIGNMENT.CENTER; t.autofit = False
    widths = widths or [7.1/len(columns)]*len(columns)
    for i, w in enumerate(widths): t.columns[i].width = Inches(w)
    set_borders(t)
    repeat = OxmlElement('w:tblHeader'); repeat.set(qn('w:val'), 'true')
    t.rows[0]._tr.get_or_add_trPr().append(repeat)
    for ri, values in enumerate([columns] + rows):
        row = t.rows[0] if ri == 0 else t.add_row()
        if sum(len(str(v)) for v in values) < 750:
            row._tr.get_or_add_trPr().append(OxmlElement('w:cantSplit'))
        for ci, cell in enumerate(row.cells):
            text = str(values[ci]) if ci < len(values) else ''
            cell.width = Inches(widths[ci]); cell.vertical_alignment = WD_CELL_VERTICAL_ALIGNMENT.CENTER
            tc_pr = cell._tc.get_or_add_tcPr()
            mar = OxmlElement('w:tcMar')
            for side, val in (('top', '75'), ('bottom', '75'), ('left', '85'), ('right', '85')):
                node = OxmlElement('w:' + side); node.set(qn('w:w'), val)
                node.set(qn('w:type'), 'dxa'); mar.append(node)
            tc_pr.append(mar)
            st = text.strip().upper()
            shade(cell, NAVY if ri == 0 else STATUS_COLORS.get(st, WHITE))
            p = cell.paragraphs[0]
            p.paragraph_format.space_after = Pt(0)
            p.paragraph_format.line_spacing = 1.05
            if (ri == 0 or st in STATUS_COLORS or re.fullmatch(r'\d+', st)):
                p.alignment = WD_ALIGN_PARAGRAPH.CENTER
            run = p.add_run(text); run.font.name = 'Arial'
            run.font.size = Pt(font)
            run.bold = ri == 0 or st in STATUS_COLORS
            run.font.color.rgb = RGBColor.from_string(WHITE if ri == 0 else INK)
    doc.add_paragraph().paragraph_format.space_after = Pt(0)
    return t


def picture_grid(doc, items, captions_font=7.5):
    """Draw only existing evidence images; one or two per row, no placeholders."""
    pictures = [(Path(str(path)), caption) for path, caption in items if path and Path(str(path)).is_file()]
    if not pictures: return 0
    for start in range(0, len(pictures), 2):
        pair = pictures[start:start+2]
        t = doc.add_table(rows=1, cols=2)
        t.autofit = False
        for ci in range(2):
            t.columns[ci].width = Inches(3.52)
            cell = t.cell(0, ci); cell.width = Inches(3.52)
            if ci >= len(pair): continue
            image, caption = pair[ci]
            p = cell.paragraphs[0]; p.alignment = WD_ALIGN_PARAGRAPH.CENTER
            p.paragraph_format.space_after = Pt(3)
            rr = p.add_run(short(caption, 66)); rr.font.size = Pt(captions_font)
            rr.bold = True; rr.font.color.rgb = RGBColor.from_string(INK)
            try:
                with Image.open(image) as im: iw, ih = im.size
                # Do not exaggerate tiny images. Limit height, as well as width.
                scale = min(3.32 * 96 / max(iw, 1), 2.53 * 96 / max(ih, 1), 1.0)
                p = cell.add_paragraph(); p.alignment = WD_ALIGN_PARAGRAPH.CENTER
                p.paragraph_format.space_after = Pt(0)
                run = p.add_run(); run.add_picture(str(image), width=Inches(iw * scale / 96))
            except Exception:
                p = cell.add_paragraph('Image unavailable or damaged.'); p.style = 'Caption'
        t.rows[0]._tr.get_or_add_trPr().append(OxmlElement('w:cantSplit'))
        doc.add_paragraph().paragraph_format.space_after = Pt(0)
    return len(pictures)


def new_doc(device, purpose):
    d = Document()
    sec = d.sections[0]
    sec.page_width = Inches(8.5); sec.page_height = Inches(11)
    sec.left_margin = sec.right_margin = Inches(.7)
    sec.top_margin = Inches(.64); sec.bottom_margin = Inches(.58)
    sec.header_distance = Inches(.27); sec.footer_distance = Inches(.28)
    normal = d.styles['Normal']; normal.font.name = 'Arial'; normal.font.size = Pt(9.4)
    normal.font.color.rgb = RGBColor.from_string(INK)
    normal.paragraph_format.space_after = Pt(3)
    normal.paragraph_format.line_spacing = 1.04
    for name, size in (('Heading 1', 11.5), ('Heading 2', 10.2)):
        s = d.styles[name]; s.font.name = 'Arial'; s.font.size = Pt(size)
        s.font.bold = True; s.font.color.rgb = RGBColor.from_string(NAVY)
        s.paragraph_format.keep_with_next = True
    hp = sec.header.paragraphs[0]; hp.alignment = WD_ALIGN_PARAGRAPH.RIGHT
    run = hp.add_run('DEVICE VERIFICATION  /  REVIEWER REPORT')
    run.font.size = Pt(7.2); run.font.color.rgb = RGBColor.from_string(SOFT)
    fp = sec.footer.paragraphs[0]; fp.alignment = WD_ALIGN_PARAGRAPH.RIGHT
    rr = fp.add_run(str(device) + '  |  PAGE '); rr.font.size = Pt(7.2)
    field = OxmlElement('w:fldSimple'); field.set(qn('w:instr'), 'PAGE'); fp._p.append(field)
    d.core_properties.title = 'Device Digitization Verification - '+str(device)
    d.core_properties.author = 'WPD Verifier'
    d.core_properties.subject = purpose
    return d


def overview_status(projects, level2, family, level):
    subset = [p for p in (projects if level == 1 else level2) if get(p, 'family', '').upper() == family]
    if not subset: return 'REVIEW'
    return base._aggregate([status(p, 'overall_status') for p in subset])


def metadata_and_summary(doc, dr, *, separate=False):
    p = doc.add_paragraph()
    p.paragraph_format.space_after = Pt(2)
    rr = p.add_run('DEVICE DIGITIZATION VERIFICATION'); rr.bold = True
    rr.font.size = Pt(8.0); rr.font.color.rgb = RGBColor.from_string('276080')
    p = doc.add_paragraph(); p.paragraph_format.space_after = Pt(5)
    rr = p.add_run(str(get(dr, 'device', ''))); rr.font.size = Pt(19)
    rr.bold = True; rr.font.color.rgb = RGBColor.from_string(NAVY)
    p = paras(doc, f"Intern: {get(dr,'intern','')}   |   Source folder: {get(dr,'device_relpath',get(dr,'path',''))}", small=True)
    p.paragraph_format.space_after = Pt(9)
    heading(doc, 'Verification at a glance')
    projects = get(dr,'projects',[]) or []
    l2 = get(dr,'level2',[]) or []
    rows = [[f, overview_status(projects,l2,f,1),overview_status(projects,l2,f,2),status(dr,f.lower()+'_status')] for f in FAMILIES]
    rows.append(['OVERALL','','',status(dr,'overall_status')])
    table(doc, ['Characteristic','Level 1 / TAR','Level 2 / CSV','Final'], rows,
          [1.63, 1.85, 1.84, 1.78], 9)
    paras(doc,'PASS = verified check; FAIL = confirmed problem; REVIEW = further evidence or inspection required.', small=True)


def findings_block(doc, dr, max_items=4):
    heading(doc, 'What needs attention')
    findings = collect_findings(dr)
    severe = [f for f in findings if f['status']=='FAIL' and f.get('section') not in {'export','filename'}]
    others = [f for f in findings if f['status']=='REVIEW' and f.get('section') not in {'export','filename'}]
    filenames = [f for f in findings if f.get('section') == 'filename' and f['status']!='PASS']
    ranked = severe + others + filenames
    # Reduce generic and repeated findings before the reader reaches technical pages.
    seen = set(); chosen=[]
    for f in ranked:
        title = str(f.get('title') or '').strip()
        family = str(f.get('family') or '').strip()
        key=(family,title)
        if key in seen: continue
        seen.add(key); chosen.append(f)
        if len(chosen)>=max_items:break
    if not chosen:
        paras(doc, 'No specific correction was recorded. Consult Level 1 and Level 2 for outstanding verification results.')
        return
    for i,f in enumerate(chosen,1):
        family=str(f.get('family') or '').strip()
        issue = str(f.get('detail') or '').strip()
        action = str(f.get('action') or '').strip()
        title = str(f.get('title') or '').strip()
        text = f"{i}. {f['status']}" + (f' / {family}' if family not in {'','General'} else '') + f': {title}.'
        if issue: text += ' ' + short(issue, 135)
        if action and action.lower() not in issue.lower(): text += ' Next: '+short(action,85)
        paras(doc,text,small=True)
    if len(ranked)>len(chosen):
        paras(doc,'Other recorded items are retained in the detailed Level 1 / Level 2 checks and audit records.',small=True)


def availability_block(doc, dr):
    heading(doc, 'File availability')
    files=get(dr,'availability',{}) or {}
    pdfs=get(dr,'datasheets',[]) or []
    label(doc,'Datasheet PDF:', ('Present - '+ '; '.join(map(str,pdfs))) if pdfs else 'Absent',small=True)
    rows=[[family, 'PRESENT' if files.get(family+'_IMAGE')=='PRESENT' else 'ABSENT' if files.get(family+'_IMAGE')=='MISSING' else files.get(family+'_IMAGE','REVIEW'),
           'PRESENT' if files.get(family+'_TAR')=='PRESENT' else 'ABSENT' if files.get(family+'_TAR')=='MISSING' else files.get(family+'_TAR','REVIEW'),
           'PRESENT' if files.get(family+'_CSV')=='PRESENT' else 'ABSENT' if files.get(family+'_CSV')=='MISSING' else files.get(family+'_CSV','REVIEW')]
          for family in FAMILIES]
    table(doc,['Characteristic','Standalone image','WPD TAR','CSV exports'],rows,[1.36,2.0,1.8,1.94],8.2)


def _requirements_summary(reqs, category):
    rows = [q for q in reqs if get(q,'category')==category]
    counts = Counter(status(q) for q in rows)
    if not rows: return 'REVIEW', 'No comparison results saved.'
    if counts['FAIL']: result='FAIL'
    elif counts['REVIEW']:result='REVIEW'
    else: result='PASS'
    if result == 'PASS':
        rounded = sum(get(q,'issue_code') == 'CSV_ROUNDING_PASS' for q in rows)
        if rounded:
            return result, f'All {len(rows)} CSV exports agree with the saved WPD points; {rounded} agree after ordinary rounding to their written decimal precision.'
        return result, f'All {len(rows)} checks passed.'
    problems = [q for q in rows if status(q) in {'FAIL','REVIEW'}]
    if category == 'CSV export consistency' and all(get(q,'issue_code')=='CSV_ROUNDING_REVIEW' for q in problems):
        return result, f'{len(problems)} CSVs match their WPD points at the CSV written decimal precision; the acceptable precision has not been specified.'
    first=problems[0]
    return result, short(f"{len(problems)} item(s) need attention. {get(first,'requirement','')}: {plain_requirement_comment(first)}",180)


def file_checks_block(doc,dr):
    heading(doc,'Filename and CSV export checks')
    audits=list(get(dr,'files',[]) or [])
    reqs=list(get(dr,'requirements',[]) or [])
    bad=[a for a in audits if status(a) not in {'PASS','OK','WARNING'}]
    filename_status=base._aggregate([status(a) for a in audits]) if audits else 'REVIEW'
    label_status,label_note=_requirements_summary(reqs,'Dataset filename correspondence')
    export_status,export_note=_requirements_summary(reqs,'CSV export consistency')
    fname_note=f'{len(audits)-len(bad)} of {len(audits)} file names follow the expected convention.' if not bad else (
        f'{len(bad)} file name(s) need attention: ' + short('; '.join(f"{get(a,'current_filename')}: {plain_file_comment(a)}" for a in bad),160))
    table(doc,['Check','Result','Reviewer note'],[
        ['Folder filename checks',filename_status,fname_note],
        ['Dataset-to-CSV labels',label_status,label_note],
        ['CSV numeric export',export_status,export_note],
    ],[2.25,1.0,3.85],8.2)
    flagged=[(str(get(a,'current_filename','')),status(a),short(plain_file_comment(a),115)) for a in bad]
    if flagged:
        label(doc,'Files to inspect:', '',small=True)
        table(doc,['File','Status','Reason / next action'],flagged,[2.8,.85,3.45],7.8)


def marker_table(doc,project):
    from wpd_reports_compact import _marker_rows
    rows=_marker_rows(project)
    if not rows:
        paras(doc,'Calibration marker details were not saved.',small=True)
        return
    simplified=[]
    for mark,assign,source,pos,val,reason in rows:
        state=base._aggregate([pos,val])
        # An interpolated estimate is NOT a measured tick label. Keep the
        # reviewer-facing source-value field factual.
        if '(Estimate)' in source:
            source='Not confirmed'
        if state == 'PASS':
            reason='Aligned with the source tick; assigned value matches.'
        elif state == 'REVIEW':
            if pos == 'REVIEW' and 'marker is' in reason.lower():
                reason='Marker placement may be off the intended tick. Check the position and tick label in WPD.'
            elif source == 'Not confirmed':
                reason='The source tick or its label could not be confirmed. Check this marker against the graph.'
            elif not str(reason).strip() or str(reason).strip() == 'REVIEW':
                reason='Marker or tick alignment could not be established; inspect the source image.'
        elif not str(reason).strip():
            reason='Incorrect marker placement or assigned value; correct using the source graph.'
        simplified.append([mark,assign,source,state,reason])
    table(doc,['Marker','In TAR','On source','Result','Plain-English finding'],simplified,[.58,.77,1.0,.68,4.07],7.8)


def _point_count(ds):
    value=get(ds,'points',[])
    if isinstance(value,(list,tuple)): return len(value)
    return int(value or 0) if str(value or '').isdecimal() else '—'


def _project_note(p):
    statuses=[(s,label) for s,label in [
        (status(p,'graph_type_status'),'graph type'),(status(p,'scale_status'),'axis scales'),
        (status(p,'calibration_status'),'calibration'),(status(p,'dataset_status'),'curve identity'),
        (status(p,'trace_status'),'curve placement'),(status(p,'completeness_status'),'coverage')]]
    failed=[label for s,label in statuses if s=='FAIL']
    unknown=[label for s,label in statuses if s=='REVIEW']
    if failed:return 'FAIL: Check ' + ', '.join(failed) + '. See marker/curve findings below.'
    if unknown:return 'REVIEW: ' + ', '.join(unknown) + ' need confirmation.'
    return 'PASS: All recorded WPD project checks are supported.'


def level1(doc, dr):
    projects=list(get(dr,'projects',[]) or [])
    if not projects:
        doc.add_page_break(); heading(doc,'LEVEL 1 / WPD VERIFICATION')
        paras(doc,'No WPD TAR project was identified in this device folder. Level 1 remains REVIEW.')
        return
    for p in projects:
        doc.add_page_break()
        paras(doc,'LEVEL 1 / WPD VERIFICATION',small=True,bold=True,keep=True,color='276080')
        fam=str(get(p,'family','General'))
        nm={'CC':'Capacitance characteristics','OC':'Output characteristics','TC':'Transfer characteristics'}.get(fam, 'Graph verification')
        heading(doc,f'{fam} - {nm}')
        label(doc,'TAR:',get(p,'project',''),small=True)
        label(doc,'Embedded source:',get(p,'image_name','Not recorded'),small=True)
        paras(doc, _project_note(p), small=True)
        scales=[[get(p,'graph_type',''),get(p,'x_scale',''),get(p,'y_scale',''),status(p,'scale_status')]]
        table(doc,['Graph type','X scale in TAR','Y scale in TAR','Scale check'],scales,[1.52,1.97,1.96,1.65],8.1)
        # Read separately; do not pretend the saved setting independently validates the source.
        axis_rows,_=axis_setting_rows(p)
        nonpass=[row for row in axis_rows if len(row)>=4 and row[3]!='PASS']
        if nonpass:
            for row in nonpass[:2]: paras(doc,'Source-axis confirmation: '+short(str(row[-1]),190),small=True)
        heading(doc,'Calibration markers',2)
        marker_table(doc,p)
        heading(doc,'Source graph and WPD extraction',2)
        added=picture_grid(doc,[(get(p,'calibration_visual',''),'X1/X2/Y1/Y2 marker positions'),
                                (get(p,'extraction_visual',''),'Saved WPD extraction points')])
        if not added:paras(doc,'Graph evidence image is unavailable; check the source TAR.',small=True)
        ds=list(get(p,'datasets',[]) or [])
        if ds:
            dataset_rows = [[get(s,'name',''),_point_count(s),status(s,'mapping_status'),status(s,'trace_status'),status(s,'completeness_status')] for s in ds]
            heading(doc,'Dataset identity and coverage',2)
            table(doc,['Dataset','Points','Identity','Trace','Coverage'],dataset_rows,[3.65,.55,.95,.9,1.05],7.6)
        outstanding = [short(str(x),190) for x in (get(p,'comments',[]) or []) if ('FAIL' in str(x).upper() or 'incorrect' in str(x).lower() or 'wrong' in str(x).lower())]
        if outstanding:
            for x in outstanding[:3]: paras(doc,'Finding: '+x,small=True)


def _csv_name(name):
    name=base._basename(name)
    for pre in ('_OC_Vgs_','_TC_'):
        if pre in name:return pre.strip('_') + ' ' + name.split(pre,1)[1].replace('.csv','')
    return name


def level2(doc,dr):
    items=list(get(dr,'level2',[]) or [])
    doc.add_page_break()
    paras(doc,'LEVEL 2 / INDEPENDENT CSV VERIFICATION',small=True,bold=True,keep=True,color='276080')
    heading(doc,'CSV plotted over standalone source images')
    paras(doc,'A verified Level 2 overlay uses only axis values read from the standalone image. Saved WPD calibration is not used. A successfully mapped axis alone does not establish that the correct source curve was digitized.',small=True)
    if not items:
        paras(doc,'No CSV export was found. Level 2 is not completed.',small=True)
        return
    rows=[]
    for q in items:
        kind=str(get(q,'overlay_kind','UNAVAILABLE'))
        can_show=kind=='INDEPENDENT_AXES' and get(q,'overlay_visual') and Path(str(get(q,'overlay_visual'))).is_file()
        mapped='PASS' if can_show else status(q,'axis_status')
        rows.append([_csv_name(get(q,'csv_file','')),base._basename(get(q,'source_image','')) if get(q,'source_image') else 'Absent',
                     str(get(q,'point_count',0)),mapped,status(q,'trace_status'),status(q,'overall_status')])
    table(doc,['CSV curve','Source','Points','Image axes','Curve/coverage','Level 2'],rows,[2.21,1.05,.55,1.1,1.1,1.09],7.2)
    plots=defaultdict(list)
    missing=[]
    for q in items:
        fam=get(q,'family','General')
        path=get(q,'overlay_visual','')
        if get(q,'overlay_kind')=='INDEPENDENT_AXES' and path and Path(str(path)).is_file():
            plots[fam].append((path, _csv_name(get(q,'csv_file',''))+' - '+status(q,'overall_status')))
        else:
            details='; '.join(str(c) for c in (get(q,'comments',[]) or []) if 'axis' in str(c).lower() or 'source' in str(c).lower())
            missing.append((get(q,'csv_file',''), fam, short(details,130) or 'An independently calibrated CSV overlay is not available.'))
    for fam in FAMILIES:
        if not plots[fam]:continue
        # Split gallery by family; Word handles page breaks when a gallery grows.
        heading(doc,f'{fam}: independently mapped CSV points',2)
        picture_grid(doc,plots[fam])
    if missing:
        heading(doc,'Level 2 checks requiring attention',2)
        for name,fam,why in missing:
            paras(doc,f"REVIEW / {fam} / {name}: {why} Confirm the source-image axes before judging the CSV trace.",small=True)
    paras(doc,'A WPD-derived preview is not independent evidence and is never displayed as a completed Level 2 overlay.',small=True)


def write_reviewer_device_reports(dr:Any, device_out:str|Path, separate_levels:bool=False)->dict[str,str]:
    out=Path(device_out);out.mkdir(parents=True,exist_ok=True)
    stem=base._safe_filename(str(get(dr,'device','Device')))
    paths={name:out/(stem+'_'+suffix+'.docx') for name,suffix in (
        ('consolidated','Consolidated_Device_Report'),('level1','Level1_Report'),('level2','Level2_Report'))}
    for name,attr in (('consolidated','report_docx'),('level1','level1_report_docx'),('level2','level2_report_docx')):
        val=str(paths[name]) if name=='consolidated' or separate_levels else ''
        if isinstance(dr,dict):dr[attr]=val
        else:setattr(dr,attr,val)
    kinds=['consolidated']+(['level1','level2'] if separate_levels else [])
    for kind in kinds:
        doc=new_doc(get(dr,'device',''),'WPD device verification')
        metadata_and_summary(doc,dr)
        if kind=='consolidated':
            findings_block(doc,dr)
            availability_block(doc,dr)
            file_checks_block(doc,dr)
        elif kind=='level1':
            availability_block(doc,dr)
            file_checks_block(doc,dr)
        elif kind=='level2':
            availability_block(doc,dr)
            file_checks_block(doc,dr)
        if kind in {'consolidated','level1'}:level1(doc,dr)
        if kind in {'consolidated','level2'}:level2(doc,dr)
        doc.save(paths[kind])
    return {key:(str(path) if key=='consolidated' or separate_levels else '') for key,path in paths.items()}


def family_review_comment(dr:Any, family:str)->str:
    """One plain-English Excel/intern summary, not raw OCR/mapping logs."""
    reqs=[q for q in (get(dr,'requirements',[]) or []) if get(q,'family')==family]
    audits=[q for q in (get(dr,'files',[]) or []) if get(q,'family')==family]
    projects=[q for q in (get(dr,'projects',[]) or []) if get(q,'family')==family]
    csvs=[q for q in (get(dr,'level2',[]) or []) if get(q,'family')==family]
    parts=[]
    for q in reqs:
        if status(q)=='FAIL':
            parts.append('FAIL: '+short(plain_requirement_comment(q),160))
    for q in audits:
        if status(q)=='FAIL':
            parts.append('FAIL: '+base._basename(get(q,'current_filename',''))+' - '+short(plain_file_comment(q),145))
    for p in projects:
        if status(p,'overall_status')=='FAIL':
            bad=[]
            for c in (get(p,'calibration_checks',[]) or []):
                if 'FAIL' in {status(c),status(c,'geometry_status'),status(c,'value_status')}:
                    bad.append(f"{get(c,'role','marker')} incorrectly calibrated")
            if status(p,'scale_status')=='FAIL': bad.append('linear/log scale incorrect')
            if bad:parts.append('FAIL Level 1 '+base._basename(get(p,'project'))+': '+', '.join(bad))
    for c in csvs:
        if status(c,'overall_status')=='FAIL':
            parts.append('FAIL Level 2 '+base._basename(get(c,'csv_file'))+': check numerical values, source-axis projection and curve placement.')
    if parts:
        return '\n'.join(parts[:3])+(f'\n(+{len(parts)-3} other findings: see Word report)' if len(parts)>3 else '')
    if status(dr,family.lower()+'_status')=='PASS':return 'PASS: no issues identified in completed checks.'
    missing=(get(dr,'availability',{}) or {}).get(family+'_IMAGE')=='MISSING'
    if missing: parts.append('Standalone '+family+' source image missing from the folder.')
    if not projects: parts.append('Level 1: WPD TAR check unavailable.')
    else:
        if any(status(q,'calibration_status')!='PASS' for q in projects):
            parts.append('Level 1: one or more calibration markers or axis scales need confirmation.')
        if any(status(q,'dataset_status')!='PASS' or status(q,'completeness_status')!='PASS' for q in projects):
            parts.append('Level 1: physical curve identity and full-range extraction require review.')
    unplotted=[q for q in csvs if status(q,'axis_status')!='PASS']
    if not csvs:parts.append('Level 2: no suitable '+family+' CSV found.')
    elif unplotted:
        parts.append(f'Level 2: {len(unplotted)} of {len(csvs)} CSVs lack confirmed independent image-axis mapping.')
    if any(status(q,'trace_status')!='PASS' for q in csvs):
        parts.append('Level 2: source-curve alignment and coverage are not fully certified.')
    unclear=[q for q in audits if status(q)=='REVIEW']
    if unclear:
        parts.append('Filename: '+base._basename(get(unclear[0],'current_filename',''))+' needs confirmation.')
    return '\n'.join(parts[:5]) or 'REVIEW: check the individual device Word report for unresolved evidence.'


def _main_device_issue(dr:Any)->str:
    """One actionable intern-overview sentence; technical details stay in device Word."""
    for f in FAMILIES:
        for project in (get(dr,'projects',[]) or []):
            if get(project,'family')!=f or status(project,'overall_status')!='FAIL': continue
            bad=[get(check,'role','marker') for check in (get(project,'calibration_checks',[]) or [])
                 if 'FAIL' in {status(check),status(check,'geometry_status'),status(check,'value_status')}]
            if bad:return f+': Level 1 calibration failed for '+', '.join(bad)+'. Open device report for the correct tick/value.'
            return f+': Level 1 WPD project failed; inspect its individual marker and curve findings.'
    for req in (get(dr,'requirements',[]) or []):
        if status(req)=='FAIL':return 'Required file/export check failed: '+short(plain_requirement_comment(req),130)
    for f in FAMILIES:
        for q in (get(dr,'level2',[]) or []):
            if get(q,'family')==f and status(q,'overall_status')=='FAIL':
                return f+': Level 2 CSV verification failed; see the affected dataset and source overlay.'
    missing=[f for f in FAMILIES if (get(dr,'availability',{}) or {}).get(f+'_IMAGE')=='MISSING']
    if missing:return 'Missing standalone source graph image for '+', '.join(missing)+'; independent Level 2 is unavailable for those CSVs.'
    unplotted=[q for q in (get(dr,'level2',[]) or []) if status(q,'axis_status')!='PASS']
    if unplotted:
        total=len(get(dr,'level2',[]) or [])
        fs=', '.join(dict.fromkeys(get(q,'family','?') for q in unplotted))
        return f'Level 2: {len(unplotted)} of {total} CSV source-image mappings are unresolved ({fs}); check source ticks.'
    for f in FAMILIES:
        if any(get(p,'family')==f and status(p,'calibration_status')!='PASS' for p in (get(dr,'projects',[]) or [])):
            return f+': Level 1 calibration or marker alignment needs source-tick confirmation.'
    if status(dr,'overall_status')=='PASS':return 'No issues found in completed required checks.'
    return 'Level 1/2 curve identity and complete point coverage still need visual confirmation.'


def write_reviewer_intern_report(intern:str, records, path:str|Path, output_root:str|Path)->str:
    """Compact landscape report; one device per row, with working Word links."""
    from docx.enum.section import WD_ORIENT
    from wpd_reports import _link
    rows=sorted(list(records),key=lambda q:(str(get(q,'device','')).casefold(),str(get(q,'device_relpath',''))))
    target=Path(path);target.parent.mkdir(parents=True,exist_ok=True)
    counts=Counter(status(q,'overall_status') for q in rows)
    d=new_doc(intern,'Intern WPD verification overview')
    sec=d.sections[0]
    sec.orientation=WD_ORIENT.LANDSCAPE
    sec.page_width,sec.page_height=Inches(11),Inches(8.5)
    sec.left_margin=sec.right_margin=Inches(.64)
    sec.top_margin,sec.bottom_margin=Inches(.58),Inches(.48)
    sec.header_distance=Inches(.22)
    p=d.add_paragraph(); r=p.add_run('INTERN VERIFICATION OVERVIEW')
    r.font.size=Pt(8);r.bold=True;r.font.color.rgb=RGBColor.from_string('276080')
    p=d.add_paragraph();p.paragraph_format.space_after=Pt(5)
    a=p.add_run(intern.replace('_',' '));a.bold=True;a.font.size=Pt(18);a.font.color.rgb=RGBColor.from_string(NAVY)
    paras(d,f'Devices: {len(rows)}    PASS: {counts.get("PASS",0)}    FAIL: {counts.get("FAIL",0)}    REVIEW: {counts.get("REVIEW",0)}',bold=True)
    paras(d,'Open the individual Word report for calibration markers, Level 1 extracted curves and independent Level 2 CSV overlays. REVIEW means further evidence or inspection is needed, not necessarily wrong digitization.',small=True)
    items=[]
    for record in rows:
        main=_main_device_issue(record)
        items.append([get(record,'device',''),status(record,'cc_status'),status(record,'oc_status'),
                      status(record,'tc_status'),status(record,'overall_status'),main,'Open report'])
    t=table(d,['Device','CC','OC','TC','Overall','Main issue / action','Word report'],items,
            [1.64,.62,.62,.62,.72,4.47,1.01],font=7.7)
    for idx,rec in enumerate(rows,1):
        cell=t.cell(idx,6)
        report=get(rec,'report_docx')
        if not report or not Path(str(report)).is_file():continue
        paragraph=cell.paragraphs[0]
        for child in list(paragraph._p):
            if child.tag == qn('w:r'): paragraph._p.remove(child)
        _link(paragraph,str(report),'Open report',target)
    d.save(target)
    return str(target)
