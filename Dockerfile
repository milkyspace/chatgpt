FROM python:3.11-slim

RUN apt-get update && apt-get install -y --no-install-recommends build-essential libmariadb-dev ffmpeg git && rm -rf /var/lib/apt/lists/*

RUN pip3 install -U pip wheel setuptools==59.5.0
COPY ./requirements.txt /tmp/requirements.txt
RUN pip3 install -r /tmp/requirements.txt && pip3 install yookassa aiomysql

COPY . /code
WORKDIR /code

CMD ["python3", "bot.py"]
