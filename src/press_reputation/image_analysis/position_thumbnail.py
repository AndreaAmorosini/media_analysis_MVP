from pathlib import Path

import fitz

from press_reputation.models.page import PageRecord, RegionType


def enrich_article_position_thumbnails(pdf_path: Path, page: PageRecord, zoom: float = 2.0, dark_threshold: int = 110) -> PageRecord:
    """
    Analizza le regioni article_position_thumbnail.

    Estrae:
    - area relativa occupata dall'articolo nella miniatura
    - posizione dell'articolo nella pagina originale
    - prominenza stimata usando anche original_page
    """
    pdf_path = Path(pdf_path)
    pdf_doc = fitz.open(pdf_path)

    try:
        pdf_page = pdf_doc[page.pdf_page - 1]
        original_page = page.source.original_page

        for region in page.regions:
            if region.type != RegionType.ARTICLE_POSITION_THUMBNAIL:
                continue

            if not region.bbox or len(region.bbox) != 4:
                continue

            analysis = analyze_thumbnail_region(
                pdf_page=pdf_page,
                bbox=region.bbox,
                original_page=original_page,
                zoom=zoom,
                dark_threshold=dark_threshold,
            )

            region.metadata.update(analysis)

    finally:
        pdf_doc.close()

    return page


def analyze_thumbnail_region(pdf_page: fitz.Page, bbox: list[float], original_page: int | None = None, zoom: float = 2.0, dark_threshold: int = 110,) -> dict:
    x0, y0, x1, y1 = bbox
    clip = fitz.Rect(x0, y0, x1, y1)

    pixmap = pdf_page.get_pixmap(
        matrix=fitz.Matrix(zoom, zoom),
        clip=clip,
        colorspace=fitz.csGRAY,
        alpha=False,
    )

    width = pixmap.width
    height = pixmap.height

    marker_bbox = detect_dark_bbox(
        samples=pixmap.samples,
        width=width,
        height=height,
        dark_threshold=dark_threshold,
    )

    base_metadata = {
        "technical_image": True,
        "exclude_from_article_media": True,
        "thumbnail_pixel_width": width,
        "thumbnail_pixel_height": height,
        "thumbnail_detection_method": "dark_pixel_bbox",
        "original_page_for_prominence": original_page,
    }

    if marker_bbox is None:
        return {
            **base_metadata,
            "detected_article_marker": False,
            "article_marker_bbox_in_thumbnail": None,
            "original_page_relative_area": None,
            "original_page_vertical_area": None,
            "original_page_horizontal_area": None,
            "original_page_zone": None,
            "original_page_coarse_vertical_area": None,
            "original_page_coarse_horizontal_area": None,
            "estimated_prominence": None,
            "prominence_score": None,
        }

    mx0, my0, mx1, my1 = marker_bbox

    thumbnail_area = max(width * height, 1)
    marker_width = max(mx1 - mx0, 0)
    marker_height = max(my1 - my0, 0)
    marker_area = marker_width * marker_height
    relative_area = marker_area / thumbnail_area

    normalized_bbox = [
        mx0 / width,
        my0 / height,
        mx1 / width,
        my1 / height,
    ]

    position = compute_position(normalized_bbox)

    prominence = estimate_prominence(
        original_page=original_page,
        vertical_area=position["vertical_area"],
        relative_area=relative_area,
    )

    return {
        **base_metadata,
        "detected_article_marker": True,
        "article_marker_bbox_in_thumbnail": normalized_bbox,
        "original_page_relative_area": relative_area,
        "original_page_vertical_area": position["vertical_area"],
        "original_page_horizontal_area": position["horizontal_area"],
        "original_page_zone": position["zone"],
        "original_page_coarse_vertical_area": position["coarse_vertical_area"],
        "original_page_coarse_horizontal_area": position["coarse_horizontal_area"],
        "estimated_prominence": prominence["label"],
        "prominence_score": prominence["score"],
    }


def detect_dark_bbox(samples: bytes, width: int, height: int, dark_threshold: int,) -> tuple[int, int, int, int] | None:
    min_x = width
    min_y = height
    max_x = -1
    max_y = -1

    for y in range(height):
        row_start = y * width

        for x in range(width):
            value = samples[row_start + x]

            if value > dark_threshold:
                continue

            min_x = min(min_x, x)
            min_y = min(min_y, y)
            max_x = max(max_x, x)
            max_y = max(max_y, y)

    if max_x < 0 or max_y < 0:
        return None

    return min_x, min_y, max_x + 1, max_y + 1


def compute_position(normalized_bbox: list[float],) -> dict[str, str]:
    x0, y0, x1, y1 = normalized_bbox

    cx = (x0 + x1) / 2
    cy = (y0 + y1) / 2

    if cy < 0.33:
        vertical = "top"
        coarse_vertical = "top_area"
    elif cy < 0.66:
        vertical = "middle"
        coarse_vertical = "middle_area"
    else:
        vertical = "bottom"
        coarse_vertical = "bottom_area"

    if cx < 0.33:
        horizontal = "left"
        coarse_horizontal = "left"
    elif cx < 0.66:
        horizontal = "center"
        coarse_horizontal = "middle"
    else:
        horizontal = "right"
        coarse_horizontal = "right"

    return {
        "vertical_area": vertical,
        "horizontal_area": horizontal,
        "zone": f"{vertical}_{horizontal}",
        "coarse_vertical_area": coarse_vertical,
        "coarse_horizontal_area": coarse_horizontal,
    }


def estimate_prominence(original_page: int | None, vertical_area: str, relative_area: float) -> dict[str, float | str]:
    """
    Stima euristica della prominenza editoriale.

    Componenti:
    - original_page_score: pagina originale più bassa = più rilevanza
    - area_score: maggiore spazio occupato = più rilevanza
    - vertical_score: alto pagina = più rilevanza
    """

    page_score = original_page_score(original_page)
    area_score = relative_area_score(relative_area)
    vertical_score = vertical_position_score(vertical_area)

    score = (
        page_score * 0.40
        + area_score * 0.40
        + vertical_score * 0.20
    )

    if score >= 0.70:
        label = "high"
    elif score >= 0.40:
        label = "medium"
    else:
        label = "low"

    return {
        "label": label,
        "score": round(score, 4),
    }


def original_page_score(original_page: int | None) -> float:
    """
    Score tra 0 e 1.

    Euristica:
    - pagina 1: massimo valore
    - pagine 2-3: alto
    - pagine 4-10: medio
    - oltre 10: basso
    - sconosciuta: neutro basso
    """
    if original_page is None:
        return 0.35

    if original_page <= 1:
        return 1.0

    if original_page <= 3:
        return 0.85

    if original_page <= 5:
        return 0.70

    if original_page <= 10:
        return 0.50

    if original_page <= 20:
        return 0.30

    return 0.20


def relative_area_score(relative_area: float) -> float:
    """
    Converte area relativa in score 0-1.
    """
    if relative_area >= 0.35:
        return 1.0

    if relative_area >= 0.25:
        return 0.85

    if relative_area >= 0.15:
        return 0.65

    if relative_area >= 0.08:
        return 0.45

    if relative_area >= 0.03:
        return 0.25

    return 0.10


def vertical_position_score(vertical_area: str) -> float:
    if vertical_area == "top":
        return 1.0

    if vertical_area == "middle":
        return 0.60

    return 0.30