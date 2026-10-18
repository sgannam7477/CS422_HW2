#!/usr/bin/env bash
# Build the image and run the full pipeline on a Linux host (Ubuntu 22.04+) or WSL2.
#   ./run_docker.sh                       # defaults: 10 destinations, 60 s, 0.2 s interval, 1 repetition
#   ./run_docker.sh --server-file servers.txt -n 5 -t 30 -r 2 --seed 1      # any run_experiments.py options
# Env: IMAGE (default cs422-iperf), OUT (default ./results)
set -euo pipefail
IMAGE=${IMAGE:-cs422-iperf}
OUT=${OUT:-$PWD/results}

# On a native Linux/WSL2 host the container shares this kernel, so BBR must be loaded here.
# (On other hosts Docker runs the container in its own Linux VM; nothing to prepare.)
if [ -r /proc/sys/net/ipv4/tcp_available_congestion_control ]; then
  echo "host kernel: $(uname -r)"
  echo "available CC: $(cat /proc/sys/net/ipv4/tcp_available_congestion_control)"
  if ! grep -qw bbr /proc/sys/net/ipv4/tcp_available_congestion_control; then
    echo "bbr not loaded; trying 'modprobe tcp_bbr' (needs sudo)"
    sudo modprobe tcp_bbr || echo "WARNING: could not load tcp_bbr; bbr runs will be skipped"
  fi
fi

docker build -t "$IMAGE" .
mkdir -p "$OUT"
# NET_ADMIN lets the (root) container select cubic/reno/bbr without editing tcp_allowed_congestion_control.
docker run --rm --cap-add NET_ADMIN -v "$OUT:/app/results" "$IMAGE" "$@"

# files written by the container may be owned by root (native Linux)
[ -w "$OUT/samples.csv" ] || sudo chown -R "$(id -u):$(id -g)" "$OUT"
echo "results in $OUT"
