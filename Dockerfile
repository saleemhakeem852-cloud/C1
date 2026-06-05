FROM python:3.12-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
    chromium \
    chromium-driver \
    fonts-liberation \
    libnss3 \
    libatk-bridge2.0-0 \
    libatk1.0-0 \
    libgtk-3-0 \
    libdrm2 \
    libxkbcommon0 \
    libxcomposite1 \
    libxdamage1 \
    libxfixes3 \
    libxrandr2 \
    libgbm1 \
    libasound2 \
    && rm -rf /var/lib/apt/lists/*

RUN printf '#!/bin/sh\nexec /usr/bin/chromedriver --allowed-ips="" --allowed-origins="*" "$@"\n' \
    > /usr/local/bin/chromedriver \
    && chmod +x /usr/local/bin/chromedriver

WORKDIR /app

COPY requirements.txt .

# Force unset proxy vars at build time so Railway's HTTPS_PROXY doesn't break pip
RUN env -u HTTPS_PROXY -u HTTP_PROXY -u https_proxy -u http_proxy \
    pip install --no-cache-dir -r requirements.txt

COPY . .

ENV PYTHONUNBUFFERED=1

CMD ["python", "server.py"]