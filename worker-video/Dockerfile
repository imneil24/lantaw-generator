FROM nvidia/cuda:13.2.1-cudnn-runtime-ubuntu22.04

# gcc + python3.11-dev are required at runtime, not just build time: triton
# (pulled in by natten's dependency chain) JIT-compiles CUDA kernels on first
# use, invoking a C compiler from inside the running container and linking
# against Python.h — neither ships in the plain -runtime base image.
RUN apt-get update && apt-get install -y --no-install-recommends python3.11 python3.11-dev python3-pip git curl ca-certificates gcc && rm -rf /var/lib/apt/lists/*

# uv manages the LTX-2 workspace's own dependency resolution (torch 2.13/cu132,
# natten's flat wheel index, the ltx-core/ltx-pipelines workspace members) —
# pip alone cannot reproduce this, see packages/ltx-core/pyproject.toml's
# [tool.uv.sources] index pins.
RUN curl -LsSf https://astral.sh/uv/install.sh | sh
ENV PATH="/root/.local/bin:${PATH}"

WORKDIR /worker

# Model weights are NOT baked into this image — they load from the
# RunPod Network Volume mounted at /runpod-volume at container start.
RUN git clone --depth 1 https://github.com/Lightricks/LTX-2.git ltx2-repo
WORKDIR /worker/ltx2-repo
RUN uv sync --extra natten

COPY requirements.txt /worker/requirements.txt
RUN uv pip install --no-cache-dir -r /worker/requirements.txt

COPY handler.py /worker/handler.py

# triton's runtime JIT-compile links against libcuda.so.1 (the driver stub),
# which RunPod's container runtime mounts in from the host driver rather than
# baking into the image — but the -runtime base image never runs ldconfig
# against those mounted paths, so gcc's default linker search misses it even
# though the file exists on disk. Refreshing the cache at container start
# (not build time, when the GPU/driver mount isn't present yet) fixes this.
CMD ["/bin/sh", "-c", "ldconfig; exec /worker/ltx2-repo/.venv/bin/python3 -u -c \"import sys; sys.path.insert(0, '/worker'); import runpod, handler; runpod.serverless.start({'handler': handler.handler})\""]
