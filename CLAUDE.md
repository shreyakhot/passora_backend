# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

Multi-country passport and visa photo processing API built with FastAPI. Handles face detection, hair-safe cropping, background removal (MODNet + rembg fallback), JPEG compression with binary search, print sheet generation, and Cloudinary storage. Supports 45+ countries via JSON configs.

## Running the Server

```bash
python main.py
# Or with hot-reload:
uvicorn main:app --host 0.0.0.0 --port 8000 --reload
```

## Key Dependencies

- MediaPipe (`mediapipe==0.10.35`) for face landmarking and hair segmentation
- ONNX Runtime for MODNet background removal
- `rembg` as fallback background removal
- Cloudinary SDK for file storage
- OpenCV, Pillow, NumPy for image processing

## Architecture

```
api/routes.py         → FastAPI endpoints (auth, validation, processing)
services/            → Core pipeline: face_detector, hair_detector, background,
│                         cropper, validator, compressor, preview
config/countries/    → 45+ country JSON configs with unified Pydantic schema
storage/             → Cloudinary (primary) and local fallback
```

## API Endpoints

| Endpoint | Method | Auth | Description |
|----------|--------|------|-------------|
| `/health` | GET | None | Health check |
| `/countries` | GET | None | List supported countries |
| `/countries/{code}` | GET | None | Get country config |
| `/validate` | POST | X-API-Key | Validate photo compliance (no processing) |
| `/process` | POST | X-API-Key | Full processing pipeline |

## Processing Pipeline (`/process`)

1. Face detection via MediaPipe FaceLandmarker
2. Hair crown detection via MediaPipe ImageSegmenter
3. Background validation
4. Background correction if needed (MODNet → rembg fallback)
5. Compute hair-safe, eye-aligned crop
6. Resize + JPEG compression (binary search to meet size constraints)
7. Generate print sheet with cut guides
8. Generate preview with measurement overlays
9. Upload all to Cloudinary

## Important Patterns

- **Singleton model loading**: FaceLandmarker, ImageSegmenter, MODNet, and rembg are loaded once at app startup via lifespan context manager. The ONNX model (`modnet.onnx`) and MediaPipe task files (`models/`) are loaded lazily on first use.
- **API key auth**: Write endpoints use `X-API-Key` header validated against `settings.api_keys`.
- **Country config schema**: All country JSON configs are validated against `config/countries/schema.py` — adding a new country means adding a JSON file, not code.
- **Storage abstraction**: `storage/cloud.py` and `storage/local.py` share a common interface; Cloudinary is primary.

## File Size Constraints

JPEG compression uses binary search to find the optimal quality setting that meets file size limits. Default max is 500KB, adjustable per request.

## Environment Variables

Required in `.env`:
- `CLOUDINARY_*` credentials (cloud_name, api_key, api_secret)
- `API_KEYS` (comma-separated list of valid API keys)

## Tests and CI

There is no unit test suite. `scripts/ci_check.py` is the CI check: it validates every country config, imports the app, and crops `test_face.jpg` in crop-only mode to the US passport size. It runs on GitHub (`.github/workflows/ci.yml`, job `checks`) for every push and pull request. Use `scratch/audit_cropper.py` or manual endpoint testing for deeper checks.

## Deployment

The API runs on AWS EC2 as the systemd service `passora-backend`, from a git checkout at `/home/ubuntu/passora_backend`. Deploys are pull-based: `passora-autodeploy.timer` runs `deploy/autodeploy.sh` every 2 minutes. When a new commit on `main` has passed CI, the script deploys it, and rolls back if `/health` fails. Pushing to `main` is the whole deploy. See `deploy/README.md`.

- Keep the CI job name `checks` in sync with `CI_CHECK_NAME` in `deploy/autodeploy.sh`.
- Systemd units live in `deploy/` as templates (`__USER__`, `__APP_DIR__`). Deploys reinstall them when they change.
- A new country/document config only needs its JSON file. The frontend entry must go live after the backend deploy has finished.