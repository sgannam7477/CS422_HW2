#!/usr/bin/env python3
"""Single entry point: pick servers -> iperf3 tests (every CC algorithm x repetitions) -> raw data -> PDFs + tables.

  python3 run_experiments.py --server-file servers.txt -n 10 -t 60 --interval 0.2 -r 1
  python3 run_experiments.py --out results --analyze-only      # redo plots/tables from saved data

Raw data in --out: samples.csv, selected_servers.json, environment.json.  Plots/tables: see analyze.py.
"""
import argparse
import csv
import json
import os
import random
import socket
import sys
import time
import urllib.request

import iperf_client as ic
from iperf_client import IperfError, log

SERVER_LIST_URL = "https://export.iperf3serverlist.net/listed_iperf3_servers.json"
PAUSE = 3.0         # seconds between tests
PORT_TRIES = 2      # ports tried per server (servers list port ranges)
BUSY_RETRIES = 2    # retries of a repeat run when the server says "busy"
BUSY_WAIT = 8.0

# (csv header, key, unit)
CSV_COLUMNS = [("run_id", "run_id", ""), ("host", "host", ""), ("port", "port", ""),
               ("repetition", "repetition", ""), ("algorithm", "algorithm", ""),
               ("t_s", "t", "s"), ("dt_s", "dt", "s"), ("acked_bytes", "acked", "bytes, cumulative"),
               ("goodput_bps", "goodput_bps", "bit/s"), ("cwnd_segs", "cwnd_segs", "MSS"),
               ("cwnd_bytes", "cwnd_bytes", "bytes"), ("mss", "mss", "bytes"),
               ("srtt_ms", "srtt_ms", "ms"), ("rttvar_ms", "rttvar_ms", "ms"),
               ("min_rtt_ms", "min_rtt_ms", "ms"), ("ssthresh_segs", "ssthresh_segs", "MSS"),
               ("retrans_total", "retrans_total", "packets, cumulative"),
               ("retrans_interval", "retrans_interval", "packets in this sampling interval"),
               ("lost", "lost", "packets, kernel's current lost estimate"),
               ("retrans_bytes", "retrans_bytes", "bytes, cumulative"),
               ("pacing_bps", "pacing_bps", "bit/s"), ("delivery_bps", "delivery_bps", "bit/s"),
               ("bytes_sent", "bytes_sent", "bytes, cumulative")]


def read_proc(path):
    try:
        with open(path) as f:
            return f.read().strip()
    except OSError:
        return None


def parse_server_file(path):
    """Lines 'host:port' or 'host:first-last', '#' starts a comment."""
    entries = []
    with open(path) as f:
        for line in f:
            line, _, comment = line.partition("#")
            line = line.strip()
            if line and ":" in line:
                host, port = line.rsplit(":", 1)
                entries.append({"host": host.strip("[]"), "port": port.strip(), "site": comment.strip()})
    return entries


def load_servers(cache_path):
    """Live list from iperf3serverlist.net (cached; the cache is used if the download fails)."""
    try:
        with urllib.request.urlopen(SERVER_LIST_URL, timeout=20) as r:
            raw = r.read()
        with open(cache_path, "wb") as f:
            f.write(raw)
    except Exception as e:
        log(f"could not fetch server list ({e}); using cache {cache_path}")
        with open(cache_path, "rb") as f:
            raw = f.read()
    return [{"host": s["IP/HOST"], "port": s["PORT"], "site": f"{s['SITE']} {s['COUNTRY']}"}
            for s in json.loads(raw)]


def expand_ports(spec):
    """'5201' | '9210-9240' | '5201,5202' -> list of ints."""
    ports = []
    for part in str(spec).replace(" ", "").split(","):
        try:
            a, _, b = part.partition("-")
            ports.extend(range(int(a), int(b or a) + 1))
        except ValueError:
            pass
    return ports or [5201]


def check_algorithms(requested):
    """Try each CC on a throw-away socket. Returns (usable, {algorithm: reason})."""
    ok, bad = [], {}
    for a in requested:
        s = socket.socket()
        try:
            s.setsockopt(socket.IPPROTO_TCP, socket.TCP_CONGESTION, a.encode())
            ok.append(a)
        except OSError as e:
            bad[a] = (f"{e}. Fix: `sudo modprobe tcp_bbr` on the host, then run the container with "
                      f"--cap-add NET_ADMIN (or add it to net.ipv4.tcp_allowed_congestion_control)")
        finally:
            s.close()
    return ok, bad


def measure(a):
    ic.require_linux()
    rng = random.Random(a.seed)
    algos, bad = check_algorithms([x for x in a.algorithms.split(",") if x])
    for k, v in bad.items():
        log(f"WARNING: algorithm '{k}' unavailable and will be SKIPPED: {v}")
    if not algos:
        sys.exit("none of the requested congestion-control algorithms is usable on this kernel")
    env = {"kernel": os.uname().release,
           "tcp_default_cc": read_proc("/proc/sys/net/ipv4/tcp_congestion_control"),
           "tcp_available_cc": read_proc("/proc/sys/net/ipv4/tcp_available_congestion_control"),
           "tcp_allowed_cc": read_proc("/proc/sys/net/ipv4/tcp_allowed_congestion_control"),
           "algorithms_used": algos, "algorithms_unavailable": bad, "args": vars(a)}
    log(f"kernel {env['kernel']}  default cc={env['tcp_default_cc']}  using {algos}")

    pool = parse_server_file(a.server_file) if a.server_file else \
        load_servers(os.path.join(a.out, "servers_cache.json"))
    rng.shuffle(pool)
    n, max_attempts = a.num_destinations, a.max_attempts or 3 * a.num_destinations

    runs, attempts, selected = [], [], []

    def probes():  # only server probes use the search budget; failed repeat runs (busy retries) don't
        return sum(1 for x in attempts if "cc" not in x)

    def record(entry, port, rep, res):
        runs.append({"run_id": f"{time.strftime('%Y%m%dT%H%M%S')}-{len(runs):03d}", "host": entry["host"],
                     "port": port, "repetition": rep, "algorithm": res["cc_actual"], **res})
        s = res["samples"]
        log(f"    OK  rep={rep} cc={res['cc_actual']}: mean {s[-1]['acked'] * 8 / s[-1]['t'] / 1e6:.1f} Mbit/s, "
            f"server received {res['server_bytes']}")

    try:
        for entry in pool:
            if len(selected) >= n or probes() >= max_attempts:
                break
            host, ports = entry["host"], expand_ports(entry["port"])
            rng.shuffle(ports)
            for port in ports[:PORT_TRIES]:
                log(f"[{len(selected)}/{n}] probing {host}:{port} ({entry['site']}) with cc={algos[0]}")
                att = {"host": host, "port": port, "site": entry["site"]}
                try:  # the first run (rep 0, first algorithm) doubles as the acceptance probe
                    res = ic.run_test(host, port, a.duration, algos[0], a.interval)
                except IperfError as e:
                    attempts.append({**att, "outcome": "failed", "kind": e.kind, "error": str(e)})
                    log(f"    FAILED: {e}")
                    if e.kind != "busy":
                        break          # unreachable / not iperf3: skip the rest of this host's ports
                    time.sleep(1)
                    continue
                attempts.append({**att, "outcome": "ok"})
                selected.append({"host": host, "port": port, "site": entry["site"]})
                record(entry, port, 0, res)
                for rep in range(a.repetitions):
                    # rotate the algorithm order each repetition to spread time-of-day effects
                    for algo in algos[rep % len(algos):] + algos[:rep % len(algos)]:
                        if rep == 0 and algo == algos[0]:
                            continue   # already done as the probe
                        for attempt in range(1 + BUSY_RETRIES):
                            time.sleep(PAUSE if attempt == 0 else BUSY_WAIT)
                            try:
                                record(entry, port, rep, ic.run_test(host, port, a.duration, algo, a.interval))
                                break
                            except IperfError as e:
                                attempts.append({**att, "cc": algo, "rep": rep, "outcome": "failed",
                                                 "kind": e.kind, "error": str(e)})
                                log(f"    FAILED rep={rep} cc={algo}: {e}")
                                if e.kind != "busy":
                                    break
                break
            time.sleep(PAUSE)
    except KeyboardInterrupt:
        log("interrupted; saving what we have")

    short = max(0, n - len(selected))
    with open(os.path.join(a.out, "selected_servers.json"), "w") as f:
        json.dump({"requested": n, "obtained": len(selected), "shortfall": short,
                   "selected": selected, "attempts": attempts}, f, indent=2)
    with open(os.path.join(a.out, "samples.csv"), "w", newline="") as f:
        w = csv.writer(f)
        w.writerow([h for h, _, _ in CSV_COLUMNS])
        for r in runs:
            for s in r["samples"]:
                row = {**s, **{k: r[k] for k in ("run_id", "host", "port", "repetition", "algorithm")}}
                w.writerow(["" if row.get(k) is None else (f"{row[k]:.4f}" if isinstance(row[k], float) else row[k])
                            for _, k, _ in CSV_COLUMNS])
    env.update(units={h: u for h, _, u in CSV_COLUMNS if u}, decoding=ic.tcp_stats.__doc__,
               interval_metrics="goodput_bps = 8*(acked_t - acked_{t-1})/(t - t_prev) with the measured elapsed time; "
                                "retrans_interval = retrans_total_t - retrans_total_{t-1} (loss proxy)")
    with open(os.path.join(a.out, "environment.json"), "w") as f:
        json.dump(env, f, indent=2)
    log(f"measurement done: {len(selected)}/{n} destinations, {len(runs)} runs"
        + (f"  (SHORTFALL of {short} after {len(attempts)} attempts)" if short else ""))


def main():
    ap = argparse.ArgumentParser(description="iperf3 TCP experiment pipeline",
                                 formatter_class=argparse.ArgumentDefaultsHelpFormatter)
    ap.add_argument("--server-file", help="host:port[-port] per line; omit to fetch the live iperf3serverlist.net list")
    ap.add_argument("-n", "--num-destinations", type=int, default=10)
    ap.add_argument("-t", "--duration", type=int, default=60, help="seconds per test")
    ap.add_argument("--interval", type=float, default=0.2, help="sampling interval (s)")
    ap.add_argument("-r", "--repetitions", type=int, default=1, help="repetitions per destination and algorithm")
    ap.add_argument("--algorithms", default="cubic,reno,bbr", help="comma list of congestion-control algorithms")
    ap.add_argument("--max-attempts", type=int, help="max servers tried (default 3*n)")
    ap.add_argument("--seed", type=int, help="seed for server/port selection")
    ap.add_argument("--out", default="results")
    ap.add_argument("--analyze-only", action="store_true", help="skip measurements, redo plots/tables")
    ap.add_argument("--representative", help="host:port used for the Q2/Q3 plots (default: auto)")
    a = ap.parse_args()
    os.makedirs(a.out, exist_ok=True)
    if not a.analyze_only:
        measure(a)
    import analyze
    analyze.run(a.out, a.representative)


if __name__ == "__main__":
    main()
