FROM node:20-bookworm-slim
RUN apt-get update \
 && apt-get install -y --no-install-recommends python3 python3-requests ca-certificates git \
 && rm -rf /var/lib/apt/lists/* \
 && npm install -g wrangler@4 --no-audit --no-fund
WORKDIR /app
COPY bot.py /app/bot.py
ENV PYTHONUNBUFFERED=1
CMD ["python3", "/app/bot.py"]
