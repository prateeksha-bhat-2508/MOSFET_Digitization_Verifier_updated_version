"""Local regression tests; run: python -m unittest discover -s tests -v."""
from __future__ import annotations

import io
import tarfile
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from PIL import Image, ImageDraw

import wpd_verifier as api
from wpd_csv_check import verify_csv
from wpd_mapping import _rectangle_candidates
from wpd_visual_preview import build_wpd_preview_mapping


class Level2OverlayChecks(unittest.TestCase):
    def test_plot_area_without_full_border(self):
        image = Image.new("L", (340, 260), 255)
        draw = ImageDraw.Draw(image)
        draw.line((55, 20, 55, 220), fill=20, width=2)
        draw.line((55, 220, 320, 220), fill=20, width=2)
        draw.line((55, 70, 320, 70), fill=90)
        draw.line((55, 120, 320, 120), fill=90)
        rects, _, _ = _rectangle_candidates(__import__("numpy").asarray(image))
        self.assertTrue(rects)
        self.assertTrue(any(abs(r[0]-55)<=2 and abs(r[3]-220)<=2 for r in rects))

    def test_preview_does_not_pass_independent_verification(self):
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp)
            source = folder / "CC.png"
            Image.new("RGB", (220, 180), "white").save(source)
            archive = folder / "device_CC.tar"
            raw = source.read_bytes()
            with tarfile.open(archive, "w") as tar:
                info = tarfile.TarInfo("device_CC/CC.png")
                info.size = len(raw)
                tar.addfile(info, io.BytesIO(raw))
            project = api.ProjectResult(project=archive.name, family="CC", x_scale="linear", y_scale="log")
            project.axis_settings = [{"axis_name": "XY"}]
            project.calibration = [
                api.CalibrationPoint("X1", 30, 140, "0", "1"),
                api.CalibrationPoint("X2", 190, 140, "10", "1"),
                api.CalibrationPoint("Y1", 30, 140, "0", "1"),
                api.CalibrationPoint("Y2", 30, 20, "10", "1000"),
            ]
            preview = build_wpd_preview_mapping(source, folder, [project])
            self.assertIsNotNone(preview)
            csv_path = folder / "device_Coss.csv"
            csv_path.write_text("0,1\n3,10\n6,100\n10,1000\n", encoding="utf8")
            overlay = folder / "overlay.png"
            with patch("wpd_csv_check.detect_plot_rect_and_ticks", return_value={"status": "REVIEW", "reason": "No reliable OCR axes"}):
                record = verify_csv(source, csv_path, overlay, api, preview_mapping=preview)
            self.assertTrue(overlay.is_file())
            self.assertEqual(record.overlay_kind, "WPD_VISUAL_PREVIEW")
            self.assertEqual(record.axis_status, "REVIEW")
            self.assertEqual(record.overall_status, "REVIEW")
            self.assertTrue(any("NOT an independent" in c for c in record.comments))
            # A shifted/different source is NOT safe to overlay using TAR pixels.
            Image.new("RGB", (220, 180), "black").save(source)
            self.assertIsNone(build_wpd_preview_mapping(source, folder, [project]))

    def test_missing_image_keeps_csv_numeric_checks(self):
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp)
            good = folder / "a_Coss.csv"
            bad = folder / "b_Crss.csv"
            good.write_text("0,4\n1,3\n", encoding="utf-8")
            bad.write_text("0,4\n1,3\ninvalid,2\n", encoding="utf-8")
            a = verify_csv(None, good, folder / "a.png", api)
            b = verify_csv(None, bad, folder / "b.png", api)
            self.assertEqual(a.point_count, 2)
            self.assertEqual(a.overall_status, "REVIEW")
            self.assertEqual(b.overall_status, "FAIL")


if __name__ == "__main__":
    unittest.main()
