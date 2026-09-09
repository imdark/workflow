from abc import ABC, abstractmethod

class AIProvider(ABC):
    @abstractmethod
    def run(self, context: str, issue=None) -> str:
        pass
