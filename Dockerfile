FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

COPY requirements.txt ./
RUN pip install --no-cache-dir --requirement requirements.txt \
    && apt-get update \
    && apt-get install -y --no-install-recommends gosu \
    && rm -rf /var/lib/apt/lists/* \
    && addgroup --system bot \
    && adduser --system --ingroup bot bot \
    && mkdir -p /data \
    && chown bot:bot /data

COPY --chown=bot:bot bot.py markov.py invites.py userbot.py greetings.txt op_admins.json ./
COPY entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

# Контейнер стартует от root, чтобы entrypoint.sh мог поправить владельца
# смонтированного volume (см. entrypoint.sh) — сам процесс бота всё равно
# исполняется от bot, entrypoint передаёт управление через gosu.
ENTRYPOINT ["/entrypoint.sh"]
CMD ["python", "bot.py"]
