#!/usr/bin/env python3
"""
Phase 2 - tool.py

Đo hiệu năng trên Mininet topology (Phase 1):
- Throughput: iperf3 (Mbps)
- Delay/Jitter/Loss: ping (avg rtt, mdev, packet loss)

Kết quả:
- CSV trong `52300211_TKM_Final_Report/logs/`
- Biểu đồ PNG trong `52300211_TKM_Final_Report/image/` (dark background)

Yêu cầu: KHÔNG dựng lại topology; phải gọi build_net() từ topology.py.
"""

from __future__ import annotations

import csv
import json
import re
import time
from dataclasses import dataclass, asdict
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402


# Chạy dạng script: `python3 source/tool.py` -> import topology trong cùng thư mục source
from topology import build_net  # type: ignore  # noqa: E402


THIS_DIR = Path(__file__).resolve().parent
REPORT_DIR = THIS_DIR.parent
LOG_DIR = REPORT_DIR / "logs"
IMG_DIR = REPORT_DIR / "image"


@dataclass
class PingResult:
    sent: int
    received: int
    loss_pct: float
    rtt_min_ms: Optional[float]
    rtt_avg_ms: Optional[float]
    rtt_max_ms: Optional[float]
    rtt_mdev_ms: Optional[float]


@dataclass
class IperfResult:
    mbps: Optional[float]
    seconds: float
    error: Optional[str]


@dataclass
class Measurement:
    ts: str
    src: str
    dst: str
    dst_ip: str
    throughput_mbps: Optional[float]
    ping_avg_ms: Optional[float]
    ping_jitter_ms: Optional[float]
    ping_loss_pct: Optional[float]
    raw_error: Optional[str]


def _ensure_dirs() -> None:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    IMG_DIR.mkdir(parents=True, exist_ok=True)


def _parse_ping(out: str) -> PingResult:
    # packet loss line: "3 packets transmitted, 3 received, 0% packet loss, time 2003ms"
    m = re.search(r"(\d+)\s+packets transmitted,\s+(\d+)\s+received.*?(\d+(?:\.\d+)?)%\s+packet loss", out)
    sent = int(m.group(1)) if m else 0
    recv = int(m.group(2)) if m else 0
    loss = float(m.group(3)) if m else 100.0

    # rtt line: "rtt min/avg/max/mdev = 0.422/1.660/3.929/1.606 ms"
    rtt_min = rtt_avg = rtt_max = rtt_mdev = None
    m2 = re.search(r"rtt [^=]+=\s*([\d\.]+)/([\d\.]+)/([\d\.]+)/([\d\.]+)\s*ms", out)
    if m2:
        rtt_min, rtt_avg, rtt_max, rtt_mdev = (float(m2.group(i)) for i in range(1, 5))

    return PingResult(sent=sent, received=recv, loss_pct=loss, rtt_min_ms=rtt_min, rtt_avg_ms=rtt_avg, rtt_max_ms=rtt_max, rtt_mdev_ms=rtt_mdev)


def _ping(host, dst_ip: str, count: int = 20, interval: float = 0.05, timeout_s: int = 2) -> Tuple[PingResult, str]:
    cmd = f"ping -c {count} -i {interval} -W {timeout_s} {dst_ip}"
    out = host.cmd(cmd)
    return _parse_ping(out), out


def _iperf3(src, dst_ip: str, seconds: int = 10, port: int = 5201) -> Tuple[IperfResult, str, str]:
    # Server on destination: -1 (one test), JSON not needed on server
    server_out = src.cmd("true")  # placeholder to keep return types stable
    # Start server on remote via Mininet: run in background on dst host namespace using `cmd` from dst node
    # Caller will provide dst node for starting server; so this function is only client side.
    t0 = time.time()
    out = src.cmd(f"iperf3 -c {dst_ip} -t {seconds} -p {port} -J 2>&1")
    dt = time.time() - t0
    try:
        j = json.loads(out)
        bps = j.get("end", {}).get("sum_received", {}).get("bits_per_second")
        mbps = (float(bps) / 1_000_000.0) if bps is not None else None
        err = j.get("error")
        return IperfResult(mbps=mbps, seconds=dt, error=err), out, server_out
    except Exception as e:
        return IperfResult(mbps=None, seconds=dt, error=str(e)), out, server_out


def _dark_style() -> None:
    plt.style.use("dark_background")
    matplotlib.rcParams.update(
        {
            "figure.figsize": (10, 5),
            "axes.grid": True,
            "grid.alpha": 0.25,
            "axes.titleweight": "bold",
        }
    )


def _save_csv(rows: List[Measurement], path: Path) -> None:
    with path.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=list(asdict(rows[0]).keys()))
        w.writeheader()
        for r in rows:
            w.writerow(asdict(r))


def _plot_bars(title: str, labels: List[str], values: List[Optional[float]], ylabel: str, out_path: Path) -> None:
    _dark_style()
    vals = [v if v is not None else 0.0 for v in values]
    fig, ax = plt.subplots()
    ax.bar(labels, vals)
    ax.set_title(title)
    ax.set_ylabel(ylabel)
    ax.set_xticklabels(labels, rotation=20, ha="right")
    fig.tight_layout()
    fig.savefig(out_path, dpi=200)
    plt.close(fig)


def main() -> None:
    _ensure_dirs()

    # Build topology (Phase 1)
    net = build_net(start_cli=False)
    try:
        # Map host names used for measurements
        host1 = net.get("host1")
        admin1 = net.get("admin1")
        web1 = net.get("web1")

        pairs = [
            ("host1", "admin1", "10.2.10.11"),
            ("host1", "web1", "10.3.10.11"),
            ("admin1", "web1", "10.3.10.11"),
        ]

        # Basic availability checks
        # iperf3 presence
        _ = host1.cmd("iperf3 -v 2>/dev/null | head -n 1")

        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        results: List[Measurement] = []

        port_base = 5201
        for idx, (src_name, dst_name, dst_ip) in enumerate(pairs):
            src = net.get(src_name)
            dst = net.get(dst_name)

            port = port_base + idx
            # start iperf3 server on dst
            dst.cmd(f"pkill -f 'iperf3 -s' 2>/dev/null || true")
            dst.cmd(f"iperf3 -s -1 -p {port} >/tmp/iperf3_{dst_name}_{port}.log 2>&1 &")
            time.sleep(0.3)

            iperf_res, iperf_raw, _ = _iperf3(src, dst_ip=dst_ip, seconds=10, port=port)
            ping_res, ping_raw = _ping(src, dst_ip=dst_ip, count=20, interval=0.05)

            err = iperf_res.error
            if err is None and "error" in iperf_raw.lower() and iperf_res.mbps is None:
                err = "iperf3 returned error output"

            results.append(
                Measurement(
                    ts=ts,
                    src=src_name,
                    dst=dst_name,
                    dst_ip=dst_ip,
                    throughput_mbps=iperf_res.mbps,
                    ping_avg_ms=ping_res.rtt_avg_ms,
                    ping_jitter_ms=ping_res.rtt_mdev_ms,
                    ping_loss_pct=ping_res.loss_pct,
                    raw_error=err,
                )
            )

            # save raw logs (optional but helpful)
            (LOG_DIR / f"raw_ping_{ts}_{src_name}_to_{dst_name}.txt").write_text(ping_raw, encoding="utf-8")
            (LOG_DIR / f"raw_iperf3_{ts}_{src_name}_to_{dst_name}.json").write_text(iperf_raw, encoding="utf-8")

        csv_path = LOG_DIR / f"results_{ts}.csv"
        _save_csv(results, csv_path)

        labels = [f"{r.src}→{r.dst}" for r in results]
        _plot_bars("Throughput (iperf3)", labels, [r.throughput_mbps for r in results], "Mbps", IMG_DIR / f"throughput_{ts}.png")
        _plot_bars("Delay (ping avg)", labels, [r.ping_avg_ms for r in results], "ms", IMG_DIR / f"delay_{ts}.png")
        _plot_bars("Jitter (ping mdev)", labels, [r.ping_jitter_ms for r in results], "ms", IMG_DIR / f"jitter_{ts}.png")
        _plot_bars("Packet loss (ping)", labels, [r.ping_loss_pct for r in results], "%", IMG_DIR / f"loss_{ts}.png")

        print(f"[OK] Saved CSV: {csv_path}")
        print(f"[OK] Saved charts to: {IMG_DIR}")
    finally:
        net.stop()


if __name__ == "__main__":
    main()

