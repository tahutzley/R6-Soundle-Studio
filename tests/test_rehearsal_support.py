from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from rehearsal_support import approve_correction, build_studio_fixture


STUDIO_ROOT = Path(__file__).resolve().parents[1]
GAME_ROOT = STUDIO_ROOT.parent / "R6-Soundle"


class StudioRehearsalSupportTests(unittest.TestCase):
    def test_synthetic_processor_import_preview_and_correction(self) -> None:
        if not (GAME_ROOT / "index.html").is_file():
            self.skipTest("Sibling game checkout is unavailable")
        with tempfile.TemporaryDirectory(prefix="r6-soundle-rehearsal-test-") as temporary:
            fixture = build_studio_fixture(
                Path(temporary), GAME_ROOT, "2030-03-11"
            )
            self.assertEqual(3, len(fixture.store.list_captures()))
            self.assertEqual("approved", fixture.set_item["status"])
            self.assertEqual([], fixture.preview_contract["issues"])
            self.assertTrue(fixture.processor_failure["rawInputsPreserved"])
            self.assertTrue(fixture.processor_failure["canonicalOutputAbsent"])
            corrected = approve_correction(fixture)
            self.assertEqual("approved", corrected["status"])
            self.assertGreater(corrected["version"], fixture.set_item["version"])


if __name__ == "__main__":
    unittest.main()

