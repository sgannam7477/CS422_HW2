"""From-scratch iperf3 (TCP, client -> server) client with goodput / TCP_INFO sampling. Linux only.

Protocol (iperf3 3.x):
  1. Control connection: TCP connect, send a 37-byte cookie (36 chars + NUL).
  2. Server sends state PARAM_EXCHANGE (or ACCESS_DENIED if busy); client sends the test
     parameters as 4-byte big-endian length + JSON.
  3. Server sends CREATE_STREAMS; client opens the data connection and sends the same cookie on it.
  4. Server sends TEST_START then TEST_RUNNING; client streams data for `duration` seconds.
  5. Client sends TEST_END; server sends EXCHANGE_RESULTS; both swap length-prefixed JSON results.
  6. Server sends DISPLAY_RESULTS; client sends IPERF_DONE and closes.
State bytes are signed chars. Statistics come from getsockopt(TCP_INFO) on the *data* socket.
"""
import json
import os
import random
import select
import socket
import string
import struct
import sys
import time

BLKSIZE = 128 * 1024
CONNECT_TIMEOUT = 8.0   # seconds to establish a TCP connection
IO_TIMEOUT = 10.0       # control-message wait / send-stall limit

TEST_START, TEST_RUNNING, TEST_END = 1, 2, 4
PARAM_EXCHANGE, CREATE_STREAMS = 9, 10
SERVER_TERMINATE = 11
EXCHANGE_RESULTS, DISPLAY_RESULTS, IPERF_DONE = 13, 14, 16
ACCESS_DENIED, SERVER_ERROR = -1, -2
STATE_NAMES = {1: "TEST_START", 2: "TEST_RUNNING", 4: "TEST_END", 9: "PARAM_EXCHANGE",
               10: "CREATE_STREAMS", 11: "SERVER_TERMINATE", 13: "EXCHANGE_RESULTS",
               14: "DISPLAY_RESULTS", 16: "IPERF_DONE", -1: "ACCESS_DENIED", -2: "SERVER_ERROR"}


class IperfError(Exception):
    """A failed test; `kind` is a short category (busy, connect-timeout, premature-close, ...)."""

    def __init__(self, kind, msg):
        super().__init__(f"{kind}: {msg}")
        self.kind = kind


def log(msg):
    print(time.strftime("%H:%M:%S ") + msg, flush=True)


def require_linux():
    if not sys.platform.startswith("linux"):
        sys.exit(f"This tool needs a Linux kernel (Ubuntu 22.04+ or WSL2); found '{sys.platform}'.")


# ---------------------------------------------------------------- socket helpers
def connect(host, port, cc=None):
    """TCP connect (IPv4/IPv6) with a timeout. `cc` selects the congestion control *before* connecting."""
    last = None
    try:
        infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except socket.gaierror as e:
        raise IperfError("dns", str(e))
    for fam, typ, proto, _, addr in infos:
        s = socket.socket(fam, typ, proto)
        try:
            if cc:
                s.setsockopt(socket.IPPROTO_TCP, socket.TCP_CONGESTION, cc.encode())
            s.settimeout(CONNECT_TIMEOUT)
            s.connect(addr)
            return s
        except socket.timeout:
            last = IperfError("connect-timeout", f"{host}:{port}")
        except OSError as e:
            last = IperfError("connect-failed", f"{host}:{port} {e}")
        s.close()
    raise last


def recv_exact(sock, n, timeout):
    """Read exactly n bytes from a TCP stream within `timeout` seconds."""
    deadline = time.monotonic() + timeout
    buf = b""
    while len(buf) < n:
        left = deadline - time.monotonic()
        if left <= 0:
            raise IperfError("timeout", "control connection unresponsive")
        sock.settimeout(left)
        try:
            chunk = sock.recv(n - len(buf))
        except socket.timeout:
            raise IperfError("timeout", "control connection unresponsive")
        except OSError as e:
            raise IperfError("premature-close", str(e))
        if not chunk:
            raise IperfError("premature-close", "server closed control connection")
        buf += chunk
    return buf


def read_state(ctrl, timeout=IO_TIMEOUT):
    return struct.unpack("b", recv_exact(ctrl, 1, timeout))[0]


def send_state(ctrl, state):
    ctrl.settimeout(IO_TIMEOUT)
    ctrl.sendall(struct.pack("b", state))


def send_json(ctrl, obj):
    data = json.dumps(obj, separators=(",", ":")).encode()
    ctrl.settimeout(IO_TIMEOUT)
    ctrl.sendall(struct.pack(">I", len(data)) + data)


def recv_json(ctrl, timeout):
    (n,) = struct.unpack(">I", recv_exact(ctrl, 4, timeout))
    if n > 1 << 20:
        raise IperfError("protocol", f"absurd JSON length {n}")
    return json.loads(recv_exact(ctrl, n, timeout))


def check_state(state, expected):
    """Raise IperfError unless the server sent the expected state."""
    if state == expected:
        return
    if state == ACCESS_DENIED:
        raise IperfError("busy", "server busy / access denied")
    if state == SERVER_TERMINATE:
        raise IperfError("server-terminated", "server terminated the test")
    if state == SERVER_ERROR:
        raise IperfError("server-error", "server reported an error")
    raise IperfError("protocol", f"got {STATE_NAMES.get(state, state)}, expected {STATE_NAMES.get(expected)}")


# ---------------------------------------------------------------- TCP statistics
STAT_FIELDS = ["acked", "cwnd_segs", "cwnd_bytes", "mss", "srtt_ms", "rttvar_ms", "min_rtt_ms",
               "ssthresh_segs", "retrans_total", "lost", "retrans_bytes",
               "pacing_bps", "delivery_bps", "bytes_sent"]


def tcp_stats(sock):
    """Decode the kernel's struct tcp_info for the *data* socket (getsockopt TCP_INFO).
    Linux byte offsets, little-endian:
      u32: snd_mss@16 lost@32 rtt(us)@68 rttvar(us)@72 snd_ssthresh@76 snd_cwnd@80
           total_retrans@100 min_rtt(us)@148
      u64: pacing_rate(B/s)@104 bytes_acked@120 delivery_rate(B/s)@160 bytes_sent@200 bytes_retrans@208
    Fields missing from an older kernel stay None."""
    out = dict.fromkeys(STAT_FIELDS)
    raw = sock.getsockopt(socket.IPPROTO_TCP, socket.TCP_INFO, 256)
    n = len(raw)
    u32 = lambda o: struct.unpack_from("=I", raw, o)[0] if o + 4 <= n else None
    u64 = lambda o: struct.unpack_from("=Q", raw, o)[0] if o + 8 <= n else None
    ms = lambda v: None if v is None else v / 1000.0
    mss, cwnd, ss = u32(16), u32(80), u32(76)
    out.update(acked=u64(120), mss=mss, cwnd_segs=cwnd,
               cwnd_bytes=cwnd * mss if mss and cwnd else None,
               srtt_ms=ms(u32(68)), rttvar_ms=ms(u32(72)), min_rtt_ms=ms(u32(148)),
               ssthresh_segs=ss if ss is not None and ss < 0x7ffffff0 else None,  # 0x7fffffff = unset
               retrans_total=u32(100), lost=u32(32), retrans_bytes=u64(208), bytes_sent=u64(200),
               pacing_bps=None if u64(104) is None else u64(104) * 8,
               delivery_bps=None if u64(160) is None else u64(160) * 8)
    return out


def congestion_control_in_use(sock):
    """Name of the CC algorithm actually attached to the data socket."""
    return sock.getsockopt(socket.IPPROTO_TCP, socket.TCP_CONGESTION, 16).split(b"\0")[0].decode()


# ---------------------------------------------------------------- one iperf3 test
def run_test(host, port, duration, cc, interval):
    """Run one TCP test. Returns dict(samples=[...], sent_bytes, server_bytes, cc_actual); raises IperfError."""
    samples = []
    ctrl = data = None
    cookie = "".join(random.choice(string.ascii_lowercase + string.digits) for _ in range(36)).encode() + b"\0"
    info = {"sent_bytes": 0, "server_bytes": None}
    try:
        # ---- handshake
        ctrl = connect(host, port)
        ctrl.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        ctrl.sendall(cookie)
        check_state(read_state(ctrl), PARAM_EXCHANGE)
        send_json(ctrl, {"tcp": True, "omit": 0, "time": int(duration), "num": 0, "blockcount": 0,
                         "parallel": 1, "len": BLKSIZE, "pacing_timer": 1000, "client_version": "3.9"})
        check_state(read_state(ctrl), CREATE_STREAMS)
        data = connect(host, port, cc)   # congestion control is chosen on the data socket before connect
        data.sendall(cookie)
        info["cc_actual"] = congestion_control_in_use(data)
        check_state(read_state(ctrl), TEST_START)
        check_state(read_state(ctrl), TEST_RUNNING)

        # ---- send for `duration` seconds, sampling TCP_INFO every `interval`
        data.setblocking(False)
        buf = os.urandom(BLKSIZE)
        base = tcp_stats(data)["acked"]          # cumulative counter at test start
        t0 = time.monotonic()
        end, next_s = t0 + duration, t0 + interval
        prev_t, prev_acked, prev_retr = t0, 0, 0
        last_progress = t0
        payload = 0

        def sample(now):
            nonlocal prev_t, prev_acked, prev_retr
            st = tcp_stats(data)
            if st["acked"] is None:
                raise IperfError("no-tcp-stats", "kernel gave no bytes_acked counter")
            acked = max(st["acked"] - base, prev_acked)          # cumulative, monotone
            dt = now - prev_t
            if dt > 0:
                samples.append({"t": now - t0, "dt": dt, "acked": acked,
                                "goodput_bps": (acked - prev_acked) * 8 / dt,
                                "retrans_interval": st["retrans_total"] - prev_retr,
                                **{k: v for k, v in st.items() if k != "acked"}})
            prev_t, prev_acked, prev_retr = now, acked, st["retrans_total"]

        while True:
            now = time.monotonic()
            if now >= end:
                break
            if now >= next_s:
                sample(now)
                next_s += interval
                if next_s <= now:  # fell behind: don't burst catch-up samples
                    next_s = now + interval
            r, w, _ = select.select([ctrl], [data], [], max(0.0, min(next_s, end) - now))
            if r:  # the server speaks mid-test only on error / termination
                b = ctrl.recv(1)
                if not b:
                    raise IperfError("premature-close", "server closed control connection mid-test")
                check_state(struct.unpack("b", b)[0], TEST_RUNNING)
            if w:
                try:
                    n = data.send(buf)
                except (BlockingIOError, InterruptedError):
                    n = 0
                except (ConnectionResetError, BrokenPipeError) as e:
                    raise IperfError("premature-close", f"data connection: {e}")
                if n:
                    payload += n
                    last_progress = time.monotonic()
            if time.monotonic() - last_progress > IO_TIMEOUT:
                raise IperfError("stalled", f"no send progress for {IO_TIMEOUT}s")
        sample(time.monotonic())
        info["sent_bytes"] = payload

        # ---- termination and results exchange
        send_state(ctrl, TEST_END)
        check_state(read_state(ctrl, 30), EXCHANGE_RESULTS)
        send_json(ctrl, {"cpu_util_total": 0, "cpu_util_user": 0, "cpu_util_system": 0,
                         "sender_has_retransmits": -1,
                         "streams": [{"id": 1, "bytes": payload, "retransmits": -1, "jitter": 0, "errors": 0,
                                      "omitted_errors": 0, "packets": 0, "omitted_packets": 0,
                                      "start_time": 0, "end_time": duration}]})
        try:  # data is already sent and sampled; a failure here only affects the epilogue
            srv = recv_json(ctrl, 30)
            info["server_bytes"] = sum(s.get("bytes", 0) for s in srv.get("streams", []))
            check_state(read_state(ctrl, 30), DISPLAY_RESULTS)
            send_state(ctrl, IPERF_DONE)
        except IperfError as e:
            info["epilogue_error"] = str(e)
        info["samples"] = samples
        return info
    except OSError as e:
        raise IperfError("os-error", str(e))
    finally:
        for s in (data, ctrl):
            if s:
                s.close()
