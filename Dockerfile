FROM ubuntu:24.04

ENV DEBIAN_FRONTEND=noninteractive
RUN apt-get update \
 && apt-get install -y --no-install-recommends python3 python3-venv ca-certificates \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN python3 -m venv /opt/venv && /opt/venv/bin/pip install --no-cache-dir -r requirements.txt
ENV PATH=/opt/venv/bin:$PATH MPLBACKEND=Agg PYTHONUNBUFFERED=1

COPY iperf_client.py run_experiments.py analyze.py servers.txt ./

# Results are written to /app/results; mount a host directory there to keep them.
VOLUME /app/results
ENTRYPOINT ["python3", "run_experiments.py"]
# Default = full pipeline with the bundled server list; override by passing options after the image name.
CMD ["--server-file", "servers.txt", "-n", "10", "-t", "60", "--interval", "0.2", "-r", "1"]
