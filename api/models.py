"""
Pydantic models for API request/response schemas.
"""

from pydantic import BaseModel, Field
from typing import Optional


# ─── Requestss ───

class ProcessingMetrics(BaseModel):
    """Biometric compliance metrics for the processed photo."""
    head_height_pct: float = Field(
        ..., description="Head height as % of image height"
    )
    eye_position_pct: float = Field(
        ..., description="Eye position as % from bottom"
    )
    top_margin_pct: float = Field(
        ..., description="Space above head as % of image height"
    )
    background_valid: bool = Field(
        ..., description="Whether background passed validation"
    )
    background_corrected: bool = Field(
        default=False,
        description="Whether background was auto-corrected",
    )


class ProcessResponse(BaseModel):
    """Response from POST /process."""
    status: str = "success"
    result_id: str
    image_url: str
    preview_url: Optional[str] = None
    dimensions: str
    format: str = "JPEG"
    size_kb: int
    metrics: ProcessingMetrics


class ValidationItem(BaseModel):
    """A single validation check result."""
    label: str
    value: str
    status: str = "success"  # success, warning, error
    details: Optional[str] = None
    target: Optional[str] = None


class ValidationResponse(BaseModel):
    """Comprehensive validation report response."""
    status: str = "success"
    overall_result: str = "PASS"  # PASS or FAIL
    score: int = 100  # Score out of 100
    summary: list[str]
    suggestions: list[str] = []
    file_format: ValidationItem
    file_size: ValidationItem
    resolution: ValidationItem
    lighting: ValidationItem
    face_detection: ValidationItem
    eye_level: ValidationItem
    head_size: ValidationItem
    head_tilt: ValidationItem
    orientation: ValidationItem
    dpi: Optional[ValidationItem] = None


class ErrorResponse(BaseModel):
    """Error response."""
    status: str = "error"
    error: str
    details: Optional[str] = None


class CountrySummary(BaseModel):
    """Summary of a supported country config."""
    country_code: str
    country_name: str
    document_type: str
    dimensions: str


class HealthResponse(BaseModel):
    """Health check response."""
    status: str = "healthy"
    version: str = "1.0.0"
    countries_loaded: int = 0
