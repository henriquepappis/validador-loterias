"""Ponto de entrada do FastAPI (Validador de Loterias).

Fluxo:
  GET  /                     formulário de upload (1+ imagens)
  POST /upload               salva imagens, roda o OCR, cria um lote pendente
  GET  /revisar/{batch_id}   revisão/edição das leituras antes de apurar
  POST /revisar/{batch_id}   valida, persiste, busca resultados e apura
  GET  /historico            lotes confirmados, agrupados por imagem
"""
from __future__ import annotations

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

from config import settings
from database import get_db, init_db
from models.batch import Batch
from models.draw import Draw
from models.game import Game
from models.image import Image
from models.ticket import Ticket
from services import caixa_scraper, nim_vision, validator
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
    r"^image_(\d+)_ticket_([0-9a-z]+)_game_([0-9a-z]+)_(id|numbers)$"
)


def _parse_revisar_form(form) -> dict[int, dict[str, dict[str, Any]]]:
    """form flat -> {image_id: {ticket_key: {lottery, draw, games:{gk:{id,numbers}}}}}"""
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
            game = slot["games"].setdefault(m.group(3), {"id": "", "numbers": ""})
            game[m.group(4)] = value
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

    batch = Batch()
    db.add(batch)
    db.flush()

    for upload_file in images:
        suffix = Path(upload_file.filename or "").suffix.lower() or ".jpg"
        filename = f"{uuid.uuid4().hex}{suffix}"
        (STORAGE_DIR / filename).write_bytes(await upload_file.read())

        image = Image(batch_id=batch.id, filename=filename)
        db.add(image)
        db.flush()

        try:
            parsed_tickets = nim_vision.extract_tickets(STORAGE_DIR / filename)
        except Exception as exc:  # noqa: BLE001 - falha de OCR não aborta o lote
            logger.exception("OCR falhou para %s", filename)
            db.add(
                Ticket(
                    image_id=image.id,
                    status="pending",
                    notes=f"Leitura automática falhou ({exc}). Preencha manualmente.",
                )
            )
            continue

        for parsed in parsed_tickets:
            ticket = Ticket(
                image_id=image.id,
                status="pending",
                lottery_type=parsed["lottery_type"],
                draw_number=parsed["draw_number"],
                notes=_join_notes(parsed),
            )
            ticket.games = [
                Game(game_identifier=g["identifier"], numbers=g["numbers"])
                for g in parsed["games"]
            ]
            db.add(ticket)

    db.commit()
    return RedirectResponse(url=f"/revisar/{batch.id}", status_code=303)


def _join_notes(parsed: dict[str, Any]) -> str | None:
    notes = list(parsed["warnings"])
    for game in parsed["games"]:
        for w in game["warnings"]:
            notes.append(f"Aposta {game['identifier']}: {w}")
    return "; ".join(notes) or None


@app.get("/revisar/{batch_id}", response_class=HTMLResponse)
def revisar(request: Request, batch_id: int, db: Session = Depends(get_db)):
    batch = _load_batch(db, batch_id)
    if batch is None:
        return templates.TemplateResponse(
            request, "index.html", {"error": "Lote não encontrado."}, status_code=404
        )
    return templates.TemplateResponse(
        request,
        "revisar.html",
        {"batch": batch, "lottery_types": LOTTERY_TYPES, "errors": []},
    )


@app.post("/revisar/{batch_id}")
async def revisar_submit(
    request: Request, batch_id: int, db: Session = Depends(get_db)
):
    batch = _load_batch(db, batch_id)
    if batch is None:
        return templates.TemplateResponse(
            request, "index.html", {"error": "Lote não encontrado."}, status_code=404
        )

    parsed = _parse_revisar_form(await request.form())
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

            games: list[tuple[str, list[int]]] = []
            for _, gdata in tdata["games"].items():
                numbers = sorted(clean_numbers(gdata["numbers"]))
                identifier = gdata["id"].strip().upper()[:4]
                if numbers or identifier:
                    games.append((identifier, numbers))

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
            for idx, (identifier, numbers) in enumerate(games):
                identifier = identifier or chr(ord("A") + idx)
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
        batch = _load_batch(db, batch_id)
        return templates.TemplateResponse(
            request,
            "revisar.html",
            {"batch": batch, "lottery_types": LOTTERY_TYPES, "errors": errors},
            status_code=400,
        )

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
    return RedirectResponse(url=f"/historico#batch-{batch.id}", status_code=303)


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
        images_view = []
        for image in batch.images:
            tickets_view = []
            for ticket in image.tickets:
                if ticket.status != "confirmed":
                    continue
                draw = draws.get((ticket.lottery_type, ticket.draw_number))
                games_view = [
                    {
                        "game": game,
                        "result": (
                            validator.evaluate(
                                game.numbers, draw.drawn_numbers, ticket.lottery_type
                            )
                            if draw
                            else None
                        ),
                    }
                    for game in ticket.games
                ]
                tickets_view.append(
                    {"ticket": ticket, "draw": draw, "games": games_view}
                )
            if tickets_view:
                images_view.append({"image": image, "tickets": tickets_view})

        if images_view:
            confirmed_batches.append({"batch": batch, "images": images_view})
        if batch.status != "confirmed":
            pending_batches.append(batch)

    return templates.TemplateResponse(
        request,
        "historico.html",
        {"batches": confirmed_batches, "pending": pending_batches},
    )
