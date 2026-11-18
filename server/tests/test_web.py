"""Static checks of the shipped web files."""

from pathlib import Path

WEB = Path(__file__).resolve().parents[2] / "web"


def test_no_dangling_source_maps():
    # Vendored bundles must not reference .map files we do not ship (404s in every devtools).
    for f in WEB.rglob("*"):
        if f.suffix in {".js", ".mjs", ".css"}:
            assert "sourceMappingURL=" not in f.read_text(encoding="utf-8", errors="replace"), f
