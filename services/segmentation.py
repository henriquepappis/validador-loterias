"""Recorte automático de cada bilhete numa foto com vários comprovantes.

`split_tickets` recebe os bytes de uma imagem e devolve uma lista de bytes JPEG,
um por bilhete detectado (com correção de perspectiva e ampliado se pequeno).
Quando não separa com confiança, devolve ``[imagem_original]`` — o pipeline
então processa a foto inteira.

Estratégia: bilhetes são papel claro sobre fundo mais escuro. Binariza por Otsu,
limpa ruído e fecha entrelinhas, extrai os retângulos dos componentes e, quando
dois/três bilhetes encostados viram um único blob alongado, fatia o blob no
número provável de bilhetes. Formas improváveis (proporção ou preenchimento
fora do comum) são descartadas.
"""
from __future__ import annotations

import logging

import cv2
import numpy as np

from config import settings

logger = logging.getLogger(__name__)

_MAX_TICKETS = 16
_MIN_SIDE_PX = 150
_TICKET_ASPECT = 1.5      # proporção típica de um comprovante da Caixa (retrato)
_MAX_ASPECT = 4.2         # acima disso não é 1–3 bilhetes em fila
_MIN_EXTENT = 0.62        # área do contorno / área do retângulo mínimo


def split_tickets(image_bytes: bytes) -> list[bytes]:
    if not settings.segmentation_enabled:
        return [image_bytes]
    try:
        crops = _segment(image_bytes)
    except Exception:  # noqa: BLE001 - segmentação é best-effort
        logger.exception("Falha na segmentação; usando a imagem inteira")
        return [image_bytes]
    if len(crops) < 2:
        return [_enhance_bytes(image_bytes)]
    logger.info("Segmentação: %d bilhetes recortados", len(crops))
    return crops


def _enhance_bytes(image_bytes: bytes) -> bytes:
    img = _decode(image_bytes)
    if img is None:
        return image_bytes
    out = _upscale(_enhance(img))
    ok, buf = cv2.imencode(".jpg", out, [cv2.IMWRITE_JPEG_QUALITY, 95])
    return buf.tobytes() if ok else image_bytes


def _decode(image_bytes: bytes) -> np.ndarray | None:
    return cv2.imdecode(np.frombuffer(image_bytes, np.uint8), cv2.IMREAD_COLOR)


def _otsu(img: np.ndarray) -> np.ndarray:
    gray = cv2.GaussianBlur(cv2.cvtColor(img, cv2.COLOR_BGR2GRAY), (5, 5), 0)
    _, mask = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
    return mask


def _segment(image_bytes: bytes) -> list[bytes]:
    img = _decode(image_bytes)
    if img is None:
        return []

    height, width = img.shape[:2]
    image_area = float(height * width)
    mask = _otsu(img)

    white_frac = float((mask > 0).mean())
    if not 0.12 < white_frac < 0.97:
        return []  # sem contraste papel/fundo suficiente

    mask = cv2.morphologyEx(
        mask, cv2.MORPH_OPEN, cv2.getStructuringElement(cv2.MORPH_RECT, (5, 5))
    )
    mask = cv2.morphologyEx(
        mask, cv2.MORPH_CLOSE, cv2.getStructuringElement(cv2.MORPH_RECT, (7, 7))
    )

    boxes = _boxes_from_mask(mask, image_area)
    if len(boxes) < 2:
        # Última tentativa: erode para quebrar pontes largas.
        eroded = cv2.erode(
            mask, cv2.getStructuringElement(cv2.MORPH_RECT, (17, 17)), iterations=2
        )
        boxes = _boxes_from_mask(eroded, image_area, margin=34)
    if len(boxes) < 2:
        return []

    crops: list[bytes] = []
    for box in _reading_order(boxes, height)[:_MAX_TICKETS]:
        crop = _upscale(_enhance(_trim_margins(_warp(img, box))))
        ok, buf = cv2.imencode(".jpg", crop, [cv2.IMWRITE_JPEG_QUALITY, 95])
        if ok:
            crops.append(buf.tobytes())
    return crops


def _trim_margins(crop: np.ndarray) -> np.ndarray:
    """Remove as faixas laterais com a marca d'água 'loterias CAIXA'."""
    h, w = crop.shape[:2]
    x0, x1 = int(w * 0.05), int(w * 0.95)
    y0, y1 = int(h * 0.015), int(h * 0.985)
    trimmed = crop[y0:y1, x0:x1]
    return trimmed if trimmed.size else crop


def _enhance(crop: np.ndarray) -> np.ndarray:
    """Realça o texto impresso sobre o papel (contraste local + nitidez)."""
    gray = cv2.cvtColor(crop, cv2.COLOR_BGR2GRAY)
    gray = cv2.createCLAHE(clipLimit=2.5, tileGridSize=(8, 8)).apply(gray)
    blur = cv2.GaussianBlur(gray, (0, 0), 3)
    sharp = cv2.addWeighted(gray, 1.6, blur, -0.6, 0)
    return cv2.cvtColor(sharp, cv2.COLOR_GRAY2BGR)


def _boxes_from_mask(
    mask: np.ndarray, image_area: float, margin: int = 0
) -> list[np.ndarray]:
    contours, _ = cv2.findContours(
        mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE
    )
    boxes: list[np.ndarray] = []
    for contour in contours:
        contour_area = cv2.contourArea(contour)
        if contour_area < image_area * 0.03:
            continue
        (cx, cy), (rw, rh), angle = cv2.minAreaRect(contour)
        if rw < _MIN_SIDE_PX or rh < _MIN_SIDE_PX:
            continue
        if rw * rh > image_area * 0.85:
            continue
        long_side, short_side = max(rw, rh), max(1.0, min(rw, rh))
        aspect = long_side / short_side
        if aspect > _MAX_ASPECT:
            continue
        if contour_area / (rw * rh) < _MIN_EXTENT:
            continue

        inflated = ((cx, cy), (rw + 2 * margin, rh + 2 * margin), angle)
        box = _order_corners(cv2.boxPoints(inflated).astype("float32"))

        pieces = max(1, round(aspect / _TICKET_ASPECT))
        if pieces == 1:
            boxes.append(box)
        else:
            boxes.extend(_slice_box(box, pieces))
    return boxes


def _slice_box(box: np.ndarray, n: int) -> list[np.ndarray]:
    """Fatia um quadrilátero (tl, tr, br, bl) em n partes ao longo do lado maior."""
    tl, tr, br, bl = box
    if np.linalg.norm(tr - tl) >= np.linalg.norm(bl - tl):
        a0, a1, b0, b1 = tl, tr, bl, br  # eixo longo = tl->tr
    else:
        a0, a1, b0, b1 = tl, bl, tr, br  # eixo longo = tl->bl

    out: list[np.ndarray] = []
    for i in range(n):
        f0, f1 = i / n, (i + 1) / n
        p0 = a0 + (a1 - a0) * f0
        p1 = a0 + (a1 - a0) * f1
        p2 = b0 + (b1 - b0) * f1
        p3 = b0 + (b1 - b0) * f0
        out.append(_order_corners(np.array([p0, p1, p2, p3], dtype="float32")))
    return out


def _reading_order(boxes: list[np.ndarray], height: int) -> list[np.ndarray]:
    """Ordena em linhas (topo→base) e, dentro de cada linha, esquerda→direita."""

    def center(box: np.ndarray) -> tuple[float, float]:
        return float(box[:, 0].mean()), float(box[:, 1].mean())

    rows: list[list[np.ndarray]] = []
    for box in sorted(boxes, key=lambda b: center(b)[1]):
        cy = center(box)[1]
        for row in rows:
            mean_y = sum(center(b)[1] for b in row) / len(row)
            if abs(cy - mean_y) < 0.18 * height:
                row.append(box)
                break
        else:
            rows.append([box])

    rows.sort(key=lambda row: sum(center(b)[1] for b in row) / len(row))
    ordered: list[np.ndarray] = []
    for row in rows:
        row.sort(key=lambda b: center(b)[0])
        ordered.extend(row)
    return ordered


def _order_corners(box: np.ndarray) -> np.ndarray:
    s = box.sum(axis=1)
    d = np.diff(box, axis=1).ravel()
    return np.array(
        [
            box[np.argmin(s)],  # top-left
            box[np.argmin(d)],  # top-right
            box[np.argmax(s)],  # bottom-right
            box[np.argmax(d)],  # bottom-left
        ],
        dtype="float32",
    )


def _warp(img: np.ndarray, box: np.ndarray) -> np.ndarray:
    tl, tr, br, bl = box
    out_w = int(max(np.linalg.norm(br - bl), np.linalg.norm(tr - tl)))
    out_h = int(max(np.linalg.norm(tr - br), np.linalg.norm(tl - bl)))
    if out_w < 20 or out_h < 20:
        x, y, w, h = cv2.boundingRect(box.astype(np.int32))
        return img[max(0, y) : y + h, max(0, x) : x + w]

    dst = np.array(
        [[0, 0], [out_w - 1, 0], [out_w - 1, out_h - 1], [0, out_h - 1]],
        dtype="float32",
    )
    return cv2.warpPerspective(
        img, cv2.getPerspectiveTransform(box.astype("float32"), dst), (out_w, out_h)
    )


def _upscale(crop: np.ndarray) -> np.ndarray:
    target = settings.segmentation_upscale_to
    longest = max(crop.shape[:2]) if crop.size else 0
    if longest == 0 or longest >= target:
        return crop
    scale = target / longest
    return cv2.resize(
        crop, None, fx=scale, fy=scale, interpolation=cv2.INTER_CUBIC
    )
