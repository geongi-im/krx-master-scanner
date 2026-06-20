import unittest

import db_scheme


class DbSchemeTest(unittest.TestCase):
    def test_table_names_use_sanitized_prefix(self):
        self.assertEqual(db_scheme.ohlcv_table_names("kms_"), ("kms_ohlcv", "kms_ohlcv_cache_meta"))
        self.assertEqual(db_scheme.scan_table_names("kms"), ("kms_scan_runs", "kms_scan_results"))
        self.assertEqual(db_scheme.vcp_table_names("kms"), ("kms_vcp_runs", "kms_vcp_candidates"))

    def test_invalid_table_prefix_is_rejected(self):
        with self.assertRaises(ValueError):
            db_scheme.ohlcv_table_names("1_bad")

    def test_all_table_sql_contains_runtime_tables(self):
        sql = "\n".join(db_scheme.all_table_sql("kms"))

        self.assertIn("CREATE TABLE IF NOT EXISTS kms_ohlcv", sql)
        self.assertIn("CREATE TABLE IF NOT EXISTS kms_ohlcv_cache_meta", sql)
        self.assertIn("CREATE TABLE IF NOT EXISTS kms_scan_runs", sql)
        self.assertIn("CREATE TABLE IF NOT EXISTS kms_scan_results", sql)
        self.assertIn("CREATE TABLE IF NOT EXISTS kms_vcp_runs", sql)
        self.assertIn("CREATE TABLE IF NOT EXISTS kms_vcp_candidates", sql)


if __name__ == "__main__":
    unittest.main()
