import os
import tempfile
import time
import unittest
from pathlib import Path

import main


class ChartCleanupTest(unittest.TestCase):
    def test_cleanup_old_chart_images_deletes_only_expired_pngs(self):
        now_ts = time.time()

        with tempfile.TemporaryDirectory() as temp_dir:
            charts_dir = Path(temp_dir)
            expired_png = charts_dir / "expired.png"
            fresh_png = charts_dir / "fresh.png"
            expired_txt = charts_dir / "expired.txt"

            expired_png.write_text("old", encoding="utf-8")
            fresh_png.write_text("new", encoding="utf-8")
            expired_txt.write_text("not a chart", encoding="utf-8")

            expired_ts = now_ts - (4 * 24 * 60 * 60)
            fresh_ts = now_ts - (2 * 24 * 60 * 60)
            for path, modified_ts in ((expired_png, expired_ts), (expired_txt, expired_ts), (fresh_png, fresh_ts)):
                os.utime(path, (modified_ts, modified_ts))

            deleted = main.cleanup_old_chart_images(charts_dir, now_ts=now_ts)

            self.assertEqual(deleted, 1)
            self.assertFalse(expired_png.exists())
            self.assertTrue(fresh_png.exists())
            self.assertTrue(expired_txt.exists())


if __name__ == "__main__":
    unittest.main()
