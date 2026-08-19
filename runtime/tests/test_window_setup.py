"""Per-platform window configuration.

These branches cannot all be exercised on the machine that runs the tests,
which is exactly why the decision lives in a pure function instead of inline
in the widget constructor.
"""

from __future__ import annotations

import unittest

from runtime.helper import window_setup


class WindowSetupTests(unittest.TestCase):
    def test_every_platform_is_frameless_on_top_and_translucent(self):
        for platform in ("linux", "win32", "darwin"):
            setup = window_setup(platform)
            self.assertIn("FramelessWindowHint", setup["flags"], platform)
            self.assertIn("WindowStaysOnTopHint", setup["flags"], platform)
            self.assertIn("WA_TranslucentBackground", setup["attributes"], platform)

    def test_macos_opts_out_of_hiding_when_the_app_deactivates(self):
        # A Qt tool window is an NSPanel on macOS, and an NSPanel hides itself
        # whenever its application is deactivated. Without this the pet would
        # vanish the moment the user clicked another app -- precisely when a
        # desktop companion should still be visible.
        self.assertIn("WA_MacAlwaysShowToolWindow", window_setup("darwin")["attributes"])

    def test_other_platforms_do_not_carry_the_mac_attribute(self):
        for platform in ("linux", "win32"):
            self.assertNotIn("WA_MacAlwaysShowToolWindow", window_setup(platform)["attributes"], platform)

    def test_every_name_is_real(self):
        # A typo in a flag name would be an AttributeError at window creation,
        # on that platform only, which is the worst place to find it.
        try:
            from PySide6.QtCore import Qt
        except ImportError:  # pragma: no cover
            self.skipTest("PySide6 is not installed")
        for platform in ("linux", "win32", "darwin"):
            setup = window_setup(platform)
            for flag in setup["flags"]:
                self.assertTrue(hasattr(Qt.WindowType, flag), f"{platform}: {flag}")
            for attribute in setup["attributes"]:
                self.assertTrue(hasattr(Qt.WidgetAttribute, attribute), f"{platform}: {attribute}")


if __name__ == "__main__":
    unittest.main()
