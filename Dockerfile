# netplot server - Docker image
# Pure stdlib Python; only system deps are the ping/traceroute tools the
# probe engines shell out to. Data persists in /data (mount a volume).
FROM python:3.11-slim

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        iputils-ping traceroute ca-certificates \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY netplot ./netplot
RUN mkdir -p /data

ENV PYTHONUNBUFFERED=1
EXPOSE 8000
VOLUME ["/data"]

# Bind 0.0.0.0 so agents on other hosts can dial in; DB lives on the volume.
CMD ["python3", "-m", "netplot", \
     "--db", "/data/netplot.db", \
     "--host", "0.0.0.0", \
     "--port", "8000"]
