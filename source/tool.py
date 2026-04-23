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
from typing import List, Optional, Tuple

# Matplotlib có thể chưa được cài trong VM; vẫn cho phép chạy và xuất CSV/logs.
HAS_MPL = True
try:
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt  # noqa: E402
except Exception:
    HAS_MPL = False


def _load_build_net():
    """
    Luôn load đúng `52300211_TKM_Final_Report/source/topology.py` bằng đường dẫn tuyệt đối,
    tránh trường hợp Python import nhầm module `topology` ở chỗ khác trong PYTHONPATH.
    """
    import importlib.util
    import sys

    topo_path = (Path(__file__).resolve().parent / "topology.py").resolve()
    spec = importlib.util.spec_from_file_location("tkm_topology", topo_path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Không load được topology từ {topo_path}")
    mod = importlib.util.module_from_spec(spec)
    # Python 3.12: dataclasses/type resolution cần module tồn tại trong sys.modules
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)  # type: ignore[attr-defined]
    if not hasattr(mod, "build_net"):
        raise RuntimeError(f"File topology không có build_net(): {topo_path}")
    return mod.build_net


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


def _run_with_timeout(node, argv: List[str], timeout_s: float) -> Tuple[int, str]:
    """
    Chạy lệnh trong namespace của node bằng popen để tránh treo Mininet node.cmd().
    Trả (returncode, stdout+stderr).
    """
    p = node.popen(argv, stdout=None, stderr=None)  # stdout/stderr mặc định pipe trong Mininet
    try:
        out, _ = p.communicate(timeout=timeout_s)
    except Exception:
        try:
            p.kill()
        except Exception:
            pass
        raise
    # Mininet popen trả bytes hoặc str tuỳ phiên bản; normalize
    if isinstance(out, bytes):
        txt = out.decode(errors="ignore")
    else:
        txt = out or ""
    return p.returncode or 0, txt


def _ping(host, dst_ip: str, count: int = 20, interval: float = 0.05, timeout_s: int = 2) -> Tuple[PingResult, str]:
    # timeout tổng: count * (interval+timeout_s) + buffer
    total = float(count) * (float(interval) + float(timeout_s)) + 3.0
    argv = ["ping", "-c", str(count), "-i", str(interval), "-W", str(timeout_s), dst_ip]
    try:
        _, out = _run_with_timeout(host, argv, timeout_s=total)
    except Exception as e:
        out = f"[tool] ping timeout/error: {e}"
    return _parse_ping(out), out


def _has_iperf3(node) -> bool:
    return bool(node.cmd("command -v iperf3 2>/dev/null").strip())


def _iperf3_client(src, dst_ip: str, seconds: int = 10, port: int = 5201) -> Tuple[IperfResult, str]:
    t0 = time.time()
    argv = ["iperf3", "-c", dst_ip, "-t", str(seconds), "-p", str(port), "-J"]
    try:
        _, out = _run_with_timeout(src, argv, timeout_s=float(seconds) + 5.0)
    except Exception as e:
        dt = time.time() - t0
        return IperfResult(mbps=None, seconds=dt, error=f"iperf3 timeout/error: {e}"), "", 
    dt = time.time() - t0
    try:
        j = json.loads(out)
        bps = j.get("end", {}).get("sum_received", {}).get("bits_per_second")
        mbps = (float(bps) / 1_000_000.0) if bps is not None else None
        err = j.get("error")
        return IperfResult(mbps=mbps, seconds=dt, error=err), out
    except Exception as e:
        return IperfResult(mbps=None, seconds=dt, error=str(e)), out


def _dark_style() -> None:
    if not HAS_MPL:
        return
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
    if not HAS_MPL:
        return
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
    print(f"[INFO] logs:  {LOG_DIR}")
    print(f"[INFO] image: {IMG_DIR}")

    # Build topology (Phase 1)
    build_net = _load_build_net()
    net = build_net(start_cli=False)
    interrupted = False
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

        # Đợi routing hội tụ thật sự (OSPF/LDP) trước khi đo.
        # Nếu ping không thông thì chờ thêm để tránh "Network is unreachable" do chưa hội tụ.
        def _wait_reachability(src_name: str, dst_ip: str, max_wait_s: int = 90) -> bool:
            src = net.get(src_name)
            deadline = time.time() + max_wait_s
            while time.time() < deadline:
                pr, _ = _ping(src, dst_ip=dst_ip, count=1, interval=0.1, timeout_s=1)
                if pr.received >= 1:
                    return True
                time.sleep(2)
            return False

        # Warm-up: đảm bảo 2 đích quan trọng reachable từ host1
        for dst_ip in ("10.2.10.11", "10.3.10.11"):
            ok = _wait_reachability("host1", dst_ip, max_wait_s=90)
            print(f"[INFO] warm-up host1→{dst_ip}: {'OK' if ok else 'FAIL'}")

        has_iperf = _has_iperf3(host1)
        if not has_iperf:
            print("[WARN] Không thấy `iperf3` trong VM -> bỏ qua throughput (chỉ đo ping).")

        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        results: List[Measurement] = []

        port_base = 5201
        for idx, (src_name, dst_name, dst_ip) in enumerate(pairs):
            src = net.get(src_name)
            dst = net.get(dst_name)

            port = port_base + idx
            iperf_res = IperfResult(mbps=None, seconds=0.0, error=None)
            iperf_raw = ""
            if has_iperf:
                # start iperf3 server on dst (background)
                dst.cmd("pkill -f 'iperf3 -s' 2>/dev/null || true")
                dst.cmd(f"iperf3 -s -1 -p {port} >/tmp/iperf3_{dst_name}_{port}.log 2>&1 &")
                time.sleep(0.25)
                iperf_res, iperf_raw = _iperf3_client(src, dst_ip=dst_ip, seconds=10, port=port)
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

            # in nhanh 1 dòng tóm tắt để user thấy tool đang chạy được
            thr = f"{iperf_res.mbps:.2f} Mbps" if iperf_res.mbps is not None else "N/A"
            rtt = f"{ping_res.rtt_avg_ms:.3f} ms" if ping_res.rtt_avg_ms is not None else "N/A"
            jit = f"{ping_res.rtt_mdev_ms:.3f} ms" if ping_res.rtt_mdev_ms is not None else "N/A"
            print(f"[MEAS] {src_name}→{dst_name} thr={thr} rtt={rtt} jitter={jit} loss={ping_res.loss_pct:.1f}%")

        csv_path = LOG_DIR / f"results_{ts}.csv"
        _save_csv(results, csv_path)
        print(f"[OK] Saved CSV: {csv_path}")

        labels = [f"{r.src}→{r.dst}" for r in results]
        if HAS_MPL:
            _plot_bars("Throughput (iperf3)", labels, [r.throughput_mbps for r in results], "Mbps", IMG_DIR / f"throughput_{ts}.png")
            _plot_bars("Delay (ping avg)", labels, [r.ping_avg_ms for r in results], "ms", IMG_DIR / f"delay_{ts}.png")
            _plot_bars("Jitter (ping mdev)", labels, [r.ping_jitter_ms for r in results], "ms", IMG_DIR / f"jitter_{ts}.png")
            _plot_bars("Packet loss (ping)", labels, [r.ping_loss_pct for r in results], "%", IMG_DIR / f"loss_{ts}.png")
            print(f"[OK] Saved charts to: {IMG_DIR}")
        else:
            print("[WARN] Matplotlib chưa có -> bỏ qua vẽ chart (chỉ có CSV + raw logs).")
    except KeyboardInterrupt:
        interrupted = True
        print("\n[WARN] Bạn đã dừng tool (Ctrl+C). Đang dọn dẹp...")
    finally:
        # Dọn dẹp an toàn: tránh AssertionError nếu Mininet node đang bận lệnh
        try:
            if net is not None:
                net.stop()
        except Exception as e:
            print(f"[WARN] net.stop() gặp lỗi (có thể do Ctrl+C khi node đang chạy lệnh): {e}")
        if interrupted:
            print("[INFO] Đã dừng theo yêu cầu. File logs/ có thể đã được tạo một phần.")


if __name__ == "__main__":
    main()

