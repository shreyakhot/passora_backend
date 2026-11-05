"""
API routes for the passport photo processing system.

POST /upload    — Upload an image, get upload_id
POST /process   — Process an uploaded image for a country
GET  /download/{id}           — Download processed photo
GET  /countries               — List all supported countries
GET  /countries/{code}        — Get specific country config
GET  /health                  — Health check
"""

import logging
import time
import uuid

import cv2
import numpy as np
from fastapi import APIRouter, File, Form, HTTPException, UploadFile, Depends, Security, BackgroundTasks
from fastapi.responses import FileResponse
from fastapi.security import APIKeyHeader

from api.models import (
    CountrySummary,
    ErrorResponse,
    HealthResponse,
    ProcessingMetrics,
    ProcessResponse,
    ValidationResponse,
)
from config.countries.loader import get_all_configs, get_config, list_supported
from config.settings import settings
from services.background import correct_background, validate_background
from services.compressor import compress_to_jpeg
from services.cropper import compute_crop
from services.face_detector import detect_face
from services.hair_detector import detect_hair_crown
from services.preview import create_preview_image
from services.validator import analyze_image
from storage.cloud import upload_original, download_image, upload_results

logger = logging.getLogger(__name__)

api_key_header = APIKeyHeader(name="X-API-Key", auto_error=True)

async def verify_api_key(api_key: str = Security(api_key_header)):
    if api_key != settings.api_key:
        raise HTTPException(
            status_code=403,
            detail="Invalid API Key",
        )
    return api_key

router = APIRouter()


# ─── POST /validate ───

@router.post(
    "/validate",
    response_model=ValidationResponse,
    responses={400: {"model": ErrorResponse}, 500: {"model": ErrorResponse}},
    summary="Validate photo compliance without processing",
)
async def validate_photo(
    image: UploadFile = File(...),
    country_code: str = Form("US"),
    document_type: str = Form("passport"),
    api_key: str = Depends(verify_api_key)
):
    """
    Perform deep validation of a photo against country specs.
    Returns a detailed report of lighting, face position, and orientation.
    """
    config = get_config(country_code, document_type)
    if config is None:
        raise HTTPException(
            status_code=400,
            detail=f"No config found for '{country_code}' / '{document_type}'"
        )

    file_bytes = await image.read()
    
    try:
        # Provide a fallback string since UploadFile.content_type can be None
        report = analyze_image(file_bytes, config, image.content_type or "unknown")
        return report
    except ValueError as e:
        logger.error(f"Validation failed: {e}")
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        logger.error(f"Validation error: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Validation failed: {str(e)}")


# ─── POST /process ───

@router.post(
    "/process",
    response_model=ProcessResponse,
    responses={400: {"model": ErrorResponse}, 500: {"model": ErrorResponse}},
    summary="Process an image directly in one call",
)
async def process_image(
    background_tasks: BackgroundTasks,
    image: UploadFile = File(...),
    country_code: str = Form("US"),
    document_type: str = Form("passport"),
    background_mode: str = Form("auto"),
    api_key: str = Depends(verify_api_key)
):
    """
    Consolidated processing pipeline:
    1. Validate and upload original image to Cloudinary
    2. Face detection
    3. Hair/crown detection
    4. Background validation/correction
    5. Compute crop
    6. Resize + compress
    7. Generate preview
    8. Upload results to Cloudinary

    background_mode:
      "auto" (default) replaces a background that fails validation.
      "keep" is crop-only: the original pixels are only cropped, resized and
      compressed. The background is never replaced and the frame is never padded
      with a fill colour. Use it where the issuing authority rejects edited photos
      (e.g. US passports). The background is still validated and reported in
      metrics.background_valid.
    """
    start_time = time.time()

    background_mode = (background_mode or "auto").strip().lower()
    if background_mode not in ("auto", "keep"):
        raise HTTPException(
            status_code=400,
            detail="background_mode must be 'auto' or 'keep'",
        )
    crop_only = background_mode == "keep"

    # ─── Load config ───
    config = get_config(country_code, document_type)
    if config is None:
        raise HTTPException(
            status_code=400,
            detail=f"No config found for country '{country_code}' "
                   f"document_type '{document_type}'",
        )

    # ─── Step 0: Validate and Read Uploaded Image ───
    allowed_types = {"image/jpeg", "image/png", "image/webp", "image/bmp", "image/tiff"}
    if image.content_type not in allowed_types:
        raise HTTPException(
            status_code=400,
            detail=f"Unsupported file type: {image.content_type}. "
                   f"Allowed: {', '.join(allowed_types)}",
        )

    file_bytes = await image.read()
    max_bytes = settings.max_upload_size_mb * 1024 * 1024
    if len(file_bytes) > max_bytes:
        raise HTTPException(
            status_code=400,
            detail=f"File too large. Max: {settings.max_upload_size_mb}MB",
        )

    # Decode image directly from bytes
    np_arr = np.frombuffer(file_bytes, np.uint8)
    processed_image = cv2.imdecode(np_arr, cv2.IMREAD_COLOR)
    if processed_image is None:
        raise HTTPException(status_code=400, detail="Invalid image file")

    # Upload original to Cloudinary in background
    background_tasks.add_task(upload_original, file_bytes, image.filename or "photo.jpg")

    try:
        # ─── Step 1: Face detection ───
        logger.info(f"Processing: country={country_code}, doc={document_type}")
        cv_image = processed_image  # decoded numpy array

        try:
            face = detect_face(cv_image)
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))

        # ─── Step 2: Hair crown detection (BEFORE background change) ───
        try:
            crown_y = detect_hair_crown(cv_image)
        except ValueError as e:
            logger.warning(f"Hair detection failed, using face estimate: {e}")
            crown_y = face.top_of_head_y

        # ─── Step 3: Background validation ───
        bg_color = config.background_rules.color
        bg_validation = validate_background(cv_image, target_color=bg_color)
        background_corrected = False

        # ─── Step 4: Background correction if needed (never in crop-only mode) ───
        if not bg_validation.is_valid and crop_only:
            logger.info(f"Crop-only: keeping original background ({bg_validation.message})")
        elif not bg_validation.is_valid:
            logger.info(f"Background invalid: {bg_validation.message}")

            corrected_image, success = correct_background(cv_image, target_color=bg_color)

            if not success:
                raise HTTPException(
                    status_code=400,
                    detail="Background cannot be corrected automatically. "
                           "Please use a photo with a plain white background.",
                )

            cv_image = corrected_image
            background_corrected = True
            bg_validation = validate_background(cv_image, target_color=bg_color)

        # ─── Step 5: Crop ───
        try:
            crop_result = compute_crop(
                cv_image, face, crown_y, config, allow_padding=not crop_only
            )
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))

        # ─── Step 6: Compress ───
        max_size_kb = config.digital_requirements.max_file_size_kb
        target_dpi = config.photo_spec.print_dpi

        photo_bytes = compress_to_jpeg(
            crop_result.image,
            max_size_kb=max_size_kb,
            target_dpi=target_dpi,
        )

        # ─── Step 7: Preview Image ───
        preview_bytes = create_preview_image(
            crop_result.image,
            config,
            crop_result.head_height_pct,
            crop_result.eye_from_bottom_pct,
            crop_result.top_margin_pct,
        )

        # ─── Step 8: Save results to Cloudinary ───
        result_id = str(uuid.uuid4())
        urls = upload_results(result_id, photo_bytes, preview_bytes)

        elapsed = time.time() - start_time
        logger.info(f"Processing complete in {elapsed:.2f}s — result_id={result_id}")

        return ProcessResponse(
            status="success",
            result_id=result_id,
            image_url=urls.get("image_url", ""),
            preview_url=urls.get("preview_url"),
            dimensions=f"{config.target_width_px}x{config.target_height_px}",
            format="JPEG",
            size_kb=len(photo_bytes) // 1024,
            metrics=ProcessingMetrics(
                head_height_pct=crop_result.head_height_pct,
                eye_position_pct=crop_result.eye_from_bottom_pct,
                top_margin_pct=crop_result.top_margin_pct,
                background_valid=bg_validation.is_valid,
                background_corrected=background_corrected,
                background_mode=background_mode,
            ),
        )

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Processing failed: {e}", exc_info=True)
        raise HTTPException(
            status_code=500,
            detail=f"Processing failed: {str(e)}",
        )



# ─── GET /countries ───

@router.get(
    "/countries",
    response_model=list[CountrySummary],
    summary="List all supported countries",
)
async def list_countries():
    """Get a list of all supported country/document configurations."""
    return list_supported()


@router.get(
    "/countries/{code}",
    summary="Get country config",
)
async def get_country_config(code: str, document_type: str = "passport"):
    """Get the full configuration for a specific country."""
    config = get_config(code, document_type)
    if config is None:
        raise HTTPException(
            status_code=404,
            detail=f"No config for '{code}' / '{document_type}'",
        )
    return config.model_dump()


# ─── GET /health ───

@router.get(
    "/health",
    response_model=HealthResponse,
    summary="Health check",
)
async def health_check():
    """Check API health and loaded config count."""
    configs = get_all_configs()
    return HealthResponse(
        status="healthy",
        version="1.0.0",
        countries_loaded=len(configs),
    )
