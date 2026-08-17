import argparse
import unittest

import numpy as np

from beanoflight.app import background_key_action
from beanoflight.mock_inferencer_app import crop_preview_image
from beanoflight.system_test import _background_indices


class GuiHelperTests(unittest.TestCase):
    def test_background_shortcuts_are_case_insensitive(self):
        for key in ("u", "U", "y", "Y"):
            self.assertEqual(background_key_action(key), "use")
        for key in ("n", "N"):
            self.assertEqual(background_key_action(key), "skip")
        self.assertIsNone(background_key_action("space"))

    def test_three_background_indices_are_required(self):
        self.assertEqual(_background_indices("43,222,347"), (43, 222, 347))
        for invalid in ("43,222", "43,222,347,400", "43,43,347"):
            with self.assertRaises(argparse.ArgumentTypeError):
                _background_indices(invalid)

    def test_crop_preview_supports_installed_pillow(self):
        crop = np.zeros((300, 300, 3), dtype=np.uint8)
        crop[0, 0] = (3, 2, 1)
        preview = crop_preview_image(crop)
        self.assertEqual(preview.size, (300, 300))
        self.assertEqual(preview.getpixel((0, 0)), (1, 2, 3))


if __name__ == "__main__":
    unittest.main()
