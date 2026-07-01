FROM python:3.12-alpine AS builder
WORKDIR /app
COPY event_processor/requirements.txt .
RUN pip install --no-cache-dir --user -r requirements.txt

FROM python:3.12-alpine
WORKDIR /app
COPY --from=builder /root/.local /root/.local
ENV PATH=/root/.local/bin:$PATH
COPY common/ common/
COPY event_processor/main.py .
CMD ["python", "main.py"]
