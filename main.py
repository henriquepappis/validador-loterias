"""Ponto de entrada do FastAPI (Validador de Loterias).

Fluxo:
  GET  /                     formulário de upload (1+ imagens)
  POST /upload               salva imagens, roda o OCR, cria um lote pendente
  GET  /revisar/{batch_id}   revisão/edição das leituras antes de apurar
  POST /revisar/{batch_id}   valida, persiste, busca resultados e apura
  GET  /historico            lotes confirmados, agrupados por imagem
"""
from __future__ import annotations

import json
import logging
import re
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import Depends, FastAPI, File, Request, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session, selectinload
from starlette.concurrency import run_in_threadpool

from config import settings
from database import get_db, init_db
from models.batch import Batch
from models.draw import Draw
from models.game import Game
from models.image import Image
from models.ticket import Ticket
from services import caixa_scraper, nim_vision, segmentation, validator
from services.lottery import (
    LOTTERY_TYPES,
    clean_numbers,
    normalize_lottery_type,
    validate_numbers,
)

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

BASE_DIR = Path(__file__).resolve().parent
STORAGE_DIR = BASE_DIR / settings.storage_dir
ALLOWED_CONTENT_TYPES = {"image/png", "image/jpeg", "image/jpg"}
ALLOWED_SUFFIXES = {".png", ".jpg", ".jpeg"}


@asynccontextmanager
async def lifespan(_: FastAPI):
    STORAGE_DIR.mkdir(parents=True, exist_ok=True)
    init_db()
    yield


app = FastAPI(title="Validador de Loterias", lifespan=lifespan)
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))

app.mount("/static", StaticFiles(directory=str(BASE_DIR / "static")), name="static")
app.mount("/storage", StaticFiles(directory=str(STORAGE_DIR)), name="storage")


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def _is_image(upload: UploadFile) -> bool:
    suffix = Path(upload.filename or "").suffix.lower()
    return upload.content_type in ALLOWED_CONTENT_TYPES or suffix in ALLOWED_SUFFIXES


def _load_batch(db: Session, batch_id: int) -> Batch | None:
    return (
        db.query(Batch)
        .options(
            selectinload(Batch.images)
            .selectinload(Image.tickets)
            .selectinload(Ticket.games)
        )
        .filter(Batch.id == batch_id)
        .one_or_none()
    )


_TICKET_FIELD_RE = re.compile(r"^image_(\d+)_ticket_([0-9a-z]+)_(lottery|draw)$")
_GAME_FIELD_RE = re.compile(
    r"^image_(\d+)_ticket_([0-9a-z]+)_game_([0-9a-z]+)_numbers$"
)


def _parse_revisar_form(form) -> dict[int, dict[str, dict[str, Any]]]:
    """form flat -> {image_id: {ticket_key: {lottery, draw, games:{gk: numbers_str}}}}

    A ordem de inserção em ``games`` reflete a ordem do formulário (= ordem das
    linhas na tela); o identificador da aposta (A, B, C...) é atribuído depois
    por posição.
    """
    data: dict[int, dict[str, dict[str, Any]]] = {}

    def ticket_slot(image_id: int, tkey: str) -> dict[str, Any]:
        return (
            data.setdefault(image_id, {})
            .setdefault(tkey, {"lottery": "", "draw": "", "games": {}})
        )

    for key, value in form.multi_items():
        m = _TICKET_FIELD_RE.match(key)
        if m:
            slot = ticket_slot(int(m.group(1)), m.group(2))
            slot["lottery" if m.group(3) == "lottery" else "draw"] = value
            continue
        m = _GAME_FIELD_RE.match(key)
        if m:
            slot = ticket_slot(int(m.group(1)), m.group(2))
            slot["games"][m.group(3)] = value
    return data


# --------------------------------------------------------------------------- #
# Rotas
# --------------------------------------------------------------------------- #
@app.get("/", response_class=HTMLResponse)
def index(request: Request):
    return templates.TemplateResponse(request, "index.html", {})


@app.post("/upload")
async def upload(
    request: Request,
    files: list[UploadFile] = File(...),
    db: Session = Depends(get_db),
):
    images = [f for f in files if _is_image(f)]
    if not images:
        return templates.TemplateResponse(
            request,
            "index.html",
            {"error": "Envie ao menos um arquivo PNG ou JPEG."},
            status_code=400,
        )

    # Lê os uploads no event loop; o trabalho bloqueante (gravação em disco,
    # chamada à NVIDIA NIM, ORM) roda em threadpool para não travar o servidor.
    payloads = [
        (Path(f.filename or "").suffix.lower() or ".jpg", await f.read())
        for f in images
    ]
    batch_id = await run_in_threadpool(_ingest_originals, db, payloads)
    return RedirectResponse(url=f"/recortes/{batch_id}", status_code=303)


_LETTERS = "ABCDEFGHIJKLMNOPQRSTUVWXYZ"
_MODALITY_ORDER = {"Mega-Sena": 0, "Quina": 1}


def _letter(index: int) -> str:
    return _LETTERS[index] if index < len(_LETTERS) else str(index + 1)


def _group_tickets_by_modality(batch: Batch) -> list[dict[str, Any]]:
    """Agrupa todos os bilhetes do lote por modalidade (Mega-Sena, Quina, ...)."""
    buckets: dict[str | None, list[Ticket]] = {}
    for image in batch.images:
        for ticket in image.tickets:
            buckets.setdefault(ticket.lottery_type, []).append(ticket)

    # Sempre mostra as duas modalidades conhecidas, mesmo vazias.
    for known in LOTTERY_TYPES:
        buckets.setdefault(known, [])

    ordered = sorted(
        buckets, key=lambda k: (_MODALITY_ORDER.get(k, 9), k or "zzz")
    )
    return [
        {
            "lottery_type": lt,
            "label": lt or "Não identificado",
            "tickets": buckets[lt],
        }
        for lt in ordered
    ]


def _save_crop(db: Session, batch_id: int, parent: Image, box, content: bytes):
    crop_bytes = segmentation.crop_box(content, tuple(box))
    filename = f"{uuid.uuid4().hex}.jpg"
    (STORAGE_DIR / filename).write_bytes(crop_bytes)
    crop = Image(
        batch_id=batch_id,
        filename=filename,
        kind="crop",
        parent_id=parent.id,
        accepted=True,
        box=[float(v) for v in box],
    )
    db.add(crop)
    return crop


def _ingest_originals(db: Session, payloads: list[tuple[str, bytes]]) -> int:
    """Etapa 1: salva as fotos e propõe os recortes (sem OCR ainda)."""
    batch = Batch(status="cropping")
    db.add(batch)
    db.flush()

    for suffix, content in payloads:
        filename = f"{uuid.uuid4().hex}{suffix or '.jpg'}"
        (STORAGE_DIR / filename).write_bytes(content)
        original = Image(
            batch_id=batch.id, filename=filename, kind="original", accepted=True
        )
        db.add(original)
        db.flush()

        boxes = segmentation.detect_boxes(content) or [(0.0, 0.0, 1.0, 1.0)]
        for box in boxes:
            _save_crop(db, batch.id, original, box, content)

    db.commit()
    return batch.id


def _apply_recortes(
    db: Session, batch_id: int, form: dict[str, str]
) -> tuple[str, Any]:
    """Etapa 2: regera os recortes a partir das caixas confirmadas e roda o OCR."""
    batch = _load_batch(db, batch_id)
    if batch is None:
        return ("not_found", None)

    originals = [im for im in batch.images if im.kind == "original"]
    for original in originals:
        raw = form.get(f"boxes_{original.id}", "")
        if form.get(f"whole_{original.id}"):
            rects = [(0.0, 0.0, 1.0, 1.0)]
        else:
            rects = _parse_boxes(raw)
        if not rects:
            continue  # mantém os recortes atuais deste original

        content = (STORAGE_DIR / original.filename).read_bytes()
        for crop in [im for im in batch.images if im.parent_id == original.id]:
            (STORAGE_DIR / crop.filename).unlink(missing_ok=True)
            db.delete(crop)
        db.flush()
        for box in rects:
            _save_crop(db, batch.id, original, box, content)

    db.flush()

    # Consulta direta: a coleção batch.images pode estar defasada após os deletes.
    crops = (
        db.query(Image)
        .filter(Image.batch_id == batch_id, Image.kind == "crop")
        .order_by(Image.id)
        .all()
    )
    for crop in crops:
        if crop.tickets:
            continue
        try:
            parsed = nim_vision.extract_tickets(STORAGE_DIR / crop.filename)
        except Exception as exc:  # noqa: BLE001
            logger.exception("OCR falhou para %s", crop.filename)
            db.add(
                Ticket(
                    image_id=crop.id,
                    status="pending",
                    notes=(
                        f"Leitura automática falhou ({type(exc).__name__}). "
                        "Preencha manualmente."
                    ),
                )
            )
            continue
        _persist_tickets(db, crop, parsed)

    batch.status = "pending"
    db.commit()
    return ("ok", batch.id)


def _parse_boxes(raw: str) -> list[tuple[float, float, float, float]]:
    try:
        items = json.loads(raw) if raw else []
    except (json.JSONDecodeError, TypeError):
        return []
    out: list[tuple[float, float, float, float]] = []
    for item in items if isinstance(items, list) else []:
        try:
            x = min(max(float(item["x"]), 0.0), 1.0)
            y = min(max(float(item["y"]), 0.0), 1.0)
            w = min(max(float(item["w"]), 0.02), 1.0 - x)
            h = min(max(float(item["h"]), 0.02), 1.0 - y)
        except (KeyError, TypeError, ValueError):
            continue
        out.append((x, y, w, h))
    return out


def _persist_tickets(
    db: Session, image: Image, parsed_tickets: list[dict[str, Any]]
) -> None:
    """Grava um comprovante por item lido; apostas re-letradas A, B, C..."""
    seen_tickets: set[tuple] = set()

    for parsed in parsed_tickets:
        lottery_type = parsed["lottery_type"]
        draw_number = parsed["draw_number"]

        seen_numbers: set[tuple[int, ...]] = set()
        unique: list[list[int]] = []
        duplicates = 0
        for game in parsed["games"]:
            numbers = sorted(game["numbers"])
            if not numbers:
                continue
            signature = tuple(numbers)
            if signature in seen_numbers:
                duplicates += 1
                continue
            seen_numbers.add(signature)
            unique.append(numbers)

        # Comprovante idêntico repetido pela IA: ignora.
        ticket_signature = (lottery_type, draw_number, frozenset(seen_numbers))
        if unique and ticket_signature in seen_tickets:
            continue
        seen_tickets.add(ticket_signature)

        notes: list[str] = list(parsed["warnings"])
        for idx, numbers in enumerate(unique):
            for warning in validate_numbers(numbers, lottery_type):
                notes.append(f"aposta {_letter(idx)}: {warning}")
        if duplicates:
            notes.append(
                f"{duplicates} aposta(s) idêntica(s) na leitura foram unificadas "
                "— confira se nenhum jogo faltou."
            )
        if not (lottery_type and draw_number is not None) and not notes:
            notes.append("Modalidade/concurso não reconhecidos — preencha.")

        ticket = Ticket(
            image_id=image.id,
            status="pending",
            lottery_type=lottery_type,
            draw_number=draw_number,
            notes="; ".join(notes) or None,
        )
        ticket.games = [
            Game(game_identifier=_letter(idx), numbers=numbers)
            for idx, numbers in enumerate(unique)
        ]
        db.add(ticket)


def _revisar_context(batch: Batch, errors: list[str]) -> dict[str, Any]:
    crops = [im for im in batch.images if im.kind == "crop"]
    anchor = crops[0].id if crops else (batch.images[0].id if batch.images else 0)
    return {
        "batch": batch,
        "groups": _group_tickets_by_modality(batch),
        "anchor_image_id": anchor,
        "lottery_types": LOTTERY_TYPES,
        "errors": errors,
    }


@app.get("/recortes/{batch_id}", response_class=HTMLResponse)
def recortes(request: Request, batch_id: int, db: Session = Depends(get_db)):
    batch = _load_batch(db, batch_id)
    if batch is None:
        return templates.TemplateResponse(
            request, "index.html", {"error": "Lote não encontrado."}, status_code=404
        )
    if batch.status != "cropping":
        return RedirectResponse(url=f"/revisar/{batch_id}", status_code=303)

    originals = [im for im in batch.images if im.kind == "original"]
    view = [
        {
            "original": original,
            "boxes": [
                c.box or [0.0, 0.0, 1.0, 1.0]
                for c in batch.images
                if c.parent_id == original.id
            ],
        }
        for original in originals
    ]
    return templates.TemplateResponse(
        request, "recortes.html", {"batch": batch, "view": view}
    )


@app.post("/recortes/{batch_id}")
async def recortes_submit(
    request: Request, batch_id: int, db: Session = Depends(get_db)
):
    form = dict((await request.form()).multi_items())
    outcome, data = await run_in_threadpool(
        _apply_recortes, db, batch_id, form
    )
    if outcome == "not_found":
        return templates.TemplateResponse(
            request, "index.html", {"error": "Lote não encontrado."}, status_code=404
        )
    return RedirectResponse(url=f"/revisar/{data}", status_code=303)


@app.get("/revisar/{batch_id}", response_class=HTMLResponse)
def revisar(request: Request, batch_id: int, db: Session = Depends(get_db)):
    batch = _load_batch(db, batch_id)
    if batch is None:
        return templates.TemplateResponse(
            request, "index.html", {"error": "Lote não encontrado."}, status_code=404
        )
    return templates.TemplateResponse(
        request, "revisar.html", _revisar_context(batch, [])
    )


@app.post("/revisar/{batch_id}")
async def revisar_submit(
    request: Request, batch_id: int, db: Session = Depends(get_db)
):
    parsed = _parse_revisar_form(await request.form())
    outcome, data = await run_in_threadpool(_apply_revisar, db, batch_id, parsed)

    if outcome == "not_found":
        return templates.TemplateResponse(
            request, "index.html", {"error": "Lote não encontrado."}, status_code=404
        )
    if outcome == "errors":
        batch = await run_in_threadpool(_load_batch, db, batch_id)
        return templates.TemplateResponse(
            request,
            "revisar.html",
            _revisar_context(batch, data),
            status_code=400,
        )
    return RedirectResponse(url=f"/historico#batch-{data}", status_code=303)


def _apply_revisar(
    db: Session, batch_id: int, parsed: dict[int, dict[str, dict[str, Any]]]
) -> tuple[str, Any]:
    batch = _load_batch(db, batch_id)
    if batch is None:
        return ("not_found", None)

    images_by_id = {img.id: img for img in batch.images}
    errors: list[str] = []

    for image_id, tickets in parsed.items():
        image = images_by_id.get(image_id)
        if image is None:
            continue
        existing = {str(t.id): t for t in image.tickets}
        kept: set[str] = set()

        for tkey, tdata in tickets.items():
            lottery_type = normalize_lottery_type(tdata["lottery"])
            draw_number = _first_int(tdata["draw"])

            # Ordem do formulário = ordem das linhas na tela; a letra da aposta
            # é atribuída por posição.
            games: list[list[int]] = []
            for raw_numbers in tdata["games"].values():
                numbers = sorted(clean_numbers(raw_numbers))
                if numbers:
                    games.append(numbers)

            # comprovante totalmente vazio: ignorar
            if lottery_type is None and not tdata["draw"].strip() and not games:
                continue

            ticket_errors: list[str] = []
            if lottery_type is None:
                ticket_errors.append("selecione a modalidade")
            if draw_number is None:
                ticket_errors.append("informe o concurso")
            if not games:
                ticket_errors.append("informe ao menos uma aposta")

            fixed_games: list[tuple[str, list[int]]] = []
            for idx, numbers in enumerate(games):
                identifier = _letter(idx)
                for w in validate_numbers(numbers, lottery_type):
                    ticket_errors.append(f"aposta {identifier}: {w}")
                fixed_games.append((identifier, numbers))

            ticket = existing.get(tkey) or Ticket(image_id=image.id)
            if ticket.id is None:
                db.add(ticket)
            ticket.lottery_type = lottery_type
            ticket.draw_number = draw_number
            ticket.notes = "; ".join(ticket_errors) or None
            ticket.status = "pending" if ticket_errors else "confirmed"
            ticket.games = [
                Game(game_identifier=i, numbers=n) for i, n in fixed_games
            ]
            db.flush()
            kept.add(str(ticket.id))

            if ticket_errors:
                errors.append(
                    f"Imagem #{image_id}: {', '.join(ticket_errors)}."
                )

        for tid, ticket in existing.items():
            if tid not in kept:
                db.delete(ticket)

    db.flush()

    if errors:
        db.commit()
        return ("errors", errors)

    # Busca os resultados oficiais (uma vez por concurso) e finaliza o lote.
    confirmed = [
        t for img in batch.images for t in img.tickets if t.status == "confirmed"
    ]
    for lottery_type, draw_number in {
        (t.lottery_type, t.draw_number) for t in confirmed
    }:
        try:
            caixa_scraper.fetch_draw(lottery_type, draw_number, db)
        except caixa_scraper.DrawNotFoundError as exc:
            logger.warning("Resultado indisponível: %s", exc)

    batch.status = "confirmed"
    db.commit()
    return ("ok", batch.id)


def _first_int(text: str) -> int | None:
    match = re.search(r"\d+", text or "")
    return int(match.group()) if match else None


@app.get("/historico", response_class=HTMLResponse)
def historico(request: Request, db: Session = Depends(get_db)):
    batches = (
        db.query(Batch)
        .options(
            selectinload(Batch.images)
            .selectinload(Image.tickets)
            .selectinload(Ticket.games)
        )
        .order_by(Batch.created_at.desc())
        .all()
    )
    draws = {(d.lottery_type, d.draw_number): d for d in db.query(Draw).all()}

    confirmed_batches: list[dict[str, Any]] = []
    pending_batches: list[Batch] = []

    for batch in batches:
        buckets: dict[str | None, list[dict[str, Any]]] = {}
        for image in batch.images:
            for ticket in image.tickets:
                if ticket.status != "confirmed":
                    continue
                draw = draws.get((ticket.lottery_type, ticket.draw_number))
                buckets.setdefault(ticket.lottery_type, []).append(
                    {
                        "ticket": ticket,
                        "draw": draw,
                        "games": [
                            {
                                "game": game,
                                "result": (
                                    validator.evaluate(
                                        game.numbers,
                                        draw.drawn_numbers,
                                        ticket.lottery_type,
                                    )
                                    if draw
                                    else None
                                ),
                            }
                            for game in ticket.games
                        ],
                    }
                )

        if buckets:
            groups = [
                {"label": lt or "Não identificado", "tickets": buckets[lt]}
                for lt in sorted(
                    buckets, key=lambda k: (_MODALITY_ORDER.get(k, 9), k or "zzz")
                )
            ]
            confirmed_batches.append({"batch": batch, "groups": groups})
        if batch.status != "confirmed":
            pending_batches.append(
                {
                    "batch": batch,
                    "url": (
                        f"/recortes/{batch.id}"
                        if batch.status == "cropping"
                        else f"/revisar/{batch.id}"
                    ),
                    "step": (
                        "confirmar recortes"
                        if batch.status == "cropping"
                        else "revisar leitura"
                    ),
                }
            )

    return templates.TemplateResponse(
        request,
        "historico.html",
        {"batches": confirmed_batches, "pending": pending_batches},
    )
