FROM python:3.12-slim AS builder

WORKDIR /build

COPY pyproject.toml setup.cfg* setup.py* ./
COPY src/ src/

RUN pip install --no-cache-dir --prefix=/install .

FROM python:3.12-slim

WORKDIR /app

COPY --from=builder /install /usr/local
COPY src/ src/

EXPOSE 8080

CMD ["python", "-m", "remote_control.main", "-c", "/app/config.yaml"]
