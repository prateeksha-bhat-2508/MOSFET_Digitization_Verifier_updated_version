"""Independent graph calibration for WPD verifier.

Only the source image supplies scale and range evidence. CSV values and WPD
settings are never used to choose a mapping. Unreadable or ambiguous labels
produce REVIEW. This module uses Pillow, NumPy and the Tesseract executable;
OpenCV and the pytesseract Python wrapper are not required.
"""
from __future__ import annotations

import copy
import csv
import hashlib
import io
import itertools
import math
from pathlib import Path
import re
import shutil
import subprocess
import os
import tempfile

import numpy as np
from PIL import Image, ImageOps

_CACHE = {}
_CACHE_LIMIT = 512


def _tesseract_command():
    executable = shutil.which('tesseract')
    if executable:
        return executable
    for executable in (r'C:\Program Files\Tesseract-OCR\tesseract.exe',
                       r'C:\Program Files (x86)\Tesseract-OCR\tesseract.exe'):
        if Path(executable).is_file():
            return executable
    return None


def configure_tesseract():
    return _tesseract_command() is not None


def parse_numeric_label(text):
    """Accept complete numeric tokens; do not strip text into false numbers."""
    raw = str(text).strip().replace('\u2212', '-').replace('\u2013', '-')
    raw = raw.replace(',', '').replace('O', '0').replace('o', '0')
    superscript=re.fullmatch(r'10([\u2070\u00b9\u00b2\u00b3\u2074\u2075\u2076\u2077\u2078\u2079\u207b\u207a]+)',raw)
    if superscript:
        exponent=superscript.group(1).translate(str.maketrans('\u2070\u00b9\u00b2\u00b3\u2074\u2075\u2076\u2077\u2078\u2079\u207b\u207a','0123456789-+'))
        try:
            return 10.**int(exponent)
        except (ValueError,OverflowError):
            return None
    if re.fullmatch(r'[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?', raw):
        try:
            value = float(raw)
            return value if math.isfinite(value) else None
        except ValueError:
            return None
    m = re.fullmatch(r'10\s*\^\s*([+-]?\d+)', raw)
    if m:
        try:
            value = 10.0 ** int(m.group(1))
            return value if math.isfinite(value) else None
        except (ValueError, OverflowError):
            return None
    return None


def _runs(mask):
    boundaries = np.diff(np.r_[False, np.asarray(mask, dtype=bool), False].astype(np.int8))
    return list(zip(np.flatnonzero(boundaries == 1), np.flatnonzero(boundaries == -1)))


def _line_segments(gray, orientation):
    work = gray if orientation == 'H' else gray.T
    minimum = max(12, int(work.shape[1] * .38))
    found = []
    for coordinate, row in enumerate(work):
        mask = row < 205
        # Bridge a single antialiasing/JPEG gap, not a chain of arbitrary marks.
        if len(mask) > 2:
            mask[1:-1] |= mask[:-2] & mask[2:]
        runs = _runs(mask)
        if not runs:
            continue
        start, end = max(runs, key=lambda q: q[1] - q[0])
        if end - start >= minimum:
            found.append((float(coordinate), float(start), float(end - 1)))
    groups = []
    for item in found:
        if groups and item[0] - groups[-1][-1][0] <= 2:
            groups[-1].append(item)
        else:
            groups.append([item])
    output = []
    for group in groups:
        longest = max(group, key=lambda q:q[2]-q[1])
        output.append((float(np.mean([q[0] for q in group])), longest[1], longest[2]))
    return output


def _rectangle_candidates(gray):
    h, w = gray.shape
    horizontal = _line_segments(gray, 'H')
    vertical = _line_segments(gray, 'V')
    result = []
    tolerance = max(2., min(w,h) * .006)
    for left, right in itertools.combinations(vertical, 2):
        xl, xr = left[0], right[0]
        if xr - xl < .38*w or xl < max(9., .035*w):
            continue
        common_top = max(left[1], right[1])
        common_bottom = min(left[2], right[2])
        if common_bottom - common_top < .35*h:
            continue
        hh = [q for q in horizontal if q[1] <= xl + tolerance and q[2] >= xr - tolerance
              and common_top-tolerance <= q[0] <= common_bottom+tolerance]
        if len(hh) < 2:
            continue
        yt, yb = hh[0][0], hh[-1][0]
        if yb - yt < .35*h or h - yb < max(8., .025*h):
            continue
        # A boundary must agree with the ends of the independent vertical lines.
        if abs(yt-common_top)>tolerance*2 or abs(yb-common_bottom)>tolerance*2:
            continue
        rect = tuple(int(round(v)) for v in (xl,yt,xr,yb))
        area = (xr-xl)*(yb-yt)
        if not any(sum(abs(a-b) for a,b in zip(rect,q[1]))<8 for q in result):
            result.append((area,rect))
    # Datasheet figures frequently omit the top/right plot borders. Recover
    # their plotting area from the intersecting, independently detected left
    # and bottom axes instead of insisting on a four-sided black rectangle.
    # The outside figure frame is excluded by the minimum left margin.
    for v in vertical:
        xl, top, bottom = v
        if not (.09*w <= xl <= .36*w and .025*h <= top <= .23*h
                and .55*h <= bottom <= .88*h and bottom-top >= .38*h):
            continue
        for line in horizontal:
            yb, start, end = line
            if (abs(yb-bottom)>max(5.,.022*h)
                    or abs(start-xl)>max(8.,.03*w)
                    or end-xl<.48*w):
                continue
            rect = tuple(int(round(z)) for z in (xl, top, end, yb))
            area = (end-xl)*(yb-top)
            # An axis-pair candidate is slightly down-weighted compared with
            # a verified four-sided plotting rectangle of equal size.
            if not any(sum(abs(a-b) for a,b in zip(rect,q[1]))<8 for q in result):
                result.append((.85*area,rect))
    result.sort(reverse=True)
    return [q[1] for q in result[:4]], horizontal, vertical


def _ocr_strip(image, box, axis, scale=5):
    executable = _tesseract_command()
    if not executable:
        return [], ['Tesseract executable unavailable.']
    x0,y0,x1,y1 = [int(q) for q in box]
    crop = ImageOps.autocontrast(image.crop((x0,y0,x1,y1)).convert('L'))
    if not crop.width or not crop.height:
        return [], ['Empty axis-label region.']
    resampling = getattr(Image, "Resampling", Image)
    up = crop.resize((crop.width*scale,crop.height*scale), resampling.LANCZOS)
    found = []
    errors = []
    with tempfile.TemporaryDirectory(prefix='wpd_ticks_') as temp:
        path = Path(temp)/'ticks.png'
        up.save(path)
        for psm in (6,11):
            try:
                proc = subprocess.run([executable,str(path),'stdout','--psm',str(psm),'tsv'],
                    stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True,
                    encoding='utf-8',errors='replace',timeout=20,
                    env={**os.environ, 'OMP_THREAD_LIMIT': '1'},
                    creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
                if proc.returncode:
                    errors.append('Tesseract returned exit code '+str(proc.returncode))
                    continue
                for row in csv.DictReader(io.StringIO(proc.stdout), delimiter='\t'):
                    raw=(row.get('text') or '').strip()
                    value=parse_numeric_label(raw)
                    if not raw or len(raw)>30:
                        continue
                    try:
                        confidence=float(row.get('conf',-1))
                        if confidence<0:
                            continue
                        bx=float(row['left'])/scale+x0
                        by=float(row['top'])/scale+y0
                        bw=float(row['width'])/scale
                        bh=float(row['height'])/scale
                    except (ValueError,KeyError):
                        continue
                    center=bx+bw/2 if axis=='X' else by+bh/2
                    found.append({'label_center':center,'value':value,'raw_text':raw,
                                  'bbox':(bx,by,bw,bh),'confidence':confidence,'psm':psm})
            except (OSError,subprocess.SubprocessError) as exc:
                errors.append(type(exc).__name__+': '+str(exc))
    deduped=[]
    for item in sorted(found,key=lambda x:-x['confidence']):
        if not any(abs(item['label_center']-q['label_center'])<=3
                   and ((item['value'] is None and q['value'] is None and item['raw_text']==q['raw_text'])
                        or (item['value'] is not None and q['value'] is not None
                            and math.isclose(item['value'],q['value'],rel_tol=1e-9,abs_tol=1e-12)))
                   for q in deduped):
            deduped.append(item)
    return sorted(deduped,key=lambda x:x['label_center']), errors


def _tick_positions(gray, rect, axis, horizontal, vertical):
    xl,yt,xr,yb=rect
    plot=gray[yt:yb+1,xl:xr+1]
    # Pale dashed grids still form narrow row/column occupancy peaks. A curve
    # contributes relatively few pixels along either complete axis direction.
    if plot.size:
        # Use pale grid pixels here. Including dark curve ink can create a false
        # tick candidate beside a nearly horizontal low-current trace.
        grid_projection=np.sum((plot>=205)&(plot<242),axis=0 if axis=='X' else 1)
        orthogonal_span=plot.shape[0] if axis=='X' else plot.shape[1]
        grid_groups=_runs(grid_projection>=max(6,.28*orthogonal_span))
        grid_offset=xl if axis=='X' else yt
        dashed=[grid_offset+(lo+hi-1)/2 for lo,hi in grid_groups
                if hi-lo<=max(6,.012*len(grid_projection))]
    else:
        dashed=[]
    if axis=='X':
        candidates=[q[0] for q in vertical if xl-2<=q[0]<=xr+2
                    and q[1]<=yt+4 and q[2]>=yb-4]
        # Short ticks near the lower axis can be used when there is no grid.
        strip=gray[max(0,yb-6):min(gray.shape[0],yb+6),xl:xr+1]
        projection=np.sum(strip<155,axis=0) if strip.size else np.array([])
        offset=xl
    else:
        candidates=[q[0] for q in horizontal if yt-2<=q[0]<=yb+2
                    and q[1]<=xl+4 and q[2]>=xr-4]
        strip=gray[yt:yb+1,max(0,xl-6):min(gray.shape[1],xl+6)]
        projection=np.sum(strip<155,axis=1) if strip.size else np.array([])
        offset=yt
    if len(projection):
        baseline=float(np.median(projection))
        peaks=projection>=max(baseline+3,6)
        for lo,hi in _runs(peaks):
            if hi-lo<=max(5,.025*len(projection)):
                candidates.append(offset+(lo+hi-1)/2)
    candidates.extend(dashed)
    # Full-width grid lines are better anchor candidates than local pixel
    # projection peaks, which often come from curve ink beside a tick.
    if axis=='X':
        trusted=[q[0] for q in vertical if xl-3<=q[0]<=xr+3
                 and q[1]<=yt+4 and q[2]>=yb-4]
    else:
        trusted=[q[0] for q in horizontal if yt-3<=q[0]<=yb+3
                 and q[1]<=xl+4 and q[2]>=xr-4]
    candidates=[p for p in candidates if not any(abs(p-t)<=5 for t in trusted)]
    candidates.extend(trusted)
    distinct=[]
    for p in sorted(candidates):
        if not distinct or p-distinct[-1]>2:
            distinct.append(float(p))
    return distinct


def _anchor_labels(raw, ticks, span, axis='X'):
    anchored=[]
    rejected=[]
    max_gap=max(3.,.012*span)
    for label in raw:
        item=dict(label)
        if label.get('value') is None:
            item['reason']='OCR did not recover a complete numeric tick label. No numeric value or missing exponent was inferred.'
            rejected.append(item)
            continue
        if label.get('confidence',100)<25:
            item['reason']='OCR confidence is too low for a calibration value.'
            rejected.append(item)
            continue
        if not ticks:
            item['reason']='No independently detected tick or grid line.'
            rejected.append(item)
            continue
        tick=min(ticks,key=lambda t:abs(t-label['label_center']))
        gap=abs(tick-label['label_center'])
        # A merged OCR token spanning multiple ticks is not a reliable label.
        label_extent=label['bbox'][2 if axis=='X' else 3]
        association_tolerance=max(max_gap,min(5.,.55*label_extent))
        if gap>association_tolerance:
            item['reason']='OCR label cannot be associated with a detected tick or grid line.'
            rejected.append(item)
            continue
        item.update(pixel=float(tick),label_to_tick_error_px=float(gap),
                    reason='OCR value associated with an independently detected tick or grid line.')
        anchored.append(item)
    # Conflicting values at the same tick remain unavailable until reviewed.
    usable=[]
    for label in anchored:
        conflicts=[q for q in anchored if abs(q['pixel']-label['pixel'])<2
                   and not math.isclose(q['value'],label['value'],rel_tol=1e-8,abs_tol=1e-10)]
        if conflicts:
            label['reason']='Conflicting OCR values were read at this tick.'
            rejected.append(label)
        elif not any(abs(q['pixel']-label['pixel'])<2 for q in usable):
            usable.append(label)
    return usable,rejected


def _fit_candidates(labels, span):
    if len(labels)<3:
        return [], 'At least three distinct, unambiguous labelled ticks are needed to establish scale independently.'
    points=np.array([(q['pixel'],q['value']) for q in labels],dtype=float)
    pixel,value=points[:,0],points[:,1]
    tolerance=max(1.25,.004*span)
    fits=[]
    for kind in ('linear','log'):
        valid=np.isfinite(value)&np.isfinite(pixel)
        if kind=='log':
            valid &= value>0
        pp=pixel[valid]
        vv=np.log10(value[valid]) if kind=='log' else value[valid]
        indices=np.flatnonzero(valid)
        if len(pp)<3 or len(np.unique(vv))<3:
            continue
        best=None
        for i,j in itertools.combinations(range(len(pp)),2):
            if abs(pp[j]-pp[i])<max(4,.05*span) or abs(vv[j]-vv[i])<1e-15:
                continue
            a=(vv[j]-vv[i])/(pp[j]-pp[i]); b=vv[i]-a*pp[i]
            residual=np.abs((vv-b)/a-pp)
            keep=residual<=tolerance
            if int(keep.sum())<3:
                continue
            spread=float(np.ptp(pp[keep]))
            if spread<.46*span:
                continue
            a,b=np.polyfit(pp[keep],vv[keep],1)
            residual=np.abs((vv-b)/a-pp)
            keep=residual<=tolerance
            n=int(keep.sum())
            if n<3 or n < math.ceil(.7*len(points)):
                continue
            score=(n,spread,-float(np.max(residual[keep])))
            if best is None or score>best[0]:
                best=(score,a,b,keep,residual,indices)
        if best:
            _,a,b,keep,residual,indices=best
            chosen=indices[keep]
            values=[(float(pixel[i]),float(value[i])) for i in chosen]
            fits.append({'kind':kind,'coef':[float(a),float(b)],'a':float(a),'b':float(b),
                         'n':len(values),'points':values,'rejected':len(labels)-len(values),
                         'observed_pixel_range':[min(p for p,v in values),max(p for p,v in values)],
                         'observed_value_range':[min(v for p,v in values),max(v for p,v in values)],
                         'pixel_error':float(np.max(residual[keep])),
                         'fit_error':float(np.max(residual[keep]))/max(span,1),
                         'span_px':span,'tolerance_px':tolerance,
                         'status':'PASS','scale_status':'PASS',
                         'tick_evidence':[copy.deepcopy(labels[i]) for i in chosen]})
    if not fits:
        return [], 'No linear or logarithmic mapping fits at least three observed tick values over enough of the axis.'
    fits.sort(key=lambda q:(q['n'],-q['pixel_error']),reverse=True)
    if len(fits)>1 and fits[0]['n']==fits[1]['n']:
        return [], 'Both linear and logarithmic mappings remain plausible within the image resolution.'
    return fits, ''


def fit_axis_mapping(labels):
    """Numerical helper for independently anchored labels, never a two-point guess."""
    prepared=[{'pixel':float(p),'value':float(v),'raw_text':str(rest[0]) if rest else str(v),
               'bbox':rest[1] if len(rest)>1 else (0,0,0,0),'label_center':float(p)}
              for p,v,*rest in labels]
    span=max((q['pixel'] for q in prepared),default=0)-min((q['pixel'] for q in prepared),default=0)
    candidates,_=_fit_candidates(prepared,span)
    return candidates[0] if candidates else None


def data_to_pixel(value, fit):
    try:
        v=float(value)
        if not math.isfinite(v) or (fit.get('kind')=='log' and v<=0):
            return None
        a,b=fit['coef']
        if not math.isfinite(a) or abs(a)<1e-20:
            return None
        target=math.log10(v) if fit.get('kind')=='log' else v
        result=(target-b)/a
        return result if math.isfinite(result) else None
    except (TypeError,ValueError,KeyError,OverflowError,ZeroDivisionError):
        return None


def pixel_to_data(pixel, fit):
    try:
        a,b=fit['coef']; result=a*float(pixel)+b
        result=10.**result if fit.get('kind')=='log' else result
        return float(result) if math.isfinite(result) else None
    except (TypeError,ValueError,KeyError,OverflowError):
        return None


def _axis_evidence(raw_labels, fit):
    accepted=list((fit or {}).get('points',[]))
    evidence=[]
    for p,v,text,bbox,*_ in raw_labels or []:
        match=next(((ap,av) for ap,av in accepted if abs(ap-p)<=3 and math.isclose(av,v,rel_tol=1e-8,abs_tol=1e-10)),None)
        evidence.append({'pos':float(match[0] if match else p),'raw_value':float(v),
                         'raw_text':str(text),'bbox':bbox,'interpreted':float(v) if match else None,
                         'used':bool(match),'reason':'Observed label and detected tick used by the image-only mapping.' if match else 'Not used because the independent mapping or tick association was not established.'})
    return evidence


def _complete_axis_evidence(labels, fit):
    accepted=list((fit or {}).get('points',[]))
    evidence=[]
    for item in labels:
        value=item.get('value')
        pos=float(item.get('pixel',item['label_center']))
        used=value is not None and 'pixel' in item and any(abs(p-pos)<2 and math.isclose(v,value,rel_tol=1e-8,abs_tol=1e-10) for p,v in accepted)
        if used:
            reason='Observed OCR value and independently detected tick position used by the mapping.'
        elif value is not None and 'pixel' in item:
            reason='Candidate is inconsistent with the accepted axis mapping.' if fit else 'No reliable scale mapping was established from these tick candidates.'
        else:
            reason=item.get('reason','Rejected from the independent mapping.')
        evidence.append({'pos':pos,'label_center':float(item['label_center']),
                         'raw_value':value,'raw_text':item['raw_text'],'bbox':item['bbox'],
                         'interpreted':value if used else None,'used':bool(used),
                         'reason':reason})
    return sorted(evidence,key=lambda q:q['pos'])


def _level2_origin_and_anchor(cal):
    return cal.get('origin_px')


def _anchor_linear_zero_axes(cal):
    """Compatibility function: fitted coefficients are deliberately never altered."""
    return cal


def _calibrate_image(image_path):
    try:
        image=Image.open(image_path).convert('L')
        gray=np.asarray(image)
    except Exception as exc:
        return {'status':'REVIEW','reason':'Cannot read independent source image: '+str(exc)}
    rectangles,horizontal,vertical=_rectangle_candidates(gray)
    if not rectangles:
        return {'status':'REVIEW','rect_status':'REVIEW',
                'reason':'A complete plot rectangle with space for external tick labels could not be established from the image.'}
    if not configure_tesseract():
        return {'status':'REVIEW','rect_status':'REVIEW','rect':rectangles[0],
                'reason':'Tesseract unavailable; independent numerical calibration cannot be established.'}
    h,w=gray.shape
    best=None
    for rect in rectangles[:2]:
        xl,yt,xr,yb=rect
        x_box=(max(0,xl-12),max(0,yb+2),min(w,xr+14),min(h,yb+max(22,int(.13*h))))
        y_box=(0,max(0,yt-10),max(1,xl-2),min(h,yb+10))
        cal={'rect':rect,'rect_status':'REVIEW','status':'REVIEW','x_labels':[],'y_labels':[],
             'source':'standalone_image_only','csv_values_used':False,'wpd_settings_used':False,
             'issues':[]}
        success=0
        for axis,box,span in (('X',x_box,xr-xl),('Y',y_box,yb-yt)):
            raw,errors=_ocr_strip(image,box,axis)
            ticks=_tick_positions(gray,rect,axis,horizontal,vertical)
            usable,rejected=_anchor_labels(raw,ticks,span,axis)
            # Raw evidence retains its read value and box; position is the actual tick
            # only when association was established. No missing exponent is invented.
            all_labels=usable+rejected
            cal[axis.lower()+'_labels']=[(float(q.get('pixel',q['label_center'])),float(q['value']),q['raw_text'],q['bbox']) for q in all_labels if q.get('value') is not None]
            candidates,reason=_fit_candidates(usable,span)
            cal[axis.lower()+'_ticks']=ticks
            cal[axis.lower()+'_ocr_evidence']=all_labels
            if candidates:
                cal[axis.lower()]=candidates[0]
                success+=1
            else:
                cal['issues'].append(axis+' axis: '+reason)
            if errors:
                cal['issues'].extend(axis+' axis OCR: '+e for e in errors)
            cal[axis.lower()+'_evidence']=_complete_axis_evidence(all_labels,cal.get(axis.lower()))
        if success==2:
            cal['status']='PASS';cal['rect_status']='PASS'
            ax,ay=float(xl),float(yb)
            cal['origin_px']=(ax,ay)
            cal['origin_kind']='reference_anchor'
            cal['anchor_values']=(pixel_to_data(ax,cal['x']),pixel_to_data(ay,cal['y']))
            # Show a true zero only if both axes actually establish it within the plot.
            if cal['x']['kind']=='linear' and cal['y']['kind']=='linear':
                zx=data_to_pixel(0,cal['x']);zy=data_to_pixel(0,cal['y'])
                if zx is not None and zy is not None and xl-2<=zx<=xr+2 and yt-2<=zy<=yb+2:
                    if any(abs(v)<1e-12 for _,v in cal['x']['points']) and any(abs(v)<1e-12 for _,v in cal['y']['points']):
                        cal['origin_px']=(zx,zy);cal['origin_kind']='observed_zero_intersection';cal['anchor_values']=(0.,0.)
            cal['reason']='Independent calibration uses observed tick values and detected tick/grid positions. No CSV range or WPD calibration is used.'
            return cal
        cal['reason']=' '.join(cal['issues'])
        if best is None or success>best[0]:
            best=(success,cal)
    return best[1]


def detect_plot_rect_and_ticks(image_path, csv_y=None):
    """Image-only calibration; csv_y is accepted solely for legacy call compatibility."""
    path=Path(image_path)
    try:
        digest=hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError as exc:
        return {'status':'REVIEW','reason':'Independent source image cannot be read: '+str(exc)}
    key=(digest,_tesseract_command())
    if key not in _CACHE:
        if len(_CACHE)>=_CACHE_LIMIT:
            _CACHE.pop(next(iter(_CACHE)))
        _CACHE[key]=_calibrate_image(path)
    return copy.deepcopy(_CACHE[key])
