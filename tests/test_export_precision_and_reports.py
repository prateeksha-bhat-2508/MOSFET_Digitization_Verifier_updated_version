"""Regression tests: CSV-written precision, independent preview policy and pilot report shape."""
import tempfile
import unittest
from pathlib import Path
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import numpy as np
from wpd_inventory import compare_export_values
from wpd_reports_reviewer import write_reviewer_device_reports


class PrecisionPolicy(unittest.TestCase):
    def test_written_three_decimal_rounding_passes_export_only(self):
        saved = [[1.32279314888, 247.706269288], [1.34650856, 405.700342082]]
        csv = {"rows": 2, "x": np.array([1.323,1.347]),
               "y":np.array([247.706,405.700]),
               "invalid_lines":[],"extra_numeric_columns":[],
               "rounding_quantum":[[.001,.001],[.001,.001]],
               "raw_xy_tokens":[['1.323','247.706'],['1.347','405.700']]}
        check=compare_export_values(saved,csv)
        self.assertEqual(check['status'],'PASS')
        self.assertEqual(check['issue_code'],'CSV_ROUNDING_PASS')
    def test_real_numeric_mismatch_is_not_excused(self):
        saved=[[1.,10.],[2.,20.]]
        csv={"rows":2,"x":np.array([1.,2.]),"y":np.array([12.,20.]),
             "invalid_lines":[],"extra_numeric_columns":[],
             "rounding_quantum":[[.001,.001],[.001,.001]],
             "raw_xy_tokens":[['1.000','12.000'],['2.000','20.000']]}
        self.assertEqual(compare_export_values(saved,csv)['status'],'FAIL')


class ReportEvidencePolicy(unittest.TestCase):
    def test_old_wpd_preview_is_not_presented_as_independent_level_two(self):
        # Older saved records can still contain visual previews. They must be
        # omitted from the new report even in report-only regeneration.
        import docx
        with tempfile.TemporaryDirectory() as t:
            path=Path(t)
            from PIL import Image
            pic=path/'preview.png'; Image.new('RGB',(180,120),'white').save(pic)
            record={'device':'TESTCASE','intern':'Sample','device_relpath':'Sample/TESTCASE',
                    'datasheets':[],'availability':{},'files':[], 'requirements':[],
                    'cc_status':'REVIEW','oc_status':'REVIEW','tc_status':'REVIEW',
                    'overall_status':'REVIEW','projects':[],
                    'level2':[{'family':'CC','csv_file':'TESTCASE_Coss.csv',
                               'source_image':'CC.jpeg','axis_status':'REVIEW',
                               'trace_status':'REVIEW','overall_status':'REVIEW',
                               'overlay_kind':'WPD_VISUAL_PREVIEW',
                               'overlay_visual':str(pic),'point_count':3,
                               'comments':['Independent Y axis not established.']}],
                    'family_comments':{},'source_notes':[]}
            saved=write_reviewer_device_reports(record,path)
            d=docx.Document(saved['consolidated'])
            self.assertEqual(len(d.inline_shapes),0)
            words=' '.join(p.text for p in d.paragraphs)
            self.assertIn('A WPD-derived preview is not independent evidence', words)
            self.assertIn('TESTCASE_Coss.csv', words)

if __name__=='__main__': unittest.main()
