import os
import uuid
import wave
from abc import ABC, abstractmethod


class TTSProvider(ABC):
    @abstractmethod
    def generate(self, text: str, target_duration: float) -> str:
        raise NotImplementedError


class NullTTSProvider(TTSProvider):
    def __init__(self, output_dir: str = "/tmp"):
        self._output_dir = output_dir

    def generate(self, text: str, target_duration: float) -> str:
        sample_rate = 44100
        n_frames = int(sample_rate * target_duration)
        path = os.path.join(self._output_dir, f"silence_{uuid.uuid4().hex}.wav")
        with wave.open(path, "wb") as wf:
            wf.setnchannels(1)
            wf.setsampwidth(2)
            wf.setframerate(sample_rate)
            wf.writeframes(b"\x00\x00" * n_frames)
        return path
