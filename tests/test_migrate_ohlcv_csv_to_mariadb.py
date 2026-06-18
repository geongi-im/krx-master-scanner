import tempfile
import unittest
from pathlib import Path

import pandas as pd

from scripts.migrate_ohlcv_csv_to_mariadb import read_cache_csv, rows_from_frame, symbol_from_cache_path


class MigrateOhlcvCsvToMariaDbTest(unittest.TestCase):
    def test_symbol_is_inferred_from_cache_file_name(self):
        self.assertEqual(symbol_from_cache_path(Path("ohlcv_005930.csv")), "005930")
        self.assertEqual(symbol_from_cache_path(Path("ohlcv_KQ11.csv")), "KQ11")

    def test_rows_compute_amount_when_csv_has_no_amount_column(self):
        df = pd.DataFrame(
            {
                "Open": [100],
                "High": [110],
                "Low": [90],
                "Close": [105],
                "Volume": [1000],
            },
            index=pd.to_datetime(["2024-01-02"]),
        )

        rows = rows_from_frame("005930", df)

        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].amount, 105000)
        self.assertEqual(rows[0].amount_source, "computed")

    def test_rows_use_source_amount_when_csv_has_amount_column(self):
        df = pd.DataFrame(
            {
                "Open": [100],
                "High": [110],
                "Low": [90],
                "Close": [105],
                "Volume": [1000],
                "Amount": [123456],
            },
            index=pd.to_datetime(["2024-01-02"]),
        )

        rows = rows_from_frame("005930", df)

        self.assertEqual(rows[0].amount, 123456)
        self.assertEqual(rows[0].amount_source, "source")

    def test_read_cache_csv_supports_current_index_based_format(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "ohlcv_005930.csv"
            frame = pd.DataFrame(
                {
                    "Open": [100],
                    "High": [110],
                    "Low": [90],
                    "Close": [105],
                    "Volume": [1000],
                },
                index=pd.to_datetime(["2024-01-02"]),
            )
            frame.to_csv(path, encoding="utf-8-sig")

            loaded = read_cache_csv(path)

        self.assertEqual(list(loaded.columns), ["Open", "High", "Low", "Close", "Volume"])
        self.assertEqual(loaded.index[0], pd.Timestamp("2024-01-02"))

    def test_read_cache_csv_supports_explicit_date_column_with_change(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "005930.csv"
            path.write_text(
                "\n".join(
                    [
                        "Date,Open,High,Low,Close,Volume,Change",
                        "2024-10-28,55700,58500,55700,58100,27775009,0.0393559928443649",
                        "2024-10-29,58000,59600,57300,59600,28369314,0.0258175559380378",
                    ]
                ),
                encoding="utf-8",
            )

            loaded = read_cache_csv(path)
            rows = rows_from_frame("005930", loaded)

        self.assertEqual(loaded.index[0], pd.Timestamp("2024-10-28"))
        self.assertIn("Change", loaded.columns)
        self.assertEqual(rows[0].amount, 58100 * 27775009)
        self.assertEqual(rows[0].amount_source, "computed")


if __name__ == "__main__":
    unittest.main()
