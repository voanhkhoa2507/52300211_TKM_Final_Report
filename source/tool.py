#!/usr/bin/env python3
"""
Phase 2 - GUI Tool (Tkinter) - chạy SAU khi bạn đã start `source/topology.py`.

Tool này làm giống demo của bạn:
- KHÔNG gọi build_net() và KHÔNG tạo topo mới.
- Dùng `ip netns exec <node> ...` để đo trực tiếp trên các namespace do Mininet đang chạy.

Chức năng:
- Ping Test: RTT avg + loss
- Traceroute Path: in đường đi (hop list)
- Throughput: iperf3 giữa 2 node
- CASE 1 (giống ảnh mẫu): OSPF restart/convergence + đồ thị throughput theo thời gian
- CASE 5 (giống ảnh mẫu): Bảng đường đi (hops) + throughput end-to-end

Output:
- `logs/` và `image/` trong `52300211_TKM_Final_Report/`
"""

from __future__ import annotations

import csv
import datetime as dt
import re
import subprocess
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional, Tuple

import tkinter as tk
from tkinter import messagebox, scrolledtext, ttk

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402


REPORT_DIR = Path(__file__).resolve().parent.parent
LOG_DIR = REPORT_DIR / "logs"
IMG_DIR = REPORT_DIR / "image"
LOG_DIR.mkdir(parents=True, exist_ok=True)
IMG_DIR.mkdir(parents=True, exist_ok=True)


NODE_LIST = [
    "host1",
    "admin1",
    "lab1",
    "guest1",
    "web1",
    "dns1",
    "db1",
    "CE1",
    "CE2",
    "CE3",
    "PE1",
    "PE2",
    "PE3",
    "P1",
    "P2",
    "P3",
    "P4",
    "SPINE1",
    "SPINE2",
    "LEAF_WEB",
    "LEAF_DNS",
    "LEAF_DB",
]

IP_MAP = {
    "host1": "10.1.0.11",
    "admin1": "10.2.10.11",
    "lab1": "10.2.20.11",
    "guest1": "10.2.30.11",
    "web1": "10.3.10.11",
    "dns1": "10.3.20.11",
    "db1": "10.3.30.11",
}


@dataclass
class PingStats:
    loss_pct: float
    rtt_avg_ms: float
    rtt_mdev_ms: float


def _sh(cmd: str, timeout_s: int = 20) -> str:
    p = subprocess.run(
        cmd,
        shell=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        timeout=timeout_s,
    )
    return p.stdout


def exec_netns(node: str, cmd: str, timeout_s: int = 20) -> str:
    return _sh(f"sudo ip netns exec {node} {cmd}", timeout_s=timeout_s)


def list_netns() -> List[str]:
    out = _sh("sudo ip netns list", timeout_s=10)
    ns = []
    for line in out.splitlines():
        name = line.strip().split(" ", 1)[0]
        if name:
            ns.append(name)
    return ns


def ensure_namespaces_ready(required: List[str]) -> Tuple[bool, str]:
    ns = set(list_netns())
    missing = [n for n in required if n not in ns]
    if missing:
        return (
            False,
            "Không thấy namespace của các node sau:\n"
            + "\n".join(missing)
            + "\n\nBạn cần chạy `sudo python3 source/topology.py` và giữ nguyên cửa sổ `mininet>` trước khi mở tool.",
        )
    return True, "OK"


def parse_ping_stats(ping_out: str) -> Optional[PingStats]:
    m_loss = re.search(r"(\d+(?:\.\d+)?)%\s+packet loss", ping_out)
    m_rtt = re.search(r"rtt [^=]+=\s*[\d\.]+/([\d\.]+)/[\d\.]+/([\d\.]+)\s*ms", ping_out)
    if not m_loss or not m_rtt:
        return None
    return PingStats(loss_pct=float(m_loss.group(1)), rtt_avg_ms=float(m_rtt.group(1)), rtt_mdev_ms=float(m_rtt.group(2)))


def ping_test(src: str, dst_ip: str, count: int = 5) -> Tuple[Optional[PingStats], str]:
    out = exec_netns(src, f"ping -c {count} -W 1 -q {dst_ip}", timeout_s=10 + count)
    return parse_ping_stats(out), out


def traceroute_path(src: str, dst_ip: str, max_hops: int = 12) -> Tuple[List[str], str]:
    out = exec_netns(src, f"traceroute -n -q 1 -w 1 -m {max_hops} {dst_ip}", timeout_s=30)
    hops = []
    for line in out.splitlines()[1:]:
        parts = line.split()
        if len(parts) >= 2 and parts[1] != "*":
            hops.append(parts[1])
    return hops, out


def iperf3_throughput_mbps(src: str, dst: str, dst_ip: str, seconds: int = 3, port: int = 5201) -> Tuple[Optional[float], str]:
    exec_netns(dst, "pkill -f 'iperf3 -s' 2>/dev/null || true", timeout_s=5)
    exec_netns(dst, f"iperf3 -s -1 -p {port} >/tmp/iperf3_{dst}_{port}.log 2>&1 &", timeout_s=5)
    time.sleep(0.2)
    out = exec_netns(src, f"iperf3 -c {dst_ip} -t {seconds} -p {port} -J 2>&1", timeout_s=seconds + 10)
    m = re.search(r'"bits_per_second"\s*:\s*([\d\.]+)', out)
    if not m:
        return None, out
    bps = float(m.group(1))
    return bps / 1_000_000.0, out


def save_case5_table(title: str, path_nodes: List[str], throughput_mbps: float, out_png: Path) -> None:
    fig, ax = plt.subplots(figsize=(12, 3))
    ax.axis("off")
    row1 = ["Path"] + path_nodes
    row2 = ["Thông lượng (Mbps)"] + [f"{throughput_mbps:.2f}"] * len(path_nodes)
    table = ax.table(
        cellText=[row1, row2],
        loc="center",
        cellLoc="center",
        colWidths=[0.12] + [0.12] * len(path_nodes),
    )
    table.auto_set_font_size(False)
    table.set_fontsize(10)
    table.scale(1, 2.2)
    for (r, c), cell in table.get_celld().items():
        cell.set_linewidth(1.5)
        if c == 0:
            cell.set_facecolor("#f4a261")
            cell.set_text_props(weight="bold")
        elif r == 0:
            cell.set_facecolor("#e3f2fd")
            cell.set_text_props(weight="bold")
        else:
            cell.set_facecolor("#ffffff")
    ax.set_title(title, fontsize=14, fontweight="bold", pad=12)
    fig.tight_layout()
    fig.savefig(out_png, dpi=200)
    plt.close(fig)


def save_case1_convergence(title: str, times_s: List[int], mbps: List[float], off_start: int, off_end: int, out_png: Path) -> None:
    fig, ax = plt.subplots(figsize=(10, 5))
    ax.axvspan(off_start, off_end, color="#ffcc99", alpha=0.5, label="OSPF OFF (Sập mạng)")
    ax.plot(times_s, mbps, color="#2a9d8f", linestyle="--", linewidth=2.5, label="S1 Băng Thông")
    ax.axvline(x=off_start, color="red", linewidth=1.5)
    ax.axvline(x=off_end, color="green", linewidth=1.5)
    ax.set_title(title, fontweight="bold")
    ax.set_xlabel("Thời gian (s) - Kéo dài chờ OSPF Bcast Wait Timer")
    ax.set_ylabel("Thông lượng ICMP (Mbps)")
    ax.set_xlim([0, max(times_s) if times_s else 80])
    ax.grid(linestyle="--", alpha=0.5)
    ax.legend()
    fig.tight_layout()
    fig.savefig(out_png, dpi=200)
    plt.close(fig)


class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("MPLS/Metro-E Measurement Tool (Phase 2)")
        self.geometry("980x640")

        ok, msg = ensure_namespaces_ready(["host1", "admin1", "web1", "PE1", "P1"])
        if not ok:
            messagebox.showerror("Không thấy Mininet namespaces", msg)
        self._build_ui()

    def _build_ui(self) -> None:
        top = ttk.Frame(self)
        top.pack(fill="x", padx=10, pady=8)

        ttk.Label(top, text="Nguồn (Src)").pack(side="left")
        self.src_var = tk.StringVar(value="host1")
        ttk.Combobox(top, textvariable=self.src_var, values=NODE_LIST, width=18, state="readonly").pack(side="left", padx=6)

        ttk.Label(top, text="Đích (Dst)").pack(side="left")
        self.dst_var = tk.StringVar(value="admin1")
        ttk.Combobox(top, textvariable=self.dst_var, values=list(IP_MAP.keys()), width=18, state="readonly").pack(side="left", padx=6)

        ttk.Button(top, text="Ping Test", command=self._run_ping).pack(side="left", padx=6)
        ttk.Button(top, text="Traceroute Path", command=self._run_trace).pack(side="left", padx=6)
        ttk.Button(top, text="Đo Throughput (iperf3)", command=self._run_iperf).pack(side="left", padx=6)

        right = ttk.Frame(top)
        right.pack(side="right")
        ttk.Button(right, text="Case 1 (OSPF restart)", command=self._run_case1).pack(side="left", padx=6)
        ttk.Button(right, text="Case 5 (Path table)", command=self._run_case5).pack(side="left", padx=6)

        self.log = scrolledtext.ScrolledText(self, height=28)
        self.log.pack(fill="both", expand=True, padx=10, pady=10)
        self._log(f"[INFO] logs: {LOG_DIR}")
        self._log(f"[INFO] image: {IMG_DIR}")
        self._log("[INFO] Tool này KHÔNG tạo topo mới. Hãy giữ cửa sổ `mininet>` đang chạy.")

    def _log(self, s: str) -> None:
        self.log.insert(tk.END, s + "\n")
        self.log.see(tk.END)
        self.log.update()
        with (LOG_DIR / "tool_gui.log").open("a", encoding="utf-8") as f:
            f.write(f"[{dt.datetime.now().isoformat(timespec='seconds')}] {s}\n")

    def _thread(self, fn) -> None:
        threading.Thread(target=fn, daemon=True).start()

    def _run_ping(self) -> None:
        def work():
            src = self.src_var.get()
            dst = self.dst_var.get()
            dst_ip = IP_MAP.get(dst, dst)
            self._log(f"[PING] {src} -> {dst} ({dst_ip})")
            stats, raw = ping_test(src, dst_ip, count=5)
            if stats:
                self._log(f"  loss={stats.loss_pct:.1f}% avg={stats.rtt_avg_ms:.3f}ms jitter={stats.rtt_mdev_ms:.3f}ms")
            else:
                self._log("  Không parse được ping (có thể unreachable).")
            (LOG_DIR / f"raw_ping_{src}_to_{dst}.txt").write_text(raw, encoding="utf-8")
        self._thread(work)

    def _run_trace(self) -> None:
        def work():
            src = self.src_var.get()
            dst = self.dst_var.get()
            dst_ip = IP_MAP.get(dst, dst)
            self._log(f"[TRACE] {src} -> {dst} ({dst_ip})")
            hops, raw = traceroute_path(src, dst_ip)
            self._log("  Hops: " + (" -> ".join(hops) if hops else "(none)"))
            (LOG_DIR / f"raw_traceroute_{src}_to_{dst}.txt").write_text(raw, encoding="utf-8")
        self._thread(work)

    def _run_iperf(self) -> None:
        def work():
            src = self.src_var.get()
            dst = self.dst_var.get()
            dst_ip = IP_MAP.get(dst)
            if not dst_ip:
                self._log("[IPERF] Dst phải là host có IP trong IP_MAP (admin1/web1/dns1/db1/...)")
                return
            self._log(f"[IPERF] {src} -> {dst} ({dst_ip})")
            mbps, raw = iperf3_throughput_mbps(src, dst, dst_ip, seconds=3, port=5201)
            if mbps is None:
                self._log("  iperf3 FAIL. Xem raw log.")
            else:
                self._log(f"  throughput={mbps:.2f} Mbps")
            (LOG_DIR / f"raw_iperf3_{src}_to_{dst}.txt").write_text(raw, encoding="utf-8")
        self._thread(work)

    def _run_case5(self) -> None:
        def work():
            src = "web1"
            dst = "db1"
            dst_ip = IP_MAP[dst]
            self._log(f"[CASE5] {src} -> {dst} ({dst_ip})")
            hops, _ = traceroute_path(src, dst_ip)
            mbps, raw = iperf3_throughput_mbps(src, dst, dst_ip, seconds=3, port=5205)
            if mbps is None:
                self._log("  iperf3 FAIL -> không vẽ được bảng throughput. (Xem raw)")
                (LOG_DIR / "case5_raw_iperf3.txt").write_text(raw, encoding="utf-8")
                return
            path_nodes = [src] + hops[:4] + [dst]
            title = f"BẢNG ĐIỀU TRA ĐƯỜNG ĐI ROUTING (IPv4) TỪ [{src.upper()}] ĐẾN [{dst.upper()}]"
            ts = dt.datetime.now().strftime("%Y%m%d_%H%M%S")
            out_png = IMG_DIR / f"case5_path_table_{ts}.png"
            save_case5_table(title, path_nodes, mbps, out_png)
            self._log(f"  Saved: {out_png}")
        self._thread(work)

    def _run_case1(self) -> None:
        def work():
            spine = "SPINE1"
            src = "web1"
            dst_ip = IP_MAP["db1"]
            off_start = 5
            off_end = 15
            total_s = 80
            self._log(f"[CASE1] OSPF restart on {spine}. Traffic: {src} -> db1 ({dst_ip})")

            exec_netns(src, "pkill -f 'ping ' 2>/dev/null || true", timeout_s=5)
            exec_netns(src, f"nohup sh -c 'ping -i 0.02 -s 1400 {dst_ip} >/dev/null 2>&1' >/tmp/case1_ping.log 2>&1 &", timeout_s=5)

            def tx_bytes() -> int:
                out = exec_netns(spine, "cat /sys/class/net/SPINE1-eth1/statistics/tx_bytes 2>/dev/null || echo 0", timeout_s=3)
                try:
                    return int(out.strip().splitlines()[-1])
                except Exception:
                    return 0

            times_s: List[int] = []
            mbps: List[float] = []
            last_tx = tx_bytes()
            for t in range(total_s + 1):
                time.sleep(1)
                cur = tx_bytes()
                m = max(0.0, ((cur - last_tx) * 8) / 1_000_000.0)
                last_tx = cur
                times_s.append(t)
                mbps.append(m)

                if t == off_start:
                    exec_netns(spine, "test -f /tmp/SPINE1/ospfd.pid && kill -9 $(cat /tmp/SPINE1/ospfd.pid) 2>/dev/null || true", timeout_s=5)
                    self._log("  -> Đã tắt ospfd SPINE1 tại t=5s")
                if t == off_end:
                    exec_netns(spine, "/usr/lib/frr/ospfd -d -A 127.0.0.1 -z /tmp/SPINE1/run/zserv.api --vty_socket /tmp/SPINE1/run -f /tmp/SPINE1/ospfd.conf -i /tmp/SPINE1/ospfd.pid >/dev/null 2>&1", timeout_s=5)
                    self._log("  -> Đã bật lại ospfd SPINE1 tại t=15s")

            exec_netns(src, "pkill -f 'ping -i 0.02' 2>/dev/null || true", timeout_s=5)

            ts = dt.datetime.now().strftime("%Y%m%d_%H%M%S")
            out_png = IMG_DIR / f"case1_ospf_convergence_{ts}.png"
            save_case1_convergence("CASE 1: OSPF STARTUP CONVERGENCE (SPINE1)", times_s, mbps, off_start, off_end, out_png)
            out_csv = LOG_DIR / f"case1_ospf_convergence_{ts}.csv"
            with out_csv.open("w", newline="", encoding="utf-8") as f:
                w = csv.writer(f)
                w.writerow(["time_s", "throughput_mbps"])
                for t, v in zip(times_s, mbps):
                    w.writerow([t, f"{v:.6f}"])
            self._log(f"  Saved: {out_png}")
            self._log(f"  Saved: {out_csv}")
        self._thread(work)


def main() -> None:
    App().mainloop()


if __name__ == "__main__":
    main()


