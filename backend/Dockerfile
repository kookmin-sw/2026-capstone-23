FROM python:3.12-slim-bookworm

WORKDIR /app

ARG PYTORCH_INDEX_URL=https://download.pytorch.org/whl/cu126
ARG INSTALL_TORCH=0
ARG INSTALL_OFFICE_TOOLS=1
ARG INSTALL_WKHTMLTOPDF=auto

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    HOME=/tmp \
    XDG_CACHE_HOME=/tmp/.cache \
    XDG_CONFIG_HOME=/tmp/.config \
    HF_HOME=/tmp/huggingface

RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates \
    fontconfig \
    poppler-utils \
    libmagic1 \
    libcairo2 \
    libpango-1.0-0 \
    libpangocairo-1.0-0 \
    libgdk-pixbuf-2.0-0 \
    libgl1 \
    libglib2.0-0 \
    libgomp1 \
    fonts-nanum \
    fonts-nanum-extra \
    fonts-noto-cjk \
    && if [ "$INSTALL_OFFICE_TOOLS" = "1" ]; then \
        apt-get install -y --no-install-recommends \
            libreoffice \
            libreoffice-writer; \
        if [ "$INSTALL_WKHTMLTOPDF" != "0" ] && apt-cache show wkhtmltopdf xfonts-75dpi xfonts-base >/dev/null 2>&1; then \
            apt-get install -y --no-install-recommends \
                wkhtmltopdf \
                xfonts-75dpi \
                xfonts-base; \
        else \
            echo "wkhtmltopdf is unavailable or disabled; xhtml2pdf/weasyprint fallback will be used."; \
        fi; \
    fi \
    && fc-cache -f \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt requirements-torch.txt requirements-qwen.txt ./
RUN apt-get update && apt-get install -y --no-install-recommends \
        build-essential \
        pkg-config \
        libcairo2-dev \
    && python -m pip install --upgrade pip setuptools wheel \
    && pip install -r requirements.txt -c requirements-torch.txt \
    && apt-get purge -y --auto-remove \
        build-essential \
        pkg-config \
        libcairo2-dev \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/*
RUN if [ "$INSTALL_TORCH" = "1" ]; then \
        pip install --index-url ${PYTORCH_INDEX_URL} -r requirements-torch.txt \
        && pip install -r requirements-qwen.txt -c requirements-torch.txt; \
    fi

COPY . .

RUN mkdir -p data/inputs data/outputs data/tmp \
    && addgroup --system app \
    && adduser --system --ingroup app --home /app --no-create-home app \
    && chown -R app:app /app/data

EXPOSE 8000

USER app

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD python -c "import json,sys,urllib.request; body=json.load(urllib.request.urlopen('http://127.0.0.1:8000/v1/health', timeout=3)); sys.exit(0 if body.get('success') and body.get('data', {}).get('status') == 'ok' else 1)"

CMD ["uvicorn", "api:app", "--host", "0.0.0.0", "--port", "8000", "--ws", "websockets"]
