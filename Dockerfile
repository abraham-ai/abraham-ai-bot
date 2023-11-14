# syntax=docker/dockerfile:1
FROM python:3.12-slim-bullseye AS python

ENV PYTHONUNBUFFERED 1
WORKDIR /marsbots

COPY bots bots
COPY requirements.txt requirements.txt
COPY bot.py bot.py
COPY entrypoint.sh entrypoint.sh

RUN apt-get update \
    && apt-get install -y git libgl1-mesa-glx ffmpeg \
    && apt-get upgrade -y libwebpdemux2 \
    && pip install -r requirements.txt \
    && chmod +x entrypoint.sh \
    && cp entrypoint.sh /tmp/entrypoint.sh

ENTRYPOINT ["/tmp/entrypoint.sh"]
