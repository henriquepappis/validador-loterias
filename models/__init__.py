"""Registro dos models para o metadata do SQLAlchemy."""
from models.batch import Batch
from models.draw import Draw
from models.game import Game
from models.image import Image
from models.ticket import Ticket

__all__ = ["Batch", "Image", "Ticket", "Game", "Draw"]
