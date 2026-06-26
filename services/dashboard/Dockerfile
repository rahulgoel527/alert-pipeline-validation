FROM python:3.12-alpine AS builder
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir --user -r requirements.txt

FROM python:3.12-alpine
WORKDIR /app
COPY --from=builder /root/.local /root/.local
ENV PATH=/root/.local/bin:$PATH
COPY main.py .
COPY templates/ templates/
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8050"]
