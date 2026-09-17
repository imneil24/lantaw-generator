FROM nvidia/cuda:12.4.1-runtime-ubuntu22.04

RUN apt-get update && apt-get install -y --no-install-recommends python3.11 python3-pip && rm -rf /var/lib/apt/lists/*

WORKDIR /worker
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt torch --extra-index-url https://download.pytorch.org/whl/cu124

COPY handler.py .

# Model weights are NOT baked into this image — they load from the
# RunPod Network Volume mounted at /runpod-volume at container start.

CMD ["python3", "-u", "-c", "import runpod, handler; runpod.serverless.start({'handler': handler.handler})"]
