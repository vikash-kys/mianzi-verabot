FROM python:3.12-slim

# Hugging Face Spaces runs containers as user 1000; works fine on Render/Fly/Railway too
RUN useradd -m -u 1000 user
WORKDIR /home/user/app
ENV PYTHONUNBUFFERED=1 PYTHONUTF8=1 PORT=7860 PATH=/home/user/.local/bin:$PATH

COPY --chown=user requirements.txt .
RUN pip install --no-cache-dir fastapi "uvicorn[standard]" pydantic anthropic
COPY --chown=user bot.py conversation_handlers.py ./
COPY --chown=user vera ./vera
USER user

EXPOSE 7860
# single worker on purpose: context + conversation state lives in process memory
CMD ["sh", "-c", "uvicorn bot:app --host 0.0.0.0 --port ${PORT} --workers 1"]
