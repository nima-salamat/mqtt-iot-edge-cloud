"""Tests for best-effort Raspberry Pi system health metrics."""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from pi_device.system_stats import collect_system_stats


class SystemStatsTests(unittest.TestCase):
    def test_reads_soc_temperature_memory_and_data_volume(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            thermal = root / "thermal" / "thermal_zone0"
            thermal.mkdir(parents=True)
            (thermal / "temp").write_text("47250\n", encoding="ascii")
            data = root / "data"
            data.mkdir()
            meminfo = root / "meminfo"
            meminfo.write_text(
                "MemTotal:       1024000 kB\n"
                "MemAvailable:    512000 kB\n",
                encoding="ascii",
            )

            result = collect_system_stats(
                data,
                meminfo_path=meminfo,
                thermal_root=root / "thermal",
                hwmon_root=root / "hwmon",
            )

            self.assertEqual(result["temperature_c"], 47.2)
            self.assertEqual(result["memory"]["used_percent"], 50.0)
            self.assertGreater(result["memory"]["available_bytes"], 0)
            self.assertGreater(result["storage"]["total_bytes"], 0)
            self.assertGreaterEqual(result["storage"]["used_percent"], 0.0)

    def test_missing_thermal_and_memory_data_are_nonfatal(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            result = collect_system_stats(
                root,
                meminfo_path=root / "missing-meminfo",
                thermal_root=root / "missing-thermal",
                hwmon_root=root / "missing-hwmon",
            )
            self.assertIsNone(result["temperature_c"])
            self.assertIsNone(result["memory"])
            self.assertIsNotNone(result["storage"])


if __name__ == "__main__":
    unittest.main()
