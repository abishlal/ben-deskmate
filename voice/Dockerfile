FROM python:3.12-slim

ARG STT_MODEL=small.en
ARG TTS_VOICE=en_US-amy-medium
ARG WAKE_MODEL=hey_jarvis
ENV PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1 PYTHONDONTWRITEBYTECODE=1 \
    STT_MODEL=${STT_MODEL} TTS_VOICE=${TTS_VOICE} WAKE_MODEL=${WAKE_MODEL} VOICE_MODEL_DIR=/models

WORKDIR /srv
RUN pip install "faster-whisper>=1.1" "piper-tts>=1.3" fastapi "uvicorn[standard]"
# openWakeWord: installed without its declared dependencies because it asks for tflite-runtime, which has no
# build for this Python. It runs on onnxruntime, which faster-whisper already brought in.
RUN pip install --no-deps "openwakeword==0.6.0" && pip install scipy scikit-learn tqdm requests
# The wake-word model and its two feature models are downloaded once, at build time.
RUN python -c "import os, openwakeword.utils as u; u.download_models(model_names=[os.environ['WAKE_MODEL']])" \
 && python -c "import os; from openwakeword.model import Model; Model(wakeword_models=[os.environ['WAKE_MODEL']], inference_framework='onnx'); print('wake model ok')"

# Models are downloaded once, at build time, so the running container needs no internet access.
RUN mkdir -p /models/piper \
 && python -c "import os; from faster_whisper import WhisperModel; WhisperModel(os.environ['STT_MODEL'], device='cpu', compute_type='int8', download_root='/models/whisper')" \
 && python -m piper.download_voices "${TTS_VOICE}" --data-dir /models/piper

COPY app.py wake_check.py ./
RUN useradd -m pet
USER pet
ENV HF_HUB_OFFLINE=1
EXPOSE 8766
HEALTHCHECK --interval=15s --timeout=5s --start-period=60s --retries=5 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8766/health')"
CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8766"]
