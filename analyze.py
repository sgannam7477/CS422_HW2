#!/usr/bin/env python3
"""Plots (PDF) and summary tables from samples.csv.   usage: analyze.py [results_dir] [host:port]

Outputs in results_dir:
  goodput_timeseries.pdf             Q1: goodput vs time per destination (legend: median / mean / p95)
  summary_by_destination.csv/.md     Q1: min / median / mean / p95 per destination and algorithm
  tcpstats_timeseries_<alg>.pdf      Q2/Q3: cwnd, RTT, loss proxy, goodput vs time, per algorithm
  scatter_relationships_<alg>.pdf    Q2/Q3: cwnd / RTT / loss vs goodput, per algorithm (same axes for all)
  compare_timeseries.pdf             Q3: the four time series, one column per algorithm, shared y-axes
  compare_repetitions.pdf            Q3: run-to-run variability on the representative destination
  compare_regimes.pdf                Q3: goodput / queueing / utilisation vs bandwidth-delay product
  compare_runs.csv, compare_by_destination.csv/.md, compare_overall.csv/.md
"""
import csv
import json
import math
import os
import sys
from collections import defaultdict

import matplotlib
import matplotlib.ticker
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np

COLORS = {"cubic": "#2a6fbb", "reno": "#d9731a", "bbr": "#2f9e55"}
NUMERIC = ["t_s", "dt_s", "acked_bytes", "goodput_bps", "cwnd_segs", "cwnd_bytes", "mss", "srtt_ms",
           "rttvar_ms", "min_rtt_ms", "ssthresh_segs", "retrans_total", "retrans_interval",
           "lost", "retrans_bytes", "pacing_bps", "delivery_bps", "bytes_sent"]


def color(alg):
    for k, c in COLORS.items():
        if alg.startswith(k):
            return c
    return "#555555"


def load(out):
    """-> dict run_id -> {meta..., col: np.array}"""
    runs = {}
    with open(os.path.join(out, "samples.csv")) as f:
        for row in csv.DictReader(f):
            r = runs.setdefault(row["run_id"], {"host": row["host"], "port": row["port"],
                                                "algorithm": row["algorithm"],
                                                "repetition": int(row["repetition"]), "rows": []})
            r["rows"].append(row)
    for r in runs.values():
        for c in NUMERIC:
            r[c] = np.array([float(x[c]) if x.get(c, "") != "" else np.nan for x in r["rows"]])
        del r["rows"]
    return runs


def stats(g):
    return {"n": len(g), "min": g.min(), "median": np.median(g), "mean": g.mean(),
            "p95": np.percentile(g, 95)}


def dest(r):
    return f"{r['host']}:{r['port']}"


def tables(runs, out):
    """Q1 summary: min / median / mean / p95 of the 0.2 s goodput samples (Mbit/s), pooled over repetitions."""
    pooled = defaultdict(list)
    for r in runs.values():
        pooled[(dest(r), r["algorithm"])].append(r["goodput_bps"] / 1e6)
    rows = []
    for (d, alg), gs in sorted(pooled.items(), key=lambda kv: (kv[0][0], alg_key(kv[0][1]))):
        s = stats(np.concatenate(gs))
        rows.append([d, alg, len(gs), s["n"]] + [f"{s[k]:.2f}" for k in ("min", "median", "mean", "p95")])
    write_table(os.path.join(out, "summary_by_destination"),
                ["destination", "algorithm", "runs", "samples", "min_mbps", "median_mbps", "mean_mbps", "p95_mbps"],
                rows, "\nThroughput in Mbit/s from acknowledged-byte deltas per sampling interval, "
                      "pooled over repetitions.\n")
    print(open(os.path.join(out, "summary_by_destination.md")).read())


def goodput_plot(runs, out):
    dests = sorted({dest(r) for r in runs.values()})
    cols = 2 if len(dests) > 1 else 1
    nrows = math.ceil(len(dests) / cols)
    fig, axes = plt.subplots(nrows, cols, figsize=(7 * cols, 2.7 * nrows), squeeze=False)
    for ax, d in zip(axes.flat, dests):
        by_alg = defaultdict(list)
        for x in runs.values():
            if dest(x) == d:
                by_alg[x["algorithm"]].append(x)
        for alg in sorted(by_alg, key=alg_key):
            rs = sorted(by_alg[alg], key=lambda x: x["repetition"])
            st = stats(np.concatenate([x["goodput_bps"] / 1e6 for x in rs]))   # statistics pool all repetitions
            ax.plot(rs[0]["t_s"], rs[0]["goodput_bps"] / 1e6, lw=0.9, color=color(alg),  # one line: first repetition
                    label=f"{alg}: {st['median']:.1f} / {st['mean']:.1f} / {st['p95']:.1f}")
        ax.set_title(d, fontsize=9)
        ax.set_ylabel("Mbit/s")
        ax.set_ylim(bottom=0)
        ax.grid(alpha=0.3)
        ax.legend(fontsize=7, loc="upper right", title="median / mean / p95, all runs (Mbit/s)", title_fontsize=7)
    for ax in axes.flat[len(dests):]:
        ax.axis("off")
    for ax in axes[-1]:
        ax.set_xlabel("time (s)")
    fig.suptitle("Acknowledged-byte goodput over time")
    fig.tight_layout()
    fig.savefig(os.path.join(out, "goodput_timeseries.pdf"))
    plt.close(fig)


ALG_ORDER = ["cubic", "reno", "bbr"]


def alg_key(a):
    for i, k in enumerate(ALG_ORDER):
        if a.startswith(k):
            return i
    return 99


def algs_in(runs):
    return sorted({r["algorithm"] for r in runs.values()}, key=alg_key)


def run_seq(rid):
    """Execution order of a run: ids look like <timestamp>-<index>."""
    pre, idx = rid.rsplit("-", 1)
    return pre, int(idx)


def pick_representative(runs, wanted):
    """One destination for every algorithm: prefer destinations measured with all algorithms, then the one
    with the longest, highest-median-goodput first-algorithm trace. Returns (dest, {algorithm: (run_id, run)})
    using each algorithm's repetition-0 run."""
    algs = algs_in(runs)
    by_dest = defaultdict(dict)
    for rid, r in sorted(runs.items(), key=lambda kv: run_seq(kv[0])):
        if r["repetition"] == 0:
            by_dest[dest(r)].setdefault(r["algorithm"], (rid, r))
    full = {d: v for d, v in by_dest.items() if all(a in v for a in algs)} or by_dest
    if wanted in full:
        return wanted, full[wanted]
    def score(d):
        r = full[d].get(algs[0], next(iter(full[d].values())))[1]
        return (len(r["t_s"]), np.nanmedian(r["goodput_bps"]))
    d = max(full, key=score)
    return d, full[d]


def limits(rs):
    """Common axis limits so that the per-algorithm plots are directly comparable."""
    def rng(c, floor=0.0):
        hi = np.nanmax([np.nanmax(r[c]) for r in rs if not np.all(np.isnan(r[c]))] or [1])
        return (floor, float(hi) * 1.05 if hi > 0 else 1.0)
    lo_rtt = np.nanmin([np.nanmin(r["srtt_ms"]) for r in rs])
    hi_rtt = np.nanmax([np.nanmax(r["srtt_ms"] + np.nan_to_num(r["rttvar_ms"])) for r in rs])
    return {"t": (0, max(float(np.nanmax(r["t_s"])) for r in rs)),
            "cwnd_segs": rng("cwnd_segs"), "srtt_ms": (float(lo_rtt) * 0.95, float(hi_rtt) * 1.05),
            "retrans_interval": (0, max(1.0, rng("retrans_interval")[1])),
            "goodput": (0, max(float(np.nanmax(r["goodput_bps"])) for r in rs) / 1e6 * 1.05)}


def plot_run(r, rid, out, suffix="", lim=None):
    """Q2(b): time-series figure and scatter figure for one run."""
    title = f"{dest(r)}  algorithm={r['algorithm']}  run={rid}"
    t, g = r["t_s"], r["goodput_bps"] / 1e6
    has = lambda c: not np.all(np.isnan(r[c]))
    fig, ax = plt.subplots(4, 1, figsize=(9, 10), sharex=True)
    ax[0].plot(t, r["cwnd_segs"], color="#2a6fbb")
    if has("ssthresh_segs"):
        ax[0].plot(t, r["ssthresh_segs"], color="#999", ls="--", lw=0.8, label="ssthresh")
        ax[0].legend(fontsize=8)
    ax[0].set_ylabel("snd_cwnd (MSS)")
    ax[1].plot(t, r["srtt_ms"], color="#d9731a", label="srtt")
    if has("rttvar_ms"):
        ax[1].fill_between(t, np.maximum(r["srtt_ms"] - r["rttvar_ms"], 0), r["srtt_ms"] + r["rttvar_ms"],
                           color="#d9731a", alpha=0.2, label="± rttvar")
    ax[1].set_ylabel("RTT (ms)")
    ax[1].legend(fontsize=8)
    ax[2].bar(t, r["retrans_interval"], width=float(np.nanmedian(r["dt_s"])), color="#c0392b",
              label="retransmits in interval")
    if has("lost"):
        ax[2].plot(t, r["lost"], color="#555", lw=0.8, label="lost (kernel estimate)")
    ax[2].set_ylabel("packets")
    ax[2].legend(fontsize=8)
    ax[3].plot(t, g, color="#2f9e55")
    ax[3].set_ylabel("goodput (Mbit/s)")
    ax[3].set_xlabel("time (s)")
    if lim:
        for a_, k in zip(ax, ["cwnd_segs", "srtt_ms", "retrans_interval", "goodput"]):
            a_.set_ylim(*lim[k])
        ax[3].set_xlim(*lim["t"])
    for a_ in ax:
        a_.grid(alpha=0.3)
    fig.suptitle(title, fontsize=10)
    fig.tight_layout()
    fig.savefig(os.path.join(out, f"tcpstats_timeseries{suffix}.pdf"))
    plt.close(fig)

    fig, ax = plt.subplots(1, 3, figsize=(15, 4.3))
    rng_ = np.random.default_rng(0)
    for a_, (c, lab, k) in zip(ax, [("cwnd_segs", "snd_cwnd (MSS)", "cwnd_segs"),
                                    ("srtt_ms", "RTT estimate (ms)", "srtt_ms"),
                                    ("retrans_interval", "retransmitted packets per interval", "retrans_interval")]):
        x = r[c].copy()
        if c == "retrans_interval":
            x = x + rng_.uniform(-0.12, 0.12, len(x))  # jitter integer counts so points don't stack
        a_.scatter(x, g, s=12, c=t, cmap="viridis", alpha=0.8)
        ok = ~np.isnan(r[c]) & ~np.isnan(g)
        if ok.sum() > 2 and np.std(r[c][ok]) > 0:
            a_.set_title(f"Pearson r = {np.corrcoef(r[c][ok], g[ok])[0, 1]:.2f}", fontsize=9)
        a_.set_xlabel(lab)
        a_.set_ylabel("goodput (Mbit/s)")
        a_.grid(alpha=0.3)
        if lim:
            a_.set_xlim(*(lim[k] if c != "retrans_interval" else (-0.5, lim[k][1] + 0.5)))
            a_.set_ylim(*lim["goodput"])
    sm = plt.cm.ScalarMappable(cmap="viridis", norm=plt.Normalize(t.min(), t.max()))
    fig.colorbar(sm, ax=ax, label="time (s)", pad=0.01)
    fig.suptitle(title, fontsize=10)
    fig.savefig(os.path.join(out, f"scatter_relationships{suffix}.pdf"), bbox_inches="tight")
    plt.close(fig)


def write_table(base, hdr, rows, note="", md=True):
    with open(base + ".csv", "w", newline="") as f:
        csv.writer(f).writerows([hdr] + rows)
    if not md:
        return
    with open(base + ".md", "w") as f:
        f.write("| " + " | ".join(hdr) + " |\n|" + "---|" * len(hdr) + "\n")
        for r in rows:
            f.write("| " + " | ".join(map(str, r)) + " |\n")
        f.write(note)


# ---------------------------------------------------------------- Q3: algorithm comparison
def run_metrics(r):
    dur = r["t_s"][-1]
    has = lambda c: not np.all(np.isnan(r[c]))
    srtt = r["srtt_ms"]
    g = r["goodput_bps"] / 1e6
    m = {"goodput": r["acked_bytes"][-1] * 8 / dur / 1e6,           # Mbit/s over the whole run
         "srtt_mean": float(np.nanmean(srtt)),
         "min_rtt": float(np.nanmin(r["min_rtt_ms"]) if has("min_rtt_ms") else np.nanmin(srtt)),
         "cwnd_bytes": float(np.nanmean(r["cwnd_bytes"])),
         "btlbw": float(np.nanpercentile(r["delivery_bps"], 95) / 1e6) if has("delivery_bps")
                  else float(np.percentile(g, 95))}                 # kernel delivery-rate estimate
    sent = np.nanmax(r["bytes_sent"]) if has("bytes_sent") else np.nan
    m["retrans_frac"] = float(np.nanmax(r["retrans_bytes"]) / sent) if has("retrans_bytes") and sent else float("nan")
    return m


def compare(runs, out, rep_dest, rep_runs):
    algs = algs_in(runs)
    M = {rid: run_metrics(r) for rid, r in runs.items()}
    # destination-level path estimates shared by all algorithms: best observed bottleneck bandwidth
    # (95th percentile of the kernel's delivery rate) and the smallest RTT seen on that path.
    D = defaultdict(lambda: {"btlbw": 0.0, "min_rtt": np.inf})
    for rid, r in runs.items():
        d = D[dest(r)]
        d["btlbw"] = max(d["btlbw"], M[rid]["btlbw"])
        d["min_rtt"] = min(d["min_rtt"], M[rid]["min_rtt"])
    for d in D.values():
        d["bdp"] = d["btlbw"] * 125.0 * d["min_rtt"]   # Mbit/s * ms -> bytes (1e6/8 * 1e-3)
    for rid, r in runs.items():
        d = D[dest(r)]
        M[rid].update(queue_ms=M[rid]["srtt_mean"] - d["min_rtt"],
                      cwnd_over_bdp=M[rid]["cwnd_bytes"] / d["bdp"] if d["bdp"] else float("nan"),
                      util=M[rid]["goodput"] / d["btlbw"] if d["btlbw"] else float("nan"))

    # ---- per-run table and test order
    f2 = lambda v: "" if v is None or (isinstance(v, float) and np.isnan(v)) else f"{v:.3f}"
    keys = ["goodput", "srtt_mean", "min_rtt", "queue_ms", "cwnd_bytes", "cwnd_over_bdp", "util", "retrans_frac"]
    ordered = sorted(runs, key=run_seq)
    write_table(os.path.join(out, "compare_runs"),   # rows are in execution order (the test order)
                ["seq", "run_id", "destination", "repetition", "algorithm"] + keys,
                [[i, rid, dest(runs[rid]), runs[rid]["repetition"], runs[rid]["algorithm"]] +
                 [f2(M[rid][k]) for k in keys] for i, rid in enumerate(ordered)], md=False)

    # ---- per destination x algorithm: mean +- std over repetitions
    grp = defaultdict(list)
    for rid, r in runs.items():
        grp[(dest(r), r["algorithm"])].append(M[rid])
    ms = lambda v: f"{np.nanmean(v):.2f} ± {np.nanstd(v, ddof=1) if len(v) > 1 else 0:.2f}"
    rows = []
    for (d, a), ml in sorted(grp.items(), key=lambda kv: (kv[0][0], alg_key(kv[0][1]))):
        col = lambda k: [x[k] for x in ml]
        rows.append([d, a, len(ml), ms(col("goodput")), ms(col("srtt_mean")), f"{D[d]['min_rtt']:.1f}",
                     ms(col("queue_ms")), f"{D[d]['btlbw']:.1f}", f"{D[d]['bdp']/1e3:.0f}",
                     f"{np.nanmean(col('cwnd_over_bdp')):.2f}", f"{np.nanmean(col('util')):.2f}",
                     f"{np.nanmean(col('retrans_frac')) * 100:.3f}" if not np.all(np.isnan(col('retrans_frac'))) else ""])
    reps = sorted({len(v) for v in grp.values()})
    note = (f"\nRepetitions per destination×algorithm: {reps if len(reps) > 1 else reps[0]}. Values are mean ± sample std "
            "across repetitions. goodput = acked bytes·8/duration (Mbit/s); srtt = mean smoothed RTT (ms); "
            "min RTT = smallest RTT seen on the path (ms); queueing = mean srtt − min RTT (ms); "
            "BtlBw = 95th percentile of the kernel delivery-rate estimate (best over all runs, Mbit/s; falls back to "
            "p95 goodput where unavailable); BDP = BtlBw·min RTT (KB); cwnd/BDP = mean cwnd ÷ BDP; "
            "util = goodput ÷ BtlBw; retrans = retransmitted bytes as % of bytes sent.\n")
    write_table(os.path.join(out, "compare_by_destination"),
                ["destination", "algorithm", "reps", "goodput Mbit/s", "mean srtt ms", "min RTT ms", "queueing ms",
                 "BtlBw Mbit/s", "BDP KB", "cwnd/BDP", "util", "retrans %"], rows, note)

    # ---- overall per algorithm
    dests = sorted(D)
    dmean = {(d, a): np.mean([x["goodput"] for x in grp[(d, a)]]) for (d, a) in grp}
    qmean = {(d, a): np.mean([x["queue_ms"] for x in grp[(d, a)]]) for (d, a) in grp}
    base = algs[0]
    rows = []
    for a in algs:
        ds = [d for d in dests if (d, a) in dmean]
        ratio = [dmean[(d, a)] / dmean[(d, base)] for d in ds if (d, base) in dmean and dmean[(d, base)] > 0]
        win_t = sum(1 for d in ds if dmean[(d, a)] == max(dmean[(d, x)] for x in algs if (d, x) in dmean))
        win_q = sum(1 for d in ds if qmean[(d, a)] == min(qmean[(d, x)] for x in algs if (d, x) in qmean))
        rows.append([a, len(ds), f"{np.mean([dmean[(d, a)] for d in ds]):.2f}",
                     f"{np.exp(np.mean(np.log(ratio))):.2f}" if ratio else "", win_t,
                     f"{np.mean([qmean[(d, a)] for d in ds]):.1f}", win_q])
    write_table(os.path.join(out, "compare_overall"),
                ["algorithm", "destinations", "mean goodput Mbit/s", f"geo-mean goodput vs {base}",
                 "dest. with best goodput", "mean queueing ms", "dest. with lowest queueing"], rows,
                "\nAcross destinations; each destination's value is the mean over its repetitions.\n")
    print(open(os.path.join(out, "compare_overall.md")).read())
    print(open(os.path.join(out, "compare_by_destination.md")).read())

    # ---- per-algorithm figures + side-by-side figures on the representative destination
    rs = [rep_runs[a][1] for a in algs if a in rep_runs]
    lim = limits(rs)
    for a in algs:
        if a in rep_runs:
            plot_run(rep_runs[a][1], rep_runs[a][0], out, f"_{a}", lim)

    n = len(rs)
    fig, ax = plt.subplots(4, n, figsize=(4.6 * n, 9), sharex=True, sharey="row", squeeze=False)
    for j, r in enumerate(rs):
        c = color(r["algorithm"])
        ax[0][j].plot(r["t_s"], r["cwnd_segs"], color=c)
        ax[1][j].plot(r["t_s"], r["srtt_ms"], color=c)
        ax[2][j].bar(r["t_s"], r["retrans_interval"], width=float(np.nanmedian(r["dt_s"])), color=c)
        ax[3][j].plot(r["t_s"], r["goodput_bps"] / 1e6, color=c)
        ax[0][j].set_title(r["algorithm"])
        ax[3][j].set_xlabel("time (s)")
    for i, lab in enumerate(["snd_cwnd (MSS)", "srtt (ms)", "retransmits / interval", "goodput (Mbit/s)"]):
        ax[i][0].set_ylabel(lab)
        for a_ in ax[i]:
            a_.grid(alpha=0.3)
    for k, i in zip(["cwnd_segs", "srtt_ms", "retrans_interval", "goodput"], range(4)):
        ax[i][0].set_ylim(*lim[k])
    ax[3][0].set_xlim(*lim["t"])
    fig.suptitle(f"{rep_dest}: same destination, shared axes", fontsize=10)
    fig.tight_layout()
    fig.savefig(os.path.join(out, "compare_timeseries.pdf"))
    plt.close(fig)

    # ---- variability across repetitions on the representative destination
    fig, ax = plt.subplots(1, 3, figsize=(13, 3.8))
    for a_, (k, lab) in zip(ax, [("goodput", "mean goodput (Mbit/s)"), ("srtt_mean", "mean srtt (ms)"),
                                 ("retrans_frac", "retransmitted fraction of bytes sent")]):
        for i, a in enumerate(algs):
            v = [M[rid][k] for rid, r in runs.items() if dest(r) == rep_dest and r["algorithm"] == a]
            a_.scatter([i] * len(v) + np.random.default_rng(1).uniform(-0.08, 0.08, len(v)), v,
                       color=color(a), s=28, alpha=0.8)
            if v:
                a_.hlines(np.nanmean(v), i - 0.25, i + 0.25, color="k", lw=1)
        a_.set_xticks(range(len(algs)))
        a_.set_xticklabels(algs)
        a_.set_ylabel(lab)
        a_.set_ylim(bottom=0)
        a_.grid(alpha=0.3, axis="y")
    fig.suptitle(f"{rep_dest}: one dot per repetition, bar = mean", fontsize=10)
    fig.tight_layout()
    fig.savefig(os.path.join(out, "compare_repetitions.pdf"))
    plt.close(fig)

    # ---- operating regimes: behaviour vs bandwidth-delay product across destinations
    fig, ax = plt.subplots(1, 3, figsize=(15, 4.3))
    for a in algs:
        ds = [d for d in dests if (d, a) in dmean]
        x = [D[d]["bdp"] / 1e3 for d in ds]
        if (algs[0] != a) and all((d, algs[0]) in dmean for d in ds):
            ax[0].scatter(x, [dmean[(d, a)] / dmean[(d, algs[0])] for d in ds], color=color(a), label=a, s=30)
        ax[1].scatter(x, [qmean[(d, a)] for d in ds], color=color(a), label=a, s=30)
        ax[2].scatter(x, [np.mean([m["util"] for m in grp[(d, a)]]) for d in ds], color=color(a), label=a, s=30)
    ax[0].axhline(1, color="k", lw=0.7)
    ax[0].set_ylabel(f"goodput relative to {algs[0]}")
    ax[1].set_ylabel("queueing delay: mean srtt − min RTT (ms)")
    ax[2].set_ylabel("utilisation = goodput / BtlBw")
    for a_ in ax:
        a_.set_xscale("log")
        a_.xaxis.set_major_formatter(matplotlib.ticker.ScalarFormatter())
        a_.xaxis.set_minor_formatter(matplotlib.ticker.NullFormatter())
        a_.set_xlabel("bandwidth-delay product (KB)")
        a_.grid(alpha=0.3)
        a_.legend(fontsize=8)
    fig.suptitle("One dot per destination (mean over repetitions)", fontsize=10)
    fig.tight_layout()
    fig.savefig(os.path.join(out, "compare_regimes.pdf"))
    plt.close(fig)


def run(out, representative=None):
    runs = load(out)
    if not runs:
        print("no successful runs to analyze")
        return
    suspect = sorted({dest(r) for r in runs.values() if np.nanmedian(r["srtt_ms"]) < 2.0})
    if suspect:  # a local TCP proxy / loopback would ACK locally and make cwnd, RTT and BDP meaningless
        print(f"\n*** WARNING: median RTT < 2 ms for {len(suspect)} destination(s): {suspect}\n"
              "*** The kernel is not seeing the real path (TCP proxied by the host/VM network?). "
              "Do not use their TCP statistics.\n")
    tables(runs, out)
    goodput_plot(runs, out)
    rep_dest, rep_runs = pick_representative(runs, representative)
    print(f"representative destination (same for all algorithms): {rep_dest}")
    compare(runs, out, rep_dest, rep_runs)
    print("wrote PDFs and tables to", out)


if __name__ == "__main__":
    run(sys.argv[1] if len(sys.argv) > 1 else "results", sys.argv[2] if len(sys.argv) > 2 else None)
