"""altf — N named pty-backed virtual consoles for a single LLM agent.

See DESIGN.md for the authoritative spec.
"""

from .classify import InputStateClassifier, LLMClassifier
from .console import Console, ConsoleError, TermState
from .machine import Machine
from .spawner import DockerExecSpawner, LocalSpawner, Spawner

__version__ = "0.1.0"

__all__ = [
    "Console",
    "ConsoleError",
    "DockerExecSpawner",
    "InputStateClassifier",
    "LLMClassifier",
    "LocalSpawner",
    "Machine",
    "Spawner",
    "TermState",
    "__version__",
]
