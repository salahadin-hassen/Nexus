FROM python:3.11-slim

# Security: non-root user
RUN adduser --disabled-password --gecos "" nexus
WORKDIR /app

# Install deps first (layer caching)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy source
COPY main.py .

# Run as non-root
USER nexus

# PORT is injected by the platform (Railway / Render / Fly)
ENV PORT=8000
EXPOSE $PORT

CMD uvicorn main:app --host 0.0.0.0 --port $PORT
