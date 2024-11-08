FROM python:3.13-slim
WORKDIR /app

COPY requirements.txt ./
RUN apt-get update \
	&& apt-get install -y --no-install-recommends \
		librtmidi6 build-essential pkg-config libasound-dev \
	&& pip install --no-cache-dir -r requirements.txt \
	&& apt-get autoremove --purge -y \
		build-essential pkg-config libasound-dev \
	&& apt-get clean \
	&& rm -rf /root/.cache

COPY control-panel.py ./
ENTRYPOINT [ "./control-panel.py" ]
