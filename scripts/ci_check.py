"""
CI checks for the photo processing API.

Run from anywhere:  python scripts/ci_check.py

1. Every country config in config/countries/ is valid and has a unique
   "{country_code}_{document_type}" key. (The loader only logs a bad file and
   skips it, so a broken config would otherwise go live silently.)
2. The FastAPI app imports and registers its main routes.
3. Smoke test: the sample photo test_face.jpg is face-detected and cropped in
   crop-only mode (no background change, no padding) to the US passport size,
   then compressed under the size limit.

No .env, Cloudinary account or network access is needed.
"""

import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.chdir(ROOT)


def check_configs() -> None:
    from config.countries.schema import CountryConfig

    files = sorted((ROOT / "config" / "countries").glob("*.json"))
    if not files:
        raise SystemExit("No country configs found")

    keys: dict[str, str] = {}
    for path in files:
        try:
            config = CountryConfig(**json.loads(path.read_text(encoding="utf-8")))
        except Exception as exc:  # noqa: BLE001 - report every kind of failure
            raise SystemExit(f"Invalid config {path.name}: {exc}") from exc
        key = f"{config.country.code}_{config.document_type}"
        if key in keys:
            raise SystemExit(f"Duplicate config key {key}: {keys[key]} and {path.name}")
        keys[key] = path.name

    print(f"OK  {len(files)} country configs are valid")


def check_app() -> None:
    from main import app

    paths = {getattr(route, "path", "") for route in app.routes}
    missing = {"/health", "/countries", "/process", "/validate"} - paths
    if missing:
        raise SystemExit(f"Routes missing from the app: {sorted(missing)}")
    print(f"OK  app imports with {len(paths)} routes")


def check_crop_only() -> None:
    import cv2

    from config.countries.loader import get_config, load_all_configs
    from services.compressor import compress_to_jpeg
    from services.cropper import compute_crop
    from services.face_detector import detect_face
    from services.hair_detector import detect_hair_crown

    load_all_configs()
    config = get_config("US", "passport")
    if config is None:
        raise SystemExit("US passport config not loaded")

    image = cv2.imread(str(ROOT / "test_face.jpg"))
    if image is None:
        raise SystemExit("Could not read test_face.jpg")

    face = detect_face(image)
    try:
        crown_y = detect_hair_crown(image)
    except ValueError:
        crown_y = face.top_of_head_y

    result = compute_crop(image, face, crown_y, config, allow_padding=False)
    height, width = result.image.shape[:2]
    expected = (config.target_width_px, config.target_height_px)
    if (width, height) != expected:
        raise SystemExit(f"Crop is {width}x{height}, expected {expected[0]}x{expected[1]}")

    max_kb = config.digital_requirements.max_file_size_kb
    jpeg = compress_to_jpeg(result.image, max_size_kb=max_kb, target_dpi=config.photo_spec.print_dpi)
    if len(jpeg) > max_kb * 1024:
        raise SystemExit(f"JPEG is {len(jpeg) // 1024} KB, limit {max_kb} KB")

    print(
        f"OK  crop-only US passport photo: {width}x{height}, {len(jpeg) // 1024} KB, "
        f"head {result.head_height_pct:.1f}%"
    )


def _report_failure(message: str) -> None:
    """Print the failure; on GitHub Actions also add it as an annotation on the commit."""
    print(message, file=sys.stderr)
    if os.environ.get("GITHUB_ACTIONS") == "true":
        escaped = message.replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")
        print(f"::error title=CI check failed::{escaped}")


if __name__ == "__main__":
    import traceback

    try:
        check_configs()
        check_app()
        check_crop_only()
    except SystemExit as exc:
        _report_failure(str(exc.code))
        sys.exit(1)
    except Exception:  # noqa: BLE001 - any error fails CI with its traceback
        _report_failure(traceback.format_exc())
        sys.exit(1)
    print("All checks passed")
