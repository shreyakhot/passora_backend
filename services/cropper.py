"""
Smart crop engine — hair-safe, eye-aligned passport photo cropping.

Port of the existing TypeScript crop logic from route.ts.

Cropping rules (STRICT):
  1. Detect eyes, chin, crown (hair top)
  2. NEVER cut above crown
  3. ALWAYS add min 5% space above crown
  4. Align eyes to target percentage
  5. Maintain aspect ratio
  6. Clamp head size to allowed range
"""

import logging
from dataclasses import dataclass

import cv2
import numpy as np

from config.countries.schema import CountryConfig
from .face_detector import FaceDetectionResult

logger = logging.getLogger(__name__)


@dataclass
class CropResult:
    """Result of the crop operation."""
    image: np.ndarray             # Cropped & resized BGR image
    crop_top: int
    crop_left: int
    crop_width: int
    crop_height: int
    head_height_pct: float        # final head % in output
    eye_from_bottom_pct: float    # final eye position in output
    top_margin_pct: float         # space above head in output


def compute_crop(
    image: np.ndarray,
    face: FaceDetectionResult,
    crown_y: float,
    config: CountryConfig,
    allow_padding: bool = True,
) -> CropResult:
    """
    Compute and execute the crop for a passport photo.

    This implements the same algorithm as route.ts, ported to Python:
      1. Compute full head height using multiplier
      2. Calculate initial crop frame from head % target
      3. Align eyes to target position
      4. Hair-safe solver: push crop up if crown is in danger
      5. Clamp to legal head size range
      6. Re-validate eye position
      7. Pad/extract with background color
      8. Resize to target dimensions

    Args:
        image: BGR numpy array (original image, possibly bg-corrected)
        face: FaceDetectionResult from face_detector
        crown_y: hair crown Y position from hair_detector
        config: CountryConfig for the target country

    Returns:
        CropResult with the cropped image and metrics

    Raises:
        ValueError: if crop cannot satisfy constraints
    """
    img_h, img_w = image.shape[:2]

    # ─── Config values ───
    multiplier = config.auto_crop_config.head_top_multiplier
    target_w_px = config.target_width_px
    target_h_px = config.target_height_px
    target_aspect = config.target_aspect_ratio

    min_head_pct = config.face_constraints.head_height_pct.min / 100.0
    max_head_pct = config.face_constraints.head_height_pct.max / 100.0
    target_head_pct = (min_head_pct + max_head_pct) / 2.0

    min_eye_from_bottom = config.face_constraints.eye_position.from_bottom_pct.min / 100.0
    max_eye_from_bottom = config.face_constraints.eye_position.from_bottom_pct.max / 100.0
    # "from top" is 1 - "from bottom"
    min_eye_from_top = 1.0 - max_eye_from_bottom
    max_eye_from_top = 1.0 - min_eye_from_bottom
    target_eye_from_top = (min_eye_from_top + max_eye_from_top) / 2.0

    safe_padding_pct = config.auto_crop_config.safe_padding_pct / 100.0

    # ─── Step 1: Compute full head height ───
    face_height = face.chin_y - face.forehead_y
    full_head_height = face_height * multiplier

    # Take the higher (more conservative) of formula vs actual crown
    formula_top = face.chin_y - full_head_height
    true_top_of_head = min(formula_top, crown_y)

    # Recalculate full head height from true top
    full_head_height = face.chin_y - true_top_of_head

    logger.info(
        f"Head measurement: face_height={face_height:.0f}, "
        f"full_head_height={full_head_height:.0f}, "
        f"formula_top={formula_top:.0f}, crown_y={crown_y:.0f}, "
        f"true_top={true_top_of_head:.0f}"
    )

    # ─── Step 2: Initial crop frame ───
    crop_height = full_head_height / target_head_pct
    crop_width = crop_height * target_aspect
    
    if config.face_constraints.top_margin_pct.recommended:
        target_margin = config.face_constraints.top_margin_pct.recommended / 100.0
        crop_top = true_top_of_head - (crop_height * target_margin)
        logger.info(f"Using strict top margin: {target_margin*100}%")
    else:
        crop_top = face.eye_center.y - (crop_height * target_eye_from_top)

    # ─── Step 3: Adaptive caution margin (hair protection) ───
    crown_gap = crop_top - true_top_of_head
    adaptive_pad = full_head_height * 0.09
    caution_margin = max(
        full_head_height * safe_padding_pct,
        crown_gap + adaptive_pad if crown_gap > 0 else full_head_height * safe_padding_pct,
    )

    # ─── Step 4: Hair-safe solver ───
    if crop_top > true_top_of_head - caution_margin:
        crop_top = true_top_of_head - caution_margin

        # Re-check eye position
        eye_offset = face.eye_center.y - crop_top
        if (eye_offset / crop_height) > max_eye_from_top:
            crop_height = eye_offset / max_eye_from_top
            crop_width = crop_height * target_aspect
            crop_top = face.eye_center.y - (crop_height * max_eye_from_top)

    # ─── Step 5: Clamp to legal head size ───
    max_crop_h = full_head_height / min_head_pct
    min_crop_h = full_head_height / max_head_pct

    if crop_height < min_crop_h:
        crop_height = min_crop_h
        crop_width = crop_height * target_aspect

    if crop_height > max_crop_h:
        crop_height = max_crop_h
        crop_width = crop_height * target_aspect

    # ─── Step 6: Eye re-validation ───
    eye_from_top_ratio = (face.eye_center.y - crop_top) / crop_height

    if not config.face_constraints.top_margin_pct.recommended:
        if eye_from_top_ratio > max_eye_from_top:
            crop_top = face.eye_center.y - (crop_height * max_eye_from_top)
        elif eye_from_top_ratio < min_eye_from_top:
            crop_top = face.eye_center.y - (crop_height * min_eye_from_top)
    else:
        if eye_from_top_ratio > max_eye_from_top or eye_from_top_ratio < min_eye_from_top:
            logger.warning(f"Strict top margin caused eye ratio ({eye_from_top_ratio:.2f}) to fall outside bounds [{min_eye_from_top:.2f}, {max_eye_from_top:.2f}]")

    # Re-enforce crown margin after eye revalidation
    if crop_top > true_top_of_head - caution_margin:
        crop_top = true_top_of_head - caution_margin

    # ─── Step 7: Safety clamps ───
    max_allowed = max(img_w, img_h) * 2
    if crop_height > max_allowed:
        crop_height = max_allowed
        crop_width = crop_height * target_aspect

    if crop_height < 200:
        crop_height = 200
        crop_width = crop_height * target_aspect

    # ─── Validate head % ───
    head_pct = (full_head_height / crop_height) * 100
    if head_pct < min_head_pct * 100 or head_pct > max_head_pct * 100:
        raise ValueError(
            f"Head size ({head_pct:.0f}%) is outside the allowed "
            f"{min_head_pct * 100:.0f}–{max_head_pct * 100:.0f}% range. "
            f"Please retake the photo with your face closer to or further from the camera."
        )

    # ─── Step 8: Round and compute crop_left ───
    crop_height = int(round(crop_height))
    crop_width = int(round(crop_width))
    crop_top = int(round(crop_top))

    face_center_x = face.face_box.x + (face.face_box.width / 2)
    crop_left = int(round(face_center_x - crop_width / 2))

    logger.info(
        f"Crop computed: head_pct={head_pct:.1f}%, "
        f"crop=({crop_left},{crop_top},{crop_width}x{crop_height}), "
        f"caution_margin={caution_margin:.0f}"
    )

    # ─── Step 9: Extract with padding ───
    extract_left = max(0, crop_left)
    extract_top = max(0, crop_top)
    extract_right = min(img_w, crop_left + crop_width)
    extract_bottom = min(img_h, crop_top + crop_height)

    extract_w = extract_right - extract_left
    extract_h = extract_bottom - extract_top

    if extract_w < 50 or extract_h < 50:
        raise ValueError(
            "Face is too close to the image edge or out of frame. "
            "Please retake the photo with your face centered."
        )

    # Extract the region from the original image
    extracted = image[extract_top:extract_bottom, extract_left:extract_right]

    # Compute padding needed
    pad_top = max(0, -crop_top)
    pad_left = max(0, -crop_left)
    pad_bottom = max(0, (crop_top + crop_height) - img_h)
    pad_right = max(0, (crop_left + crop_width) - img_w)

    # Determine background color for padding
    bg_color_str = config.background_rules.color
    from .background import _parse_bg_color
    bg_rgb = _parse_bg_color(bg_color_str)
    bg_bgr = (bg_rgb[2], bg_rgb[1], bg_rgb[0])

    needs_padding = pad_top > 0 or pad_bottom > 0 or pad_left > 0 or pad_right > 0

    if needs_padding and not allow_padding:
        # Crop-only mode must not add background the camera did not capture.
        # A sliver from rounding (up to 1% of the frame) repeats the edge pixels;
        # anything larger means the photo is framed too tightly.
        max_pad = max(2, int(round(min(crop_width, crop_height) * 0.01)))
        if max(pad_top, pad_bottom, pad_left, pad_right) > max_pad:
            raise ValueError(
                "Your head is too close to the edge of the photo to crop it without "
                "adding background. Take the photo from a little further away, with "
                "space above your head and your shoulders in the frame."
            )
        padded = cv2.copyMakeBorder(
            extracted,
            pad_top, pad_bottom, pad_left, pad_right,
            cv2.BORDER_REPLICATE,
        )
    elif needs_padding:
        padded = cv2.copyMakeBorder(
            extracted,
            pad_top, pad_bottom, pad_left, pad_right,
            cv2.BORDER_CONSTANT,
            value=bg_bgr,
        )
    else:
        padded = extracted

    logger.info(
        f"Extract: {extract_w}x{extract_h}, "
        f"padding T={pad_top} B={pad_bottom} L={pad_left} R={pad_right}"
    )

    # ─── Step 10: Resize to target dimensions ───
    resized = cv2.resize(padded, (target_w_px, target_h_px), interpolation=cv2.INTER_LANCZOS4)

    # ─── Compute final metrics ───
    output_scale = target_h_px / crop_height
    scaled_head = full_head_height * output_scale
    final_head_pct = (scaled_head / target_h_px) * 100

    effective_eye_y = face.eye_center.y - extract_top + pad_top
    scaled_eye_y = effective_eye_y * (target_h_px / crop_height)
    final_eye_from_bottom = ((target_h_px - scaled_eye_y) / target_h_px) * 100

    # Top margin: distance from top of output to top of head
    effective_head_top = true_top_of_head - extract_top + pad_top
    scaled_head_top = effective_head_top * (target_h_px / crop_height)
    final_top_margin = (scaled_head_top / target_h_px) * 100

    logger.info(
        f"Final metrics: head_pct={final_head_pct:.1f}%, "
        f"eye_from_bottom={final_eye_from_bottom:.1f}%, "
        f"top_margin={final_top_margin:.1f}%"
    )

    return CropResult(
        image=resized,
        crop_top=crop_top,
        crop_left=crop_left,
        crop_width=crop_width,
        crop_height=crop_height,
        head_height_pct=round(final_head_pct, 1),
        eye_from_bottom_pct=round(final_eye_from_bottom, 1),
        top_margin_pct=round(final_top_margin, 1),
    )
