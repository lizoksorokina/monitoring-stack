FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY bot.py ./

RUN useradd --create-home --shell /usr/sbin/nologin alertbot \
 && chown -R alertbot:alertbot /app
USER alertbot

EXPOSE 8080

CMD ["python", "-u", "bot.py"]
