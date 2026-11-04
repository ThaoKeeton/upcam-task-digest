"""Distribution branding uses one authentic, transparent source at native sizes."""
import hashlib
from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]


class DesktopBrandingTests(unittest.TestCase):
    def test_official_source_and_multisize_icon_preserve_transparency(self):
        try:
            from PIL import Image
        except ImportError:
            self.skipTest("Image decoding is covered by the packaged Windows build")
        assets = ROOT / "winui/UPCAMTaskDigest/Assets"
        source = assets / "Teambition.png"
        self.assertTrue(source.is_file(), "Official source asset is missing")
        self.assertEqual(hashlib.sha256(source.read_bytes()).hexdigest(),
                         "941a5a0aa5c3609ead813267836a717d369c1e48321fd69eae1d985df4674908")
        with Image.open(source) as image:
            original = image.convert("RGBA")
            self.assertEqual(original.size, (128, 128))
            self.assertEqual(original.getpixel((0, 0))[3], 0)
        with Image.open(assets / "Teambition.ico") as icon:
            self.assertEqual(icon.format, "ICO")
            self.assertEqual(icon.ico.sizes(), {(n, n) for n in (16, 20, 24, 32, 40, 48, 64, 96, 128)})
            self.assertEqual(icon.ico.getimage((128, 128)).convert("RGBA").tobytes(), original.tobytes())
            for size in icon.ico.sizes():
                self.assertEqual(icon.ico.getimage(size).convert("RGBA").getpixel((0, 0))[3], 0)
