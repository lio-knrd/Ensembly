# Single-image build: compile the frontend, then run the FastAPI backend
# (which serves the built frontend). FFmpeg is included for the render stage.
FROM node:20-slim AS frontend
WORKDIR /app/frontend
COPY frontend/package*.json ./
RUN npm install
COPY frontend/ ./
RUN npm run build

FROM python:3.12-slim
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt
COPY backend/ ./backend/
COPY run.py ./
COPY --from=frontend /app/frontend/dist ./frontend/dist

EXPOSE 8420
CMD ["uvicorn", "backend.main:app", "--host", "0.0.0.0", "--port", "8420"]
