# Demo image: builds the synthetic warehouse and runs the 4-beat CLI demo.
FROM python:3.11-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
ENV PYTHONPATH=/app/src
CMD ["python", "demo/demo.py"]
