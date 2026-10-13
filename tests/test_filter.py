"""Exercise the real PDF-to-PRBUF filter without submitting a print job."""
import io
import os
import re
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from pypdf import PdfWriter

PBMTODP = Path(os.environ.get("LABELPRINTER_PBMTODP", "/usr/local/lib/labelprinter/pbmtodp"))
FILTER = Path(__file__).resolve().parent.parent / "pc43-filter"
TOOLS = all(shutil.which(tool) for tool in ("pdfinfo", "pdftoppm", "pgmtopbm", "pnmtoplainpnm"))
REASON = "the filter tools and the pbmtodp binary are not installed"


@unittest.skipUnless(PBMTODP.exists() and TOOLS, REASON)
class FilterTests(unittest.TestCase):
    def test_truncated_raster_fails_without_hanging(self):
        for raster in (b"P1\n1 1\n", b"P1\n2 1\n0\n"):
            with self.subTest(raster=raster):
                result = subprocess.run(
                    [str(PBMTODP)],
                    input=raster, capture_output=True, timeout=1,
                )
                self.assertNotEqual(result.returncode, 0)
                self.assertIn(b"Image data corrupt", result.stderr)

    def test_full_size_label_fits_prbuf_print_window(self):
        writer = PdfWriter()
        writer.add_blank_page(width=100 * 72 / 25.4, height=150 * 72 / 25.4)
        document = io.BytesIO()
        writer.write(document)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "label.pdf"
            path.write_bytes(document.getvalue())
            stream = subprocess.run(
                [str(FILTER), "1", "test", "No physical print", "1", "", str(path)],
                check=True, capture_output=True, timeout=30,
            ).stdout
        commands, image = stream.split(b"PRBUF ", 1)
        count, image = image.split(b"\n", 1)
        self.assertEqual(image[int(count):], b"\nPF 1\n")
        self.assertEqual(image[:2], b"@\x02")
        width = int.from_bytes(image[2:4], "big")
        height = int.from_bytes(image[4:6], "big")
        media_width = int(re.search(rb'MEDIA SIZE,WIDTH,(\d+)"', commands)[1])
        media_height = int(re.search(rb'MEDIA SIZE,LENGTH,(\d+)"', commands)[1])
        origin = re.search(rb"PP (-?\d+),(-?\d+)\n", commands)
        x, y = int(origin[1]), int(origin[2])
        # Honeywell PRBUF renders at (x, y + 1); an out-of-window bitmap
        # raises error 1003 and blocks Direct Protocol until acknowledged.
        self.assertGreaterEqual(x, 0)
        self.assertGreaterEqual(y, 0)
        self.assertLessEqual(x + width, media_width)
        self.assertLessEqual(y + 1 + height, media_height)


if __name__ == "__main__":
    unittest.main()
