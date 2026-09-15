from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any

#Classe astratta per l'implementazione di un parser esterno specifico

class DocumentParser(ABC):
    @abstractmethod
    def extract(self, path:Path) -> Any:
        raise NotImplementedError("Subclasses must implement the extract method.")