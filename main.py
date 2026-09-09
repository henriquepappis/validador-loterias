"""Ponto de entrada do FastAPI (Validador de Loterias)."""
from __future__ import annotations

import logging
import uuid
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Depends, FastAPI, File, Request, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session, selectinload

from config import settings
from database import get_db, init_db
from models.draw import Draw
from models.game import Game
from models.ticket import Ticket
from services import caixa_scraper, nim_vision, validator

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
# Rotas
# --------------------------------------------------------------------------- #
@app.get("/", response_class=HTMLResponse)
def index(request: Request):
    return templates.TemplateResponse(request, "index.html", {})


@app.post("/upload")
async def upload(
    request: Request,
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
):
    suffix = Path(file.filename or "").suffix.lower()
    if file.content_type not in ALLOWED_CONTENT_TYPES and suffix not in ALLOWED_SUFFIXES:
        return _render_error(
            request, "Envie um arquivo PNG ou JPEG.", status_code=400
        )

    # 1. Salva o arquivo com nome único.
    filename = f"{uuid.uuid4().hex}{suffix or '.jpg'}"
    dest = STORAGE_DIR / filename
    dest.write_bytes(await file.read())

    try:
        # 2. OCR inteligente via NVIDIA NIM.
        parsed = nim_vision.extract_ticket(dest)

        # 3. Persiste bilhete + jogos.
        ticket = Ticket(
            filename=filename,
            lottery_type=parsed["lottery_type"],
            draw_number=parsed["draw_number"],
        )
        ticket.games = [
            Game(game_identifier=g["identifier"], numbers=g["numbers"])
            for g in parsed["games"]
        ]
        db.add(ticket)
        db.commit()
        db.refresh(ticket)

        # 4. Busca o resultado oficial (best-effort; não bloqueia o cadastro).
        try:
            caixa_scraper.fetch_draw(
                ticket.lottery_type, ticket.draw_number, db
            )
        except caixa_scraper.DrawNotFoundError as exc:
            logger.warning("Resultado indisponível: %s", exc)

    except Exception as exc:  # noqa: BLE001
        dest.unlink(missing_ok=True)
        logger.exception("Falha ao processar bilhete")
        return _render_error(
            request, f"Não foi possível processar o bilhete: {exc}", status_code=500
        )

    return RedirectResponse(url=f"/historico#ticket-{ticket.id}", status_code=303)


@app.get("/historico", response_class=HTMLResponse)
def historico(request: Request, db: Session = Depends(get_db)):
    tickets = (
        db.query(Ticket)
        .options(selectinload(Ticket.games))
        .order_by(Ticket.created_at.desc())
        .all()
    )

    draws = {
        (d.lottery_type, d.draw_number): d for d in db.query(Draw).all()
    }

    rows = []
    for ticket in tickets:
        draw = draws.get((ticket.lottery_type, ticket.draw_number))
        games = []
        for game in ticket.games:
            result = (
                validator.evaluate(
                    game.numbers, draw.drawn_numbers, ticket.lottery_type
                )
                if draw
                else None
            )
            games.append({"game": game, "result": result})
        rows.append({"ticket": ticket, "draw": draw, "games": games})

    return templates.TemplateResponse(
        request, "historico.html", {"rows": rows}
    )


def _render_error(request: Request, message: str, status_code: int = 400):
    return templates.TemplateResponse(
        request, "index.html", {"error": message}, status_code=status_code
    )
