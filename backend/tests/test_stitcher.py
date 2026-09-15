import os
from unittest.mock import patch, MagicMock
from app.stitcher import stitch_project, build_concat_file


def test_build_concat_file_lists_clips_in_order(tmp_path):
    clip_paths = [str(tmp_path / "clip0.mp4"), str(tmp_path / "clip1.mp4")]
    for p in clip_paths:
        open(p, "wb").close()
    concat_path = build_concat_file(clip_paths, str(tmp_path / "concat.txt"))
    content = open(concat_path).read()
    lines = [l for l in content.splitlines() if l.strip()]
    assert lines[0] == f"file '{clip_paths[0]}'"
    assert lines[1] == f"file '{clip_paths[1]}'"


@patch("app.stitcher.subprocess.run")
def test_stitch_project_invokes_ffmpeg_with_concat_and_audio(mock_run, tmp_path):
    mock_run.return_value = MagicMock(returncode=0, stderr=b"")
    clip_paths = [str(tmp_path / "clip0.mp4")]
    open(clip_paths[0], "wb").close()
    audio_path = str(tmp_path / "silence.wav")
    open(audio_path, "wb").close()
    output_path = str(tmp_path / "final.mp4")

    stitch_project(clip_paths, audio_path, output_path)

    mock_run.assert_called_once()
    call_args = mock_run.call_args[0][0]
    assert "ffmpeg" in call_args
    assert audio_path in call_args
    assert output_path in call_args


@patch("app.stitcher.subprocess.run")
def test_stitch_project_raises_on_ffmpeg_failure(mock_run, tmp_path):
    mock_run.return_value = MagicMock(returncode=1, stderr=b"ffmpeg exploded")
    clip_paths = [str(tmp_path / "clip0.mp4")]
    open(clip_paths[0], "wb").close()
    audio_path = str(tmp_path / "silence.wav")
    open(audio_path, "wb").close()
    try:
        stitch_project(clip_paths, audio_path, str(tmp_path / "final.mp4"))
        assert False, "expected RuntimeError"
    except RuntimeError as e:
        assert "ffmpeg" in str(e).lower()
