FROM python:3.11-slim

RUN apt-get update && apt-get install -y --no-install-recommends chromium chromium-driver \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY pyproject.toml requirements.txt ./
COPY healtest ./healtest
COPY config ./config
COPY suites ./suites
RUN pip install --no-cache-dir .

ENV PYTHONUNBUFFERED=1
# Pass the key at run time:  docker run -e OPENAI_API_KEY=... -v "$PWD/storage:/app/storage" healtest run --suite suites/saucedemo_suite.xlsx --app saucedemo --headless
ENTRYPOINT ["healtest"]
CMD ["--help"]
