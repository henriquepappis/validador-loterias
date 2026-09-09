"""Recorte automático de cada bilhete numa foto com vários comprovantes.

`split_tickets` recebe os bytes de uma imagem e devolve uma lista de bytes JPEG,
um por bilhete detectado (já com correção de perspectiva e ampliado se pequeno).
Quando não consegue separar com confiança, devolve ``[imagem_original]`` — o
pipeline então processa a foto inteira como antes.
"""
from __future__ import annotations

import logging

import cv2
import numpy as np

from config import settings

logger = logging.getLogger(__name__)

_MAX_TICKETS = 16
_MAX_ASPECT = 14.0  # acima disso é borda/risco, não um bilhete


def split_tickets(image_bytes: bytes) -> list[bytes]:
    if not settings.segmentation_enabled:
        return [image_bytes]
    try:
        crops = _segment(image_bytes)
    except Exception:  # noqa: BLE001 - segmentação é best-effort
        logger.exception("Falha na segmentação; usando a imagem inteira")
        return [image_bytes]
    return crops or [image_bytes]


def _decode(image_bytes: bytes) -> np.ndarray | None:
    arr = np.frombuffer(image_bytes, np.uint8)
    return cv2.imdecode(arr, cv2.IMREAD_COLOR)


def _segment(image_bytes: bytes) -> list[bytes]:
    img = _decode(image_bytes)
    if img is None:
        return []

    height, width = img.shape[:2]
    image_area = height * width
    gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    gray = cv2.GaussianBlur(gray, (5, 5), 0)

    # Bilhetes são papel claro sobre um fundo mais escuro: Otsu separa bem.
    _, mask = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
    # Fecha buracos (texto/códigos internos) para cada bilhete virar um blob.
    kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (25, 25))
    mask = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, kernel, iterations=2)

    contours, _ = cv2.findContours(
        mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE
    )

    rects: list[tuple] = []
    for contour in contours:
        if cv2.contourArea(contour) < image_area * settings.segmentation_min_area_frac:
            continue
        rect = cv2.minAreaRect(contour)
        (_, _), (rw, rh), _ = rect
        if rw < 60 or rh < 60:
            continue
        long_side, short_side = max(rw, rh), max(1.0, min(rw, rh))
        if long_side / short_side > _MAX_ASPECT:
            continue
        # Um único blob cobrindo quase tudo = não separou nada.
        if rw * rh > image_area * 0.92:
            continue
        rects.append(rect)

    if len(rects) < 2:
        return []

    # Ordena em leitura natural: linhas de cima para baixo, depois esq->dir.
    row_height = max(1.0, height / (len(rects) ** 0.5 + 1))
    rects.sort(key=lambda r: (round(r[0][1] / row_height), r[0][0]))

    crops: list[bytes] = []
    for rect in rects[:_MAX_TICKETS]:
        crop = _deskew(img, rect)
        crop = _upscale(crop)
        ok, buf = cv2.imencode(".jpg", crop, [cv2.IMWRITE_JPEG_QUALITY, 92])
        if ok:
            crops.append(buf.tobytes())
    return crops


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


def _deskew(img: np.ndarray, rect: tuple) -> np.ndarray:
    box = _order_corners(cv2.boxPoints(rect).astype("float32"))
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
    warped = cv2.warpPerspective(
        img, cv2.getPerspectiveTransform(box, dst), (out_w, out_h)
    )
    # Bilhete de loteria é mais largo que alto; corrige orientação retrato.
    if out_h > out_w * 1.3:
        warped = cv2.rotate(warped, cv2.ROTATE_90_CLOCKWISE)
    return warped


def _upscale(crop: np.ndarray) -> np.ndarray:
    target = settings.segmentation_upscale_to
    longest = max(crop.shape[:2])
    if longest >= target:
        return crop
    scale = target / longest
    return cv2.resize(
        crop, None, fx=scale, fy=scale, interpolation=cv2.INTER_CUBIC
    )
