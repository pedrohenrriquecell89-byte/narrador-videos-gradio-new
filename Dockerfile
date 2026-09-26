FROM python:3.11-slim

# System deps: ffmpeg for rendering, curl/build-essential for TTS/alignment deps
RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg curl build-essential \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Download the Piper voice model at BUILD time (baked into the image,
# so first request doesn't pay a slow download on top of the cold start).
RUN mkdir -p /app/models && \
    ( python -m piper.download_voices en_US-hfc_male-medium --download-dir /app/models || \
      ( curl -L -o /app/models/en_US-hfc_male-medium.onnx \
          https://huggingface.co/rhasspy/piper-voices/resolve/main/en/en_US/hfc_male/medium/en_US-hfc_male-medium.onnx && \
        curl -L -o /app/models/en_US-hfc_male-medium.onnx.json \
          https://huggingface.co/rhasspy/piper-voices/resolve/main/en/en_US/hfc_male/medium/en_US-hfc_male-medium.onnx.json ) )

COPY . .

ENV PORT=10000
EXPOSE 10000

CMD ["sh", "-c", "uvicorn app:app --host 0.0.0.0 --port ${PORT}"]
