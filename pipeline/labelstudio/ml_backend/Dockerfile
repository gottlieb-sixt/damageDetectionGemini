FROM python:3.12-slim

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY backend.py wsgi.py ./

EXPOSE 9090
CMD ["sh", "-c", "gunicorn -w 1 -k uvicorn.workers.UvicornWorker -b 0.0.0.0:${PORT:-9090} wsgi:app --timeout 300"]
