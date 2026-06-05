FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    DISPLAY=:99

RUN apt-get update && apt-get install -y --no-install-recommends \
    wget curl unzip gnupg ca-certificates \
    fonts-liberation libnss3 libatk-bridge2.0-0 libatk1.0-0 \
    libgtk-3-0 libdrm2 libxkbcommon0 libxcomposite1 libxdamage1 \
    libxfixes3 libxrandr2 libgbm1 libasound2 libx11-6 libxcb1 \
    libxext6 libxrender1 libxtst6 libpango-1.0-0 \
    && rm -rf /var/lib/apt/lists/*

# Install Chrome 124 + matching chromedriver 124 (stable, known-good versions)
RUN wget -q "https://storage.googleapis.com/chrome-for-testing-public/124.0.6367.91/linux64/chrome-linux64.zip" \
    && unzip -q chrome-linux64.zip -d /opt/ \
    && ln -sf /opt/chrome-linux64/chrome /usr/local/bin/google-chrome \
    && rm chrome-linux64.zip

RUN wget -q "https://storage.googleapis.com/chrome-for-testing-public/124.0.6367.91/linux64/chromedriver-linux64.zip" \
    && unzip -q chromedriver-linux64.zip -d /opt/ \
    && ln -sf /opt/chromedriver-linux64/chromedriver /usr/local/bin/chromedriver \
    && chmod +x /opt/chromedriver-linux64/chromedriver \
    && rm chromedriver-linux64.zip

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
EXPOSE 5000
CMD ["bash", "start.sh"]