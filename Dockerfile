# syntax=docker/dockerfile:1
FROM python:3.9-slim-bullseye AS python

ENV PYTHONUNBUFFERED 1
WORKDIR /marsbots

COPY requirements.txt requirements.txt
COPY bot.py bot.py
COPY entrypoint.sh entrypoint.sh

RUN apt-get update
RUN apt-get install -y git libgl1-mesa-glx

RUN pip install -r requirements.txt

RUN chmod +x entrypoint.sh
RUN cp entrypoint.sh /tmp/entrypoint.sh

ENTRYPOINT ["/tmp/entrypoint.sh"]
