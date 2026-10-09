FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 TMPDIR=/state/tmp
RUN mkdir -p /state/tmp && TMPDIR=/state/tmp pip install --no-cache-dir jsonschema==4.23.0 && \
    chown -R 65534:65534 /state
WORKDIR /app
COPY bridge.py contract.json ./
USER 65534:65534
EXPOSE 8080
CMD ["python", "-B", "-u", "/app/bridge.py"]
