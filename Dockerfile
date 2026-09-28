FROM python:3.12-alpine

ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
WORKDIR /app

# tzdata so TZ=America/Los_Angeles etc. works for log timestamps and the backup window.
RUN apk add --no-cache tzdata
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .
CMD ["python", "valheim_discord_monitor.py", "--config", "config.json"]
