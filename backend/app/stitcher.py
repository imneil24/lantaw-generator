import subprocess


def build_concat_file(clip_paths: list[str], concat_file_path: str) -> str:
    with open(concat_file_path, "w") as f:
        for path in clip_paths:
            f.write(f"file '{path}'\n")
    return concat_file_path


def stitch_project(clip_paths: list[str], audio_path: str, output_path: str) -> None:
    concat_file_path = output_path + ".concat.txt"
    build_concat_file(clip_paths, concat_file_path)

    cmd = [
        "ffmpeg", "-y",
        "-f", "concat", "-safe", "0", "-i", concat_file_path,
        "-i", audio_path,
        "-c:v", "copy", "-c:a", "aac",
        "-shortest",
        output_path,
    ]
    result = subprocess.run(cmd, capture_output=True)
    if result.returncode != 0:
        raise RuntimeError(f"ffmpeg stitch failed: {result.stderr.decode(errors='replace')}")
