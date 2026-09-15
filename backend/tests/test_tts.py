import os
import wave
from app.tts import NullTTSProvider


def test_null_provider_returns_silent_wav_of_target_duration(tmp_path):
    provider = NullTTSProvider(output_dir=str(tmp_path))
    path = provider.generate(text="unused", target_duration=2.0)
    assert os.path.exists(path)
    with wave.open(path, "rb") as wf:
        frames = wf.getnframes()
        rate = wf.getframerate()
        duration = frames / float(rate)
    assert abs(duration - 2.0) < 0.05
