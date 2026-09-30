FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY gateway.py ./
USER 10001
CMD ["uvicorn", "gateway:app", "--host", "0.0.0.0", "--port", "8080"]