| destination | algorithm | reps | goodput Mbit/s | mean srtt ms | min RTT ms | queueing ms | BtlBw Mbit/s | BDP KB | cwnd/BDP | util | retrans % |
|---|---|---|---|---|---|---|---|---|---|---|---|
| yyc-speedtest.xplore.ca:8072 | cubic | 3 | 34.43 ± 3.03 | 93.22 ± 0.71 | 76.7 | 16.53 ± 0.71 | 47.7 | 457 | 0.90 | 0.72 | 0.458 |
| yyc-speedtest.xplore.ca:8072 | reno | 3 | 27.54 ± 1.03 | 89.35 ± 0.96 | 76.7 | 12.66 ± 0.96 | 47.7 | 457 | 0.70 | 0.58 | 0.539 |
| yyc-speedtest.xplore.ca:8072 | bbr | 3 | 34.85 ± 0.88 | 114.65 ± 4.66 | 76.7 | 37.96 ± 4.66 | 47.7 | 457 | 2.25 | 0.73 | 11.085 |

Repetitions per destination×algorithm: 3. Values are mean ± sample std across repetitions. goodput = acked bytes·8/duration (Mbit/s); srtt = mean smoothed RTT (ms); min RTT = smallest RTT seen on the path (ms); queueing = mean srtt − min RTT (ms); BtlBw = 95th percentile of the kernel delivery-rate estimate (best over all runs, Mbit/s; falls back to p95 goodput where unavailable); BDP = BtlBw·min RTT (KB); cwnd/BDP = mean cwnd ÷ BDP; util = goodput ÷ BtlBw; retrans = retransmitted bytes as % of bytes sent.
