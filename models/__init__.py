"""Registro dos models para o metadata do SQLAlchemy."""
from models.draw import Draw
from models.game import Game
from models.ticket import Ticket

__all__ = ["Ticket", "Game", "Draw"]
