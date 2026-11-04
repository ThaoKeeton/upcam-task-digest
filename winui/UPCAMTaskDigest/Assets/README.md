# Internal application icon

The user's requested Teambition mark comes from the official public favicon at
https://open.teambition.com/favicon.ico, retrieved on 2026-11-04 without authentication.
The response is a 128 x 128 RGBA PNG despite its URL extension. Its unchanged bytes
are stored as `Teambition.png` with SHA-256
`941a5a0aa5c3609ead813267836a717d369c1e48321fd69eae1d985df4674908`.

`Teambition.ico` contains lossless PNG frames at 16, 20, 24, 32, 40, 48, 64, 96 and
128 pixels, downsampled with Pillow. No frame is enlarged beyond the source.
The EXE, AppWindow and tray all use that ICO; the XAML title bar uses the identical
source PNG. Transparency is retained. Rebuild with:

```python
from PIL import Image
with Image.open("Teambition.png") as source:
    source.save("Teambition.ico", format="ICO",
                sizes=[(n, n) for n in (16,20,24,32,40,48,64,96,128)])
```

The mark identifies the user's internal Teambition workflow. UPCAM Task Digest is
an independent internal utility; this asset does not imply vendor authorship or endorsement.
