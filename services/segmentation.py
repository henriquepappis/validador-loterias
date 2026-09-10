"""Detecção e recorte de cada bilhete numa foto com vários comprovantes.

Fluxo do app (em duas etapas):
  1. `detect_boxes(bytes)` -> caixas normalizadas (x, y, w, h) de cada bilhete,
     em ordem de leitura. O usuário confere/ajusta essas caixas na tela.
  2. `crop_box(bytes, box)` -> JPEG de um recorte (endireitado, sem as faixas
     laterais da marca d'água, com contraste realçado e ampliado).

`split_tickets` é um atalho: detecta e recorta tudo de uma vez.

Estratégia de detecção: bilhetes são papel claro sobre fundo mais escuro.
Binariza por Otsu, limpa ruído e fecha entrelinhas, extrai os retângulos dos
componentes e, quando 2–3 bilhetes encostados viram um blob alongado, fatia o
blob no número provável de bilhetes. Formas improváveis são descartadas.
"""
from __future__ import annotations

import logging

import cv2
import numpy as np

from config import settings

logger = logging.getLogger(__name__)

Box = tuple[float, float, float, float]  # x, y, w, h normalizados (0..1)

_MAX_TICKETS = 16
_MIN_SIDE_PX = 150
_TICKET_ASPECT = 1.5      # proporção típica de um comprovante da Caixa (retrato)
_MAX_ASPECT = 4.2         # acima disso não é 1–3 bilhetes em fila
_MIN_EXTENT = 0.62        # área do contorno / área do retângulo mínimo


# --------------------------------------------------------------------------- #
# API pública
# --------------------------------------------------------------------------- #
def detect_boxes(image_bytes: bytes) -> list[Box]:
    """Caixas normalizadas de cada bilhete, em ordem de leitura.

    Vazio quando não dá para separar com confiança (o app então oferece a foto
    inteira como recorte único).
    """
    if not settings.segmentation_enabled:
        return []
    try:
        img = _decode(image_bytes)
        if img is None:
            return []
        height, width = img.shape[:2]
        quads = _detect_quads(img)
    except Exception:  # noqa: BLE001 - detecção é best-effort
        logger.exception("Falha na detecção de bilhetes")
        return []

    if len(quads) < 2:
        return []

    boxes: list[Box] = []
    for quad in _reading_order(quads, height):
        x0 = max(0.0, float(quad[:, 0].min()) / width)
        y0 = max(0.0, float(quad[:, 1].min()) / height)
        x1 = min(1.0, float(quad[:, 0].max()) / width)
        y1 = min(1.0, float(quad[:, 1].max()) / height)
        if x1 - x0 > 0.03 and y1 - y0 > 0.03:
            boxes.append((x0, y0, x1 - x0, y1 - y0))
    return boxes[:_MAX_TICKETS]


def crop_box(image_bytes: bytes, box: Box) -> bytes:
    """Recorta a caixa (normalizada) da foto e devolve um JPEG tratado."""
    img = _decode(image_bytes)
    if img is None:
        return _enhance_bytes(image_bytes)

    height, width = img.shape[:2]
    x, y, bw, bh = box
    pad_x, pad_y = bw * 0.03, bh * 0.03
    x0 = int(max(0.0, x - pad_x) * width)
    y0 = int(max(0.0, y - pad_y) * height)
    x1 = int(min(1.0, x + bw + pad_x) * width)
    y1 = int(min(1.0, y + bh + pad_y) * height)
    sub = img[y0:y1, x0:x1]
    if sub.size == 0:
        sub = img

    out = _upscale(_enhance(_trim_margins(_deskew_sub(sub))))
    ok, buf = cv2.imencode(".jpg", out, [cv2.IMWRITE_JPEG_QUALITY, 95])
    return buf.tobytes() if ok else _enhance_bytes(image_bytes)


def split_tickets(image_bytes: bytes) -> list[bytes]:
    """Detecta e recorta todos os bilhetes de uma vez (atalho)."""
    boxes = detect_boxes(image_bytes)
    if not boxes:
        return [_enhance_bytes(image_bytes)]
    return [crop_box(image_bytes, b) for b in boxes]


# --------------------------------------------------------------------------- #
# Internos
# --------------------------------------------------------------------------- #
def _decode(image_bytes: bytes) -> np.ndarray | None:
    return cv2.imdecode(np.frombuffer(image_bytes, np.uint8), cv2.IMREAD_COLOR)


def _enhance_bytes(image_bytes: bytes) -> bytes:
    img = _decode(image_bytes)
    if img is None:
        return image_bytes
    out = _upscale(_enhance(img))
    ok, buf = cv2.imencode(".jpg", out, [cv2.IMWRITE_JPEG_QUALITY, 95])
    return buf.tobytes() if ok else image_bytes


def _otsu(img: np.ndarray) -> np.ndarray:
    gray = cv2.GaussianBlur(cv2.cvtColor(img, cv2.COLOR_BGR2GRAY), (5, 5), 0)
    _, mask = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
    return mask


def _detect_quads(img: np.ndarray) -> list[np.ndarray]:
    height, width = img.shape[:2]
    image_area = float(height * width)
    mask = _otsu(img)

    white_frac = float((mask > 0).mean())
    if not 0.12 < white_frac < 0.97:
        return []

    mask = cv2.morphologyEx(
        mask, cv2.MORPH_OPEN, cv2.getStructuringElement(cv2.MORPH_RECT, (5, 5))
    )
    mask = cv2.morphologyEx(
        mask, cv2.MORPH_CLOSE, cv2.getStructuringElement(cv2.MORPH_RECT, (7, 7))
    )

    quads = _boxes_from_mask(mask, image_area)
    if len(quads) < 2:
        eroded = cv2.erode(
            mask, cv2.getStructuringElement(cv2.MORPH_RECT, (17, 17)), iterations=2
        )
        quads = _boxes_from_mask(eroded, image_area, margin=34)
    return quads


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


def _deskew_sub(sub: np.ndarray) -> np.ndarray:
    """Endireita o papel dentro de um recorte já cortado (rotação pequena)."""
    if sub.size == 0:
        return sub
    sh, sw = sub.shape[:2]
    mask = _otsu(sub)
    if not 0.2 < float((mask > 0).mean()) < 0.99:
        return sub
    mask = cv2.morphologyEx(
        mask, cv2.MORPH_CLOSE, cv2.getStructuringElement(cv2.MORPH_RECT, (17, 17))
    )
    contours, _ = cv2.findContours(
        mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE
    )
    if not contours:
        return sub
    contour = max(contours, key=cv2.contourArea)
    if cv2.contourArea(contour) < 0.4 * sh * sw:
        return sub
    rect = cv2.minAreaRect(contour)
    (_, _), (rw, rh), angle = rect
    angle = angle if angle > -45 else angle + 90
    if abs(angle) > 12 or min(rw, rh) < 60:
        return sub
    warped = _warp(sub, _order_corners(cv2.boxPoints(rect).astype("float32")))
    return warped if warped.size else sub


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
