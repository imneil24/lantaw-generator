from abc import ABC, abstractmethod
from dataclasses import dataclass

DEFAULT_BLOCKLIST = [
    "child sexual", "csam", "bomb making", "how to build a bomb",
    "bioweapon", "chemical weapon synthesis",
]


@dataclass
class ModerationResult:
    allowed: bool
    reason: str | None = None


class ModerationProvider(ABC):
    @abstractmethod
    def check(self, prompt: str) -> ModerationResult:
        raise NotImplementedError


class KeywordModerationProvider(ModerationProvider):
    def __init__(self, blocklist: list[str] | None = None):
        self._blocklist = [b.lower() for b in (blocklist or DEFAULT_BLOCKLIST)]

    def check(self, prompt: str) -> ModerationResult:
        lowered = prompt.lower()
        for term in self._blocklist:
            if term in lowered:
                return ModerationResult(allowed=False, reason=f"blocked term: {term}")
        return ModerationResult(allowed=True)
