import importlib.util
import unittest
from pathlib import Path

MODULE_PATH = (
    Path(__file__).parents[1] / "deploy/jetson/install_boot_profiles.py"
)
SPEC = importlib.util.spec_from_file_location("install_boot_profiles", MODULE_PATH)
assert SPEC is not None and SPEC.loader is not None
boot_profiles = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(boot_profiles)


SOURCE = """TIMEOUT 50
DEFAULT JetsonIO

LABEL primary
 MENU LABEL primary kernel
 LINUX /boot/Image
 INITRD /boot/initrd
 APPEND base

LABEL JetsonIO
 MENU LABEL cameras
 LINUX /boot/Image
 FDT /boot/camera.dtb
 INITRD /boot/initrd
 APPEND base root=PARTUUID=123 rw
 OVERLAYS /boot/camera.dtbo

LABEL IMX296_SAFE
 MENU LABEL safe
 LINUX /boot/Image
 FDT /boot/camera.dtb
 INITRD /boot/initrd
 APPEND base root=PARTUUID=123 rw module_blacklist=imx296
 OVERLAYS /boot/camera.dtbo
"""


class JetsonBootProfileTests(unittest.TestCase):
    def test_render_retains_fallback_and_builds_six_profiles(self):
        rendered = boot_profiles.render_profiles(SOURCE)
        boot_profiles.validate(rendered)

        self.assertIn("DEFAULT BEANO_DESKTOP", rendered)
        self.assertIn("LABEL primary", rendered)
        self.assertEqual(rendered.count("LABEL BEANO_"), 6)
        self.assertIn("systemd.unit=multi-user.target", rendered)
        self.assertIn("beanoflight.performance=1", rendered)
        self.assertIn("module_blacklist=imx296", rendered)

    def test_render_is_idempotent(self):
        once = boot_profiles.render_profiles(SOURCE)
        twice = boot_profiles.render_profiles(once)
        self.assertEqual(once, twice)


if __name__ == "__main__":
    unittest.main()
