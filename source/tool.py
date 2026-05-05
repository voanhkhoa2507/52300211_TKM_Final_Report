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
import json
import shlex

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


def _get_node_pid(node: str) -> Optional[int]:
    """
    Mininet thường KHÔNG tạo named-netns cho `ip netns list`.
    Cách chuẩn để vào namespace node là tìm PID của node shell (mnexec) và dùng `mnexec -a <pid>`.
    """
    # Ưu tiên đọc PID map do topology.py xuất ra
    try:
        p = Path("/tmp/tkm_mininet_pids.json")
        if p.exists():
            data = json.loads(p.read_text(encoding="utf-8"))
            v = data.get(node)
            if isinstance(v, int) and v > 0:
                return v
    except Exception:
        pass

    # Ưu tiên pattern mnexec -n <node>
    out = _sh(f"pgrep -a -f \"mnexec .* -n {node}( |$)\" | head -n 1", timeout_s=5).strip()
    if out:
        try:
            return int(out.split(" ", 1)[0])
        except Exception:
            pass
    # Fallback: đôi khi dạng `mnexec -n node`
    out = _sh(f"pgrep -a -f \"mnexec -n {node}( |$)\" | head -n 1", timeout_s=5).strip()
    if out:
        try:
            return int(out.split(" ", 1)[0])
        except Exception:
            pass

    # Fallback: đôi khi cmdline chứa "mininet:<node>"
    out = _sh(f"pgrep -a -f \"mininet:{node}\" | head -n 1", timeout_s=5).strip()
    if out:
        try:
            return int(out.split(" ", 1)[0])
        except Exception:
            pass
    return None


def exec_node(node: str, cmd: str, timeout_s: int = 20) -> str:
    pid = _get_node_pid(node)
    if pid is None:
        return f"[tool] Không tìm thấy PID của node `{node}`. Hãy chắc chắn đang mở `mininet>`.\n"
    return _sh(f"sudo mnexec -a {pid} {cmd}", timeout_s=timeout_s)


def popen_node(node: str, argv: List[str]) -> Tuple[Optional[subprocess.Popen], str]:
    """
    Chạy lệnh trong namespace node và stream stdout theo dòng.
    argv: danh sách tham số (không shell).
    """
    pid = _get_node_pid(node)
    if pid is None:
        return None, f"[tool] Không tìm thấy PID của node `{node}`. Hãy chắc chắn đang mở `mininet>`.\n"
    try:
        p = subprocess.Popen(
            ["sudo", "mnexec", "-a", str(pid), *argv],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
            universal_newlines=True,
        )
        return p, "OK"
    except Exception as exc:
        return None, f"[tool] Không chạy được lệnh trong `{node}`: {exc}\n"


def ensure_namespaces_ready(required: List[str]) -> Tuple[bool, str]:
    missing = [n for n in required if _get_node_pid(n) is None]
    if missing:
        return (
            False,
            "Không thấy Mininet process (PID) của các node sau:\n"
            + "\n".join(missing)
            + "\n\nBạn cần chạy `sudo python3 source/topology.py` và giữ nguyên cửa sổ `mininet>` trước khi mở tool.\n"
            + "Lưu ý: tool dùng `mnexec -a <pid>` (không dùng `ip netns`).",
        )
    return True, "OK"


def parse_ping_stats(ping_out: str) -> Optional[PingStats]:
    m_loss = re.search(r"(\d+(?:\.\d+)?)%\s+packet loss", ping_out)
    m_rtt = re.search(r"rtt [^=]+=\s*[\d\.]+/([\d\.]+)/[\d\.]+/([\d\.]+)\s*ms", ping_out)
    if not m_loss or not m_rtt:
        return None
    return PingStats(loss_pct=float(m_loss.group(1)), rtt_avg_ms=float(m_rtt.group(1)), rtt_mdev_ms=float(m_rtt.group(2)))


def ping_test(src: str, dst_ip: str, count: int = 5) -> Tuple[Optional[PingStats], str]:
    out = exec_node(src, f"ping -c {count} -W 1 -q {dst_ip}", timeout_s=10 + count)
    return parse_ping_stats(out), out


def ping_test_stream(src: str, dst_ip: str, count: int = 5, timeout_s: int = 20) -> Tuple[Optional[PingStats], str, List[str]]:
    """
    Ping nhưng trả thêm list các dòng output để GUI hiển thị giống ping thường.
    """
    p, msg = popen_node(src, ["ping", "-c", str(count), "-W", "1", "-n", dst_ip])
    if p is None or p.stdout is None:
        return None, msg, [msg]

    lines: List[str] = []
    t_end = time.time() + max(3, timeout_s)
    try:
        while True:
            if time.time() > t_end:
                try:
                    p.kill()
                except Exception:
                    pass
                lines.append("[tool] Ping TIMEOUT\n")
                break
            line = p.stdout.readline()
            if not line:
                if p.poll() is not None:
                    break
                time.sleep(0.01)
                continue
            lines.append(line.rstrip("\n"))
    finally:
        try:
            p.stdout.close()
        except Exception:
            pass

    raw = "\n".join(lines) + ("\n" if lines else "")
    return parse_ping_stats(raw), raw, lines


def traceroute_path(src: str, dst_ip: str, max_hops: int = 12) -> Tuple[List[str], str]:
    out = exec_node(src, f"traceroute -n -q 1 -w 1 -m {max_hops} {dst_ip}", timeout_s=30)
    hops = []
    for line in out.splitlines()[1:]:
        parts = line.split()
        if len(parts) >= 2 and parts[1] != "*":
            hops.append(parts[1])
    return hops, out


def iperf3_throughput_mbps(src: str, dst: str, dst_ip: str, seconds: int = 3, port: int = 5201) -> Tuple[Optional[float], str]:
    exec_node(dst, "pkill -f 'iperf3 -s' 2>/dev/null || true", timeout_s=5)
    exec_node(dst, f"iperf3 -s -1 -p {port} >/tmp/iperf3_{dst}_{port}.log 2>&1 &", timeout_s=5)
    time.sleep(0.2)
    out = exec_node(src, f"iperf3 -c {dst_ip} -t {seconds} -p {port} -J 2>&1", timeout_s=seconds + 10)
    m = re.search(r'"bits_per_second"\s*:\s*([\d\.]+)', out)
    if not m:
        return None, out
    bps = float(m.group(1))
    return bps / 1_000_000.0, out


def _node_has_ip(node: str, ip: str) -> bool:
    out = exec_node(node, f"ip -o -4 addr show 2>/dev/null | grep -w {shlex.quote(ip)} >/dev/null 2>&1; echo $?", timeout_s=4)
    return out.strip().endswith("0")


def resolve_node_by_ip(ip: str) -> Optional[str]:
    """
    Ánh xạ 1 hop IP (traceroute) -> tên node Mininet bằng cách dò interface IP.
    """
    priority = [
        "P1",
        "P2",
        "P3",
        "P4",
        "PE1",
        "PE2",
        "PE3",
        "CE1",
        "CE2",
        "CE3",
        "SPINE1",
        "SPINE2",
        "LEAF_WEB",
        "LEAF_DNS",
        "LEAF_DB",
    ] + [n for n in NODE_LIST if n not in {"P1","P2","P3","P4","PE1","PE2","PE3","CE1","CE2","CE3","SPINE1","SPINE2","LEAF_WEB","LEAF_DNS","LEAF_DB"}]
    for n in priority:
        try:
            if _node_has_ip(n, ip):
                return n
        except Exception:
            continue
    return None


def route_out_interface(node: str, dst_ip: str) -> str:
    out = exec_node(node, f"ip route get {shlex.quote(dst_ip)} 2>/dev/null | head -n 1", timeout_s=4)
    m = re.search(r"\\bdev\\s+(\\S+)", out)
    if not m:
        return ""
    return m.group(1).split("@", 1)[0]


def read_intf_bytes(node: str, intf: str) -> int:
    rx = exec_node(node, f"cat /sys/class/net/{shlex.quote(intf)}/statistics/rx_bytes 2>/dev/null || echo 0", timeout_s=3)
    tx = exec_node(node, f"cat /sys/class/net/{shlex.quote(intf)}/statistics/tx_bytes 2>/dev/null || echo 0", timeout_s=3)
    try:
        rxi = int(rx.strip().splitlines()[-1])
    except Exception:
        rxi = 0
    try:
        txi = int(tx.strip().splitlines()[-1])
    except Exception:
        txi = 0
    return rxi + txi


def iperf3_path_throughput(
    src: str,
    dst: str,
    dst_ip: str,
    seconds: int = 3,
    port: int = 5201,
    max_hops: int = 12,
) -> Tuple[List[str], List[float], Optional[float], str]:
    """
    Đo throughput end-to-end (iperf3) và ước lượng throughput theo từng thiết bị trên đường đi.
    Mỗi thiết bị: đo delta(rx+tx) trên interface dùng để route tới dst_ip trong thời gian iperf chạy.
    """
    hops, _ = traceroute_path(src, dst_ip, max_hops=max_hops)
    hop_nodes: List[str] = []
    for hip in hops:
        n = resolve_node_by_ip(hip)
        if n and (not hop_nodes or hop_nodes[-1] != n):
            hop_nodes.append(n)
    path_nodes = [src] + hop_nodes + [dst]

    out_if: List[str] = []
    for n in path_nodes[:-1]:
        out_if.append(route_out_interface(n, dst_ip))

    b0 = [read_intf_bytes(n, iface) if iface else 0 for n, iface in zip(path_nodes[:-1], out_if)]
    t0 = time.time()

    mbps, raw = iperf3_throughput_mbps(src, dst, dst_ip, seconds=seconds, port=port)

    t1 = time.time()
    b1 = [read_intf_bytes(n, iface) if iface else 0 for n, iface in zip(path_nodes[:-1], out_if)]
    dt_s = max(0.001, t1 - t0)
    perhop = [max(0.0, ((b1i - b0i) * 8.0) / dt_s / 1_000_000.0) for b0i, b1i in zip(b0, b1)]

    # thêm cột dst để bảng đẹp: dùng end-to-end throughput
    perhop_full = perhop + ([float(mbps)] if mbps is not None else [0.0])
    return path_nodes, perhop_full, mbps, raw


def save_iperf_table(title: str, src: str, dst: str, throughput_mbps: float, out_png: Path) -> None:
    """
    Xuất ảnh bảng throughput đơn giản (giống style case5).
    """
    fig, ax = plt.subplots(figsize=(8.8, 2.6))
    ax.axis("off")
    row1 = ["Path", src, dst]
    row2 = ["Throughput (Mbps)", f"{throughput_mbps:.2f}", f"{throughput_mbps:.2f}"]
    table = ax.table(
        cellText=[row1, row2],
        loc="center",
        cellLoc="center",
        colWidths=[0.22, 0.39, 0.39],
    )
    table.auto_set_font_size(False)
    table.set_fontsize(11)
    table.scale(1, 2.0)
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


def save_path_throughput_table(title: str, path_nodes: List[str], per_node_mbps: List[float], out_png: Path) -> None:
    n = len(path_nodes)
    per_node_mbps = (per_node_mbps + [0.0] * n)[:n]

    fig, ax = plt.subplots(figsize=(max(10, 1.5 * n), 3))
    ax.axis("off")
    row1 = ["Path"] + path_nodes
    row2 = ["Thông lượng (Mbps)"] + [f"{v:.2f}" for v in per_node_mbps]

    col_w = [0.14] + [max(0.08, min(0.12, 0.86 / max(1, n))) for _ in range(n)]
    table = ax.table(
        cellText=[row1, row2],
        loc="center",
        cellLoc="center",
        colWidths=col_w,
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
            stats, raw, lines = ping_test_stream(src, dst_ip, count=5, timeout_s=20)
            for ln in lines:
                # hiển thị từng gói tin như ping thường
                if ln.strip():
                    self._log(ln)
            if stats:
                self._log(f"[PING-STAT] loss={stats.loss_pct:.1f}% avg={stats.rtt_avg_ms:.3f}ms jitter={stats.rtt_mdev_ms:.3f}ms")
            else:
                self._log("[PING-STAT] Không parse được ping (có thể unreachable).")
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
            path_nodes, per_node, mbps, raw = iperf3_path_throughput(src, dst, dst_ip, seconds=3, port=5201, max_hops=12)
            if mbps is None:
                self._log("  iperf3 FAIL. Xem raw log.")
            else:
                self._log(f"  throughput={mbps:.2f} Mbps")
                ts = dt.datetime.now().strftime("%Y%m%d_%H%M%S")
                out_png = IMG_DIR / f"iperf_table_{src}_to_{dst}_{ts}.png"
                title = f"BẢNG ĐO THROUGHPUT (IPERF3) TỪ [{src.upper()}] ĐẾN [{dst.upper()}]"
                save_iperf_table(title, src, dst, mbps, out_png)
                self._log(f"  Saved: {out_png}")
                out_png2 = IMG_DIR / f"iperf_path_table_{src}_to_{dst}_{ts}.png"
                title2 = f"BẢNG ĐƯỜNG ĐI + THÔNG LƯỢNG (IPERF3) TỪ [{src.upper()}] ĐẾN [{dst.upper()}]"
                save_path_throughput_table(title2, path_nodes, per_node, out_png2)
                self._log(f"  Saved: {out_png2}")
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

            exec_node(src, "pkill -f 'ping ' 2>/dev/null || true", timeout_s=5)
            exec_node(src, f"nohup sh -c 'ping -i 0.02 -s 1400 {dst_ip} >/dev/null 2>&1' >/tmp/case1_ping.log 2>&1 &", timeout_s=5)

            def tx_bytes() -> int:
                out = exec_node(spine, "cat /sys/class/net/SPINE1-eth1/statistics/tx_bytes 2>/dev/null || echo 0", timeout_s=3)
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
                    # Không kill daemon (dễ làm mất trạng thái/config). Thay vào đó shutdown OSPF bằng vtysh.
                    exec_node(
                        spine,
                        'vtysh --vty_socket /tmp/SPINE1/run -c "conf t" -c "router ospf" -c "shutdown" -c "end" 2>/dev/null || true',
                        timeout_s=5,
                    )
                    self._log("  -> Đã shutdown OSPF trên SPINE1 tại t=5s")
                if t == off_end:
                    exec_node(
                        spine,
                        'vtysh --vty_socket /tmp/SPINE1/run -c "conf t" -c "router ospf" -c "no shutdown" -c "end" 2>/dev/null || true',
                        timeout_s=5,
                    )
                    self._log("  -> Đã no shutdown OSPF trên SPINE1 tại t=15s")

            exec_node(src, "pkill -f 'ping -i 0.02' 2>/dev/null || true", timeout_s=5)

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


