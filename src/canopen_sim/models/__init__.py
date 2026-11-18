"""Behaviour models: own the profile-area objects (0x6000+) of a simulated device."""
from .base import Model
from .encoder import EncoderModel
from .io import IoModel

__all__ = ["Model", "IoModel", "EncoderModel"]
