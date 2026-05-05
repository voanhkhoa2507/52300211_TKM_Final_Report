#!/usr/bin/env python3
"""
Phase 1 - topology.py

Mục tiêu:
- Dựng mô hình Metro Ethernet/MPLS trên Mininet theo `image/LOGIC.drawio`
- Kết nối 3 chi nhánh (Flat / 3-layer / Leaf-Spine) qua MPLS backbone (P/PE)
- Chuẩn bị nền cấu hình FRR: OSPF underlay + LDP trên backbone; VPLS sẽ cấu hình ở phase sau (hoặc mở rộng).

"""

from __future__ import annotations

import os
import sys
import time
import subprocess
import json
from dataclasses import dataclass
from typing import Dict, Iterable, List, Optional, Tuple

from mininet.net import Mininet
from mininet.node import Node, OVSSwitch
from mininet.link import TCLink
from mininet.cli import CLI
from mininet.log import setLogLevel, info


# =========================
# Helpers
# =========================

def dpid_hex(n: int) -> str:
    """Return 16-hex-digit datapath ID for OVS switches."""
    return f"{n:016x}"


_DPID_COUNTER = 1


def add_ovs_switch(net: Mininet, name: str, *, stp: bool = False) -> OVSSwitch:
    """Add OVSSwitch with explicit DPID (non-canonical names supported)."""
    global _DPID_COUNTER
    sw = net.addSwitch(
        name,
        cls=OVSSwitch,
        failMode="standalone",
        stp=stp,
        dpid=dpid_hex(_DPID_COUNTER),
    )
    _DPID_COUNTER += 1
    return sw


def sh(cmd: List[str], check: bool = False) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, check=check)


def cleanup_mininet() -> None:
    """Dọn rác để tránh node/link cũ làm bẩn lab."""
    # FRR daemon chạy trong netns nhưng cùng PID namespace.
    # Nếu một lần chạy trước không dọn sạch, có thể còn daemon "mồ côi" giữ socket/zserv,
    # dẫn tới tình trạng xem LDP/OSPF bị "lẫn" giữa các node.
    try:
        # Kill theo tên process để chắc chắn (lab chỉ chạy FRR trong Mininet).
        sh(["bash", "-lc", "sudo pkill -9 -x zebra 2>/dev/null || true"], check=False)
        sh(["bash", "-lc", "sudo pkill -9 -x ospfd 2>/dev/null || true"], check=False)
        sh(["bash", "-lc", "sudo pkill -9 -x ldpd  2>/dev/null || true"], check=False)
        # Một số distro còn có wrapper `frrinit.sh`/`watchfrr` (nếu có), cũng dọn luôn để tránh respawn.
        sh(["bash", "-lc", "sudo pkill -9 -x watchfrr 2>/dev/null || true"], check=False)
    except Exception:
        pass
    try:
        sh(["bash", "-lc", "sudo rm -rf /tmp/P[0-9] /tmp/PE[0-9] /tmp/CE[0-9] /tmp/SPINE* /tmp/LEAF_* 2>/dev/null || true"], check=False)
    except Exception:
        pass
    try:
        # Dọn socket runtime mặc định của FRR nếu hệ thống có service FRR cài sẵn.
        sh(["bash", "-lc", "sudo rm -rf /var/run/frr/* 2>/dev/null || true"], check=False)
    except Exception:
        pass
    try:
        sh(["sudo", "mn", "-c"], check=False)
    except Exception:
        pass


def ensure_mpls_kernel() -> None:
    """
    Bắt buộc: nạp module MPLS trước khi Mininet start.
    """
    for mod in ("mpls_router", "mpls_iptunnel"):
        sh(["sudo", "modprobe", mod], check=False)


def sysctl(node: Node, key: str, value: str) -> None:
    node.cmd(f"sysctl -w {key}={value} >/dev/null 2>&1")


def set_intf_mtu(node: Node, intf: str, mtu: int) -> None:
    node.cmd(f"ip link set dev {intf} mtu {mtu} >/dev/null 2>&1")


def add_ip(node: Node, intf: str, cidr: str) -> None:
    node.cmd(f"ip link set {intf} up")
    node.cmd(f"ip addr flush dev {intf}")
    # Dùng Mininet API để `dump`/Intf update đúng (tránh hiện None)
    try:
        node.setIP(cidr, intf=intf)
    except Exception:
        node.cmd(f"ip addr add {cidr} dev {intf}")


def add_lo(node: Node, cidr: str) -> None:
    node.cmd("ip link set lo up")
    # tránh trùng nếu chạy lại
    node.cmd(f"ip addr add {cidr} dev lo 2>/dev/null || true")


# =========================
# FRR Router Node
# =========================

class FrrRouter(Node):
    """
    Node chạy FRR. Có thể bật/tắt daemon tùy vai trò.
    - Backbone P/PE: zebra + ospfd + ldpd (MPLS)
    - CE (tuỳ chọn): zebra + ospfd (không MPLS)
    """

    def __init__(self, name: str, **params):
        self.frr_conf_dir = f"/tmp/{name}"
        self.frr_run_dir = f"/tmp/{name}/run"
        self.zebra_sock = f"/tmp/{name}/run/zserv.api"
        self.enable_ospf = params.pop("enable_ospf", True)
        self.enable_ldp = params.pop("enable_ldp", False)
        super().__init__(name, **params)

    def config(self, **params):
        super().config(**params)

        # Dọn tiến trình FRR cũ (nếu chạy lại topo khi chưa cleanup sạch).
        # Nếu còn daemon cũ dùng vty socket khác, lệnh `show mpls ldp ...` có thể trả về trạng thái "lẫn" gây hiểu nhầm.
        # Giết theo pidfile nếu tồn tại trước khi xoá thư mục.
        for pidf in ("zebra.pid", "ospfd.pid", "ldpd.pid"):
            self.cmd(
                f"test -f {self.frr_conf_dir}/{pidf} && "
                f"kill $(head -n 1 {self.frr_conf_dir}/{pidf}) >/dev/null 2>&1 || true"
            )

        # Dọn firewall để tránh chặn ICMP/forwarding do rule còn sót (lab/demo trước đó)
        # (an toàn cho mô hình Mininet; giúp ping liên chi nhánh không bị drop "mysterious")
        self.cmd("iptables -P INPUT ACCEPT 2>/dev/null || true")
        self.cmd("iptables -P FORWARD ACCEPT 2>/dev/null || true")
        self.cmd("iptables -P OUTPUT ACCEPT 2>/dev/null || true")
        self.cmd("iptables -F 2>/dev/null || true")
        self.cmd("iptables -t nat -F 2>/dev/null || true")
        self.cmd("iptables -t mangle -F 2>/dev/null || true")
        self.cmd("iptables -t raw -F 2>/dev/null || true")
        self.cmd("iptables -X 2>/dev/null || true")

        # Bật forwarding
        sysctl(self, "net.ipv4.ip_forward", "1")

        # Tắt rp_filter để tránh drop gói trong mô hình có ECMP/MPLS (đường đi và về khác nhau)
        # rp_filter strict thường làm ping liên chi nhánh bị rớt dù route đã có.
        sysctl(self, "net.ipv4.conf.all.rp_filter", "0")
        sysctl(self, "net.ipv4.conf.default.rp_filter", "0")

        # Bật ECMP hash L4 (phục vụ Leaf-Spine branch)
        sysctl(self, "net.ipv4.fib_multipath_hash_policy", "1")

        # Nếu có MPLS (P/PE) thì set platform_labels
        if self.enable_ldp:
            sysctl(self, "net.mpls.platform_labels", "100000")
            # Lưu ý: interface eth* có thể chưa tồn tại tại thời điểm config().
            # Ta sẽ bật MPLS input lần nữa sau khi net.start() (xem enable_mpls_inputs()).

        # Chuẩn bị thư mục config FRR
        self.cmd(f"rm -rf {self.frr_conf_dir} && mkdir -p {self.frr_conf_dir}")
        self.cmd(f"chmod 777 {self.frr_conf_dir}")
        self.cmd(f"mkdir -p {self.frr_run_dir} && chmod 777 {self.frr_run_dir}")

        # Bật daemon cần thiết qua file daemons
        daemons = [
            "zebra=yes",
            f"ospfd={'yes' if self.enable_ospf else 'no'}",
            "ospf6d=no",
            "bgpd=no",
            "ripd=no",
            "ripngd=no",
            f"ldpd={'yes' if self.enable_ldp else 'no'}",
            "pimd=no",
            "nhrpd=no",
            "eigrpd=no",
            "babeld=no",
            "sharpd=no",
            "pbrd=no",
            "bfdd=no",
            "fabricd=no",
            "vrrpd=no",
            "staticd=yes",
        ]
        with open(f"{self.frr_conf_dir}/daemons", "w", encoding="utf-8") as f:
            f.write("\n".join(daemons) + "\n")

        # vtysh.conf tối thiểu
        with open(f"{self.frr_conf_dir}/vtysh.conf", "w", encoding="utf-8") as f:
            f.write("service integrated-vtysh-config\n")

        # Base config cho các daemon
        base = f"hostname {self.name}\nlog stdout\nservice integrated-vtysh-config\n!\n"
        for cfg in ("zebra.conf", "ospfd.conf", "ldpd.conf", "staticd.conf"):
            with open(f"{self.frr_conf_dir}/{cfg}", "w", encoding="utf-8") as f:
                f.write(base)

        # Start FRR (dùng /usr/lib/frr nếu có; fallback /usr/libexec/frr)
        frr_bin = "/usr/lib/frr"
        if not os.path.isdir(frr_bin):
            frr_bin = "/usr/libexec/frr"

        # Start zebra
        self.cmd(
            f"{frr_bin}/zebra -d -A 127.0.0.1 "
            f"-z {self.zebra_sock} --vty_socket {self.frr_run_dir} "
            f"-f {self.frr_conf_dir}/zebra.conf -i {self.frr_conf_dir}/zebra.pid "
            f"> {self.frr_conf_dir}/zebra.log 2>&1"
        )
        if self.enable_ospf:
            self.cmd(
                f"{frr_bin}/ospfd -d -A 127.0.0.1 "
                f"-z {self.zebra_sock} --vty_socket {self.frr_run_dir} "
                f"-f {self.frr_conf_dir}/ospfd.conf -i {self.frr_conf_dir}/ospfd.pid "
                f"> {self.frr_conf_dir}/ospfd.log 2>&1"
            )
        if self.enable_ldp:
            # ldpd cần cho MPLS/LDP
            self.cmd(
                f"{frr_bin}/ldpd -d -A 127.0.0.1 "
                f"-z {self.zebra_sock} --vty_socket {self.frr_run_dir} "
                f"-f {self.frr_conf_dir}/ldpd.conf -i {self.frr_conf_dir}/ldpd.pid "
                f"> {self.frr_conf_dir}/ldpd.log 2>&1"
            )

    def vty(self, cmds: str) -> str:
        """
        Chạy batch lệnh vtysh.
        cmds: chuỗi lệnh có xuống dòng, ví dụ:
          conf t
          router ospf
          ...
        """
        # vtysh không xử lý tốt nhiều dòng trong 1 tham số `-c`,
        # nên tách từng dòng thành nhiều `-c` để đảm bảo apply được cấu hình.
        lines = [l.strip() for l in cmds.splitlines() if l.strip() and not l.strip().startswith("#")]
        if not lines:
            return ""
        parts: List[str] = ['vtysh', f'--vty_socket "{self.frr_run_dir}"', '-c', '"enable"']
        for l in lines:
            safe = l.replace('"', '\\"')
            parts += ['-c', f'"{safe}"']
        return self.cmd(" ".join(parts) + " 2>/dev/null")

    def terminate(self):
        # kill daemon nếu có
        for pidf in ("zebra.pid", "ospfd.pid", "ldpd.pid"):
            self.cmd(f"test -f {self.frr_conf_dir}/{pidf} && kill $(cat {self.frr_conf_dir}/{pidf}) >/dev/null 2>&1 || true")
        super().terminate()


# =========================
# IP plan (theo label trong drawio)
# =========================

@dataclass(frozen=True)
class LinkIP:
    a: str
    a_if: str
    a_ip: str
    b: str
    b_if: str
    b_ip: str
    subnet: str


LOOPBACKS: Dict[str, str] = {
    "P1": "10.255.0.1/32",
    "P2": "10.255.0.2/32",
    "P3": "10.255.0.3/32",
    "P4": "10.255.0.4/32",
    "PE1": "10.255.0.11/32",
    "PE2": "10.255.0.12/32",
    "PE3": "10.255.0.13/32",
    # Branch 3 router-id (Leaf/Spine/CE3)
    "CE3": "10.255.3.1/32",
    "SPINE1": "10.255.3.11/32",
    "SPINE2": "10.255.3.12/32",
    "LEAF_WEB": "10.255.3.21/32",
    "LEAF_DNS": "10.255.3.22/32",
    "LEAF_DB": "10.255.3.23/32",
}

# Backbone + CE-PE (tối thiểu để OSPF+LDP chạy)
BACKBONE_LINKS: List[LinkIP] = [
    # P1-P2
    LinkIP("P1", "P1-eth0", "10.0.12.1/30", "P2", "P2-eth0", "10.0.12.2/30", "10.0.12.0/30"),
    # P1-PE1
    LinkIP("P1", "P1-eth1", "10.0.11.1/30", "PE1", "PE1-eth0", "10.0.11.2/30", "10.0.11.0/30"),
    # P3-PE1
    LinkIP("P3", "P3-eth0", "10.0.13.1/30", "PE1", "PE1-eth1", "10.0.13.2/30", "10.0.13.0/30"),
    # P1-P4 (diagonal)
    LinkIP("P1", "P1-eth2", "10.0.14.1/30", "P4", "P4-eth0", "10.0.14.2/30", "10.0.14.0/30"),
    # P3-P1 (extra)
    LinkIP("P3", "P3-eth1", "10.0.31.1/30", "P1", "P1-eth3", "10.0.31.2/30", "10.0.31.0/30"),
    # P3-P4
    LinkIP("P3", "P3-eth2", "10.0.34.1/30", "P4", "P4-eth1", "10.0.34.2/30", "10.0.34.0/30"),
    # P4-P2 (vertical)
    LinkIP("P4", "P4-eth2", "10.0.42.1/30", "P2", "P2-eth1", "10.0.42.2/30", "10.0.42.0/30"),
    # P2-P3 (diagonal shown as 10.0.23.4/30 label in drawio; dùng /30 riêng)
    LinkIP("P2", "P2-eth2", "10.0.23.5/30", "P3", "P3-eth3", "10.0.23.6/30", "10.0.23.4/30"),
    # P3-PE2
    LinkIP("P3", "P3-eth4", "10.0.23.1/30", "PE2", "PE2-eth0", "10.0.23.2/30", "10.0.23.0/30"),
    # PE2-P4
    LinkIP("PE2", "PE2-eth1", "10.0.24.1/30", "P4", "P4-eth3", "10.0.24.2/30", "10.0.24.0/30"),
    # P2-PE3
    LinkIP("P2", "P2-eth3", "10.0.27.1/30", "PE3", "PE3-eth0", "10.0.27.2/30", "10.0.27.0/30"),
    # P4-PE3
    LinkIP("P4", "P4-eth4", "10.0.47.1/30", "PE3", "PE3-eth1", "10.0.47.2/30", "10.0.47.0/30"),
]

# Tập interface backbone (chỉ P-P và P-PE). Dùng để:
# - bật MPLS input đúng cổng backbone
# - bật LDP đúng cổng backbone (KHÔNG bật trên CE-PE)
BACKBONE_INTF: Dict[str, List[str]] = {}
for l in BACKBONE_LINKS:
    BACKBONE_INTF.setdefault(l.a, []).append(l.a_if)
    BACKBONE_INTF.setdefault(l.b, []).append(l.b_if)

CE_PE_LINKS: List[LinkIP] = [
    LinkIP("CE1", "CE1-eth1", "10.0.101.1/30", "PE1", "PE1-eth2", "10.0.101.2/30", "10.0.101.0/30"),
    LinkIP("CE2", "CE2-eth2", "10.0.102.1/30", "PE2", "PE2-eth2", "10.0.102.2/30", "10.0.102.0/30"),
    LinkIP("CE3", "CE3-eth1", "10.0.103.1/30", "PE3", "PE3-eth2", "10.0.103.2/30", "10.0.103.0/30"),
]


# =========================
# Build topology
# =========================

def build_net(start_cli: bool = False) -> Mininet:
    """
    Build + start net và trả Mininet object.
    Yêu cầu tool.py: gọi build_net() từ file này.
    """
    if os.geteuid() != 0:
        raise SystemExit("Hãy chạy bằng sudo: sudo python3 source/topology.py")

    ensure_mpls_kernel()
    cleanup_mininet()

    net = Mininet(controller=None, link=TCLink, autoSetMacs=True, build=False)

    # --- Backbone routers (P/PE chạy OSPF + LDP)
    P1 = net.addHost("P1", cls=FrrRouter, ip="0.0.0.0/32", enable_ospf=True, enable_ldp=True)
    P2 = net.addHost("P2", cls=FrrRouter, ip="0.0.0.0/32", enable_ospf=True, enable_ldp=True)
    P3 = net.addHost("P3", cls=FrrRouter, ip="0.0.0.0/32", enable_ospf=True, enable_ldp=True)
    P4 = net.addHost("P4", cls=FrrRouter, ip="0.0.0.0/32", enable_ospf=True, enable_ldp=True)
    PE1 = net.addHost("PE1", cls=FrrRouter, ip="0.0.0.0/32", enable_ospf=True, enable_ldp=True)
    PE2 = net.addHost("PE2", cls=FrrRouter, ip="0.0.0.0/32", enable_ospf=True, enable_ldp=True)
    PE3 = net.addHost("PE3", cls=FrrRouter, ip="0.0.0.0/32", enable_ospf=True, enable_ldp=True)

    # --- CE routers
    CE1 = net.addHost("CE1", cls=FrrRouter, ip="0.0.0.0/32", enable_ospf=False, enable_ldp=False)
    CE2 = net.addHost("CE2", cls=FrrRouter, ip="0.0.0.0/32", enable_ospf=False, enable_ldp=False)
    # CE3 tham gia OSPF trong Branch 3 để học route từ leaf/spine, nhưng KHÔNG chạy LDP
    CE3 = net.addHost("CE3", cls=FrrRouter, ip="0.0.0.0/32", enable_ospf=True, enable_ldp=False)

    # --- Branch 1 (Flat)
    sw_flat_a = add_ovs_switch(net, "SW_FLAT_A")
    sw_flat_b = add_ovs_switch(net, "SW_FLAT_B")
    h1 = net.addHost("host1", ip="10.1.0.11/24", defaultRoute="via 10.1.0.1")
    h2 = net.addHost("host2", ip="10.1.0.12/24", defaultRoute="via 10.1.0.1")
    h3 = net.addHost("host3", ip="10.1.0.13/24", defaultRoute="via 10.1.0.1")
    h4 = net.addHost("host4", ip="10.1.0.14/24", defaultRoute="via 10.1.0.1")

    # --- Branch 2 (3-layer)
    core1 = add_ovs_switch(net, "CORE_1", stp=True)
    core2 = add_ovs_switch(net, "CORE_2", stp=True)
    dist1 = add_ovs_switch(net, "DIST_1", stp=True)
    dist2 = add_ovs_switch(net, "DIST_2", stp=True)
    acc_admin = add_ovs_switch(net, "ACC_ADMIN", stp=True)
    acc_lab = add_ovs_switch(net, "ACC_LAB", stp=True)
    acc_guest = add_ovs_switch(net, "ACC_GUEST", stp=True)
    admin1 = net.addHost("admin1", ip="10.2.10.11/24", defaultRoute="via 10.2.10.1")
    admin2 = net.addHost("admin2", ip="10.2.10.12/24", defaultRoute="via 10.2.10.1")
    lab1 = net.addHost("lab1", ip="10.2.20.11/24", defaultRoute="via 10.2.20.1")
    lab2 = net.addHost("lab2", ip="10.2.20.12/24", defaultRoute="via 10.2.20.1")
    guest1 = net.addHost("guest1", ip="10.2.30.11/24", defaultRoute="via 10.2.30.1")
    guest2 = net.addHost("guest2", ip="10.2.30.12/24", defaultRoute="via 10.2.30.1")

    # --- Branch 3 (Leaf-Spine)
    agg = add_ovs_switch(net, "AGG_EDGE", stp=True)
    spine1 = net.addHost("SPINE1", cls=FrrRouter, ip="0.0.0.0/32", enable_ospf=True, enable_ldp=False)
    spine2 = net.addHost("SPINE2", cls=FrrRouter, ip="0.0.0.0/32", enable_ospf=True, enable_ldp=False)
    leaf_web = net.addHost("LEAF_WEB", cls=FrrRouter, ip="0.0.0.0/32", enable_ospf=True, enable_ldp=False)
    leaf_dns = net.addHost("LEAF_DNS", cls=FrrRouter, ip="0.0.0.0/32", enable_ospf=True, enable_ldp=False)
    leaf_db = net.addHost("LEAF_DB", cls=FrrRouter, ip="0.0.0.0/32", enable_ospf=True, enable_ldp=False)
    web1 = net.addHost("web1", ip="10.3.10.11/24", defaultRoute="via 10.3.10.1")
    web2 = net.addHost("web2", ip="10.3.10.12/24", defaultRoute="via 10.3.10.1")
    dns1 = net.addHost("dns1", ip="10.3.20.11/24", defaultRoute="via 10.3.20.1")
    dns2 = net.addHost("dns2", ip="10.3.20.12/24", defaultRoute="via 10.3.20.1")
    db1 = net.addHost("db1", ip="10.3.30.11/24", defaultRoute="via 10.3.30.1")
    db2 = net.addHost("db2", ip="10.3.30.12/24", defaultRoute="via 10.3.30.1")

    # --- Links: Branch 1
    net.addLink(CE1, sw_flat_a, intfName1="CE1-eth0", bw=1000)
    net.addLink(sw_flat_a, sw_flat_b, bw=1000)
    net.addLink(h1, sw_flat_a, bw=100)
    net.addLink(h2, sw_flat_a, bw=100)
    net.addLink(h3, sw_flat_b, bw=100)
    net.addLink(h4, sw_flat_b, bw=100)

    # --- Links: Branch 2 (theo hình: CE2 -> CORE1/CORE2; Core -> Dist; Dist -> Access; Access -> Hosts)
    net.addLink(CE2, core1, intfName1="CE2-eth0", bw=1000)
    net.addLink(CE2, core2, intfName1="CE2-eth1", bw=1000)
    net.addLink(core1, core2, bw=1000)
    for d in (dist1, dist2):
        net.addLink(core1, d, bw=1000)
        net.addLink(core2, d, bw=1000)
    for a in (acc_admin, acc_lab, acc_guest):
        net.addLink(dist1, a, bw=1000)
        net.addLink(dist2, a, bw=1000)
    net.addLink(admin1, acc_admin, bw=100)
    net.addLink(admin2, acc_admin, bw=100)
    net.addLink(lab1, acc_lab, bw=100)
    net.addLink(lab2, acc_lab, bw=100)
    net.addLink(guest1, acc_guest, bw=100)
    net.addLink(guest2, acc_guest, bw=100)

    # --- Links: Branch 3 (CE3 -> AGG; AGG -> Spines; Spines <-> Leafs; Leafs -> Servers)
    net.addLink(CE3, agg, intfName1="CE3-eth0", bw=1000)
    net.addLink(agg, spine1, intfName2="SPINE1-eth0", bw=1000)
    net.addLink(agg, spine2, intfName2="SPINE2-eth0", bw=1000)

    # Đặt interface spine<->leaf cố định để gán IP underlay/OSPF ổn định
    net.addLink(spine1, leaf_web, intfName1="SPINE1-eth1", intfName2="LEAF_WEB-eth0", bw=1000)
    net.addLink(spine2, leaf_web, intfName1="SPINE2-eth1", intfName2="LEAF_WEB-eth1", bw=1000)
    net.addLink(spine1, leaf_dns, intfName1="SPINE1-eth2", intfName2="LEAF_DNS-eth0", bw=1000)
    net.addLink(spine2, leaf_dns, intfName1="SPINE2-eth2", intfName2="LEAF_DNS-eth1", bw=1000)
    net.addLink(spine1, leaf_db, intfName1="SPINE1-eth3", intfName2="LEAF_DB-eth0", bw=1000)
    net.addLink(spine2, leaf_db, intfName1="SPINE2-eth3", intfName2="LEAF_DB-eth1", bw=1000)
    net.addLink(leaf_web, web1, intfName1="LEAF_WEB-eth2", intfName2="web1-eth0", bw=1000)
    net.addLink(leaf_web, web2, intfName1="LEAF_WEB-eth3", intfName2="web2-eth0", bw=1000)
    net.addLink(leaf_dns, dns1, intfName1="LEAF_DNS-eth2", intfName2="dns1-eth0", bw=1000)
    net.addLink(leaf_dns, dns2, intfName1="LEAF_DNS-eth3", intfName2="dns2-eth0", bw=1000)
    net.addLink(leaf_db, db1, intfName1="LEAF_DB-eth2", intfName2="db1-eth0", bw=1000)
    net.addLink(leaf_db, db2, intfName1="LEAF_DB-eth3", intfName2="db2-eth0", bw=1000)

    # --- Links: MPLS backbone (P/PE) + CE-PE (đặt intfName cố định để gán IP/MTU đúng)
    for l in BACKBONE_LINKS:
        net.addLink(net.get(l.a), net.get(l.b), intfName1=l.a_if, intfName2=l.b_if, bw=1000)
    for l in CE_PE_LINKS:
        net.addLink(net.get(l.a), net.get(l.b), intfName1=l.a_if, intfName2=l.b_if, bw=1000)

    net.build()
    info("*** Starting network...\n")
    net.start()

    # Xuất PID map để tool Phase 2 bám đúng instance đang chạy
    try:
        pid_map = {n.name: int(getattr(n, "pid", 0) or 0) for n in net.hosts + net.switches}
        with open("/tmp/tkm_mininet_pids.json", "w", encoding="utf-8") as f:
            json.dump(pid_map, f, indent=2, sort_keys=True)
        info("*** Wrote PID map: /tmp/tkm_mininet_pids.json\n")
    except Exception:
        pass

    # =========================
    # Basic IP config
    # =========================

    # Backbone MTU
    BACKBONE_MTU = 1512

    # Loopbacks (router-id OSPF/LDP): P/PE + Branch3 routers
    for rname, lo_cidr in LOOPBACKS.items():
        add_lo(net.get(rname), lo_cidr)

    # Gán IP + MTU backbone đúng theo danh sách link
    for l in BACKBONE_LINKS:
        add_ip(net.get(l.a), l.a_if, l.a_ip)
        add_ip(net.get(l.b), l.b_if, l.b_ip)
        set_intf_mtu(net.get(l.a), l.a_if, BACKBONE_MTU)
        set_intf_mtu(net.get(l.b), l.b_if, BACKBONE_MTU)
    for l in CE_PE_LINKS:
        add_ip(net.get(l.a), l.a_if, l.a_ip)
        add_ip(net.get(l.b), l.b_if, l.b_ip)

    # Sau khi đã có interface thật, bật MPLS input cho các router backbone (P/PE)
    def enable_mpls_inputs(router: FrrRouter) -> None:
        # chỉ bật trên cổng backbone (P-P, P-PE). Tránh bật MPLS/LDP lên CE-PE làm rối dataplane.
        intfs = BACKBONE_INTF.get(router.name, [])
        for i in intfs:
            sysctl(router, f"net.mpls.conf.{i}.input", "1")
            # Đồng thời tắt rp_filter theo từng interface để chắc chắn không bị strict filter
            sysctl(router, f"net.ipv4.conf.{i}.rp_filter", "0")

    for rname in ("P1", "P2", "P3", "P4", "PE1", "PE2", "PE3"):
        enable_mpls_inputs(net.get(rname))

    # Branch 1 gateway on CE1-eth0
    add_ip(CE1, "CE1-eth0", "10.1.0.1/24")

    # Branch 2: router-on-a-stick đơn giản trên CE2-eth0 (trunk VLAN 10/20/30)
    # Để đảm bảo chạy được trong lab, ta dùng sub-interface VLAN trên CE2-eth0
    CE2.cmd("ip link set CE2-eth0 up")
    for vid, gw in [(10, "10.2.10.1/24"), (20, "10.2.20.1/24"), (30, "10.2.30.1/24")]:
        CE2.cmd(f"ip link add link CE2-eth0 name CE2-eth0.{vid} type vlan id {vid} 2>/dev/null || true")
        CE2.cmd(f"ip link set CE2-eth0.{vid} up")
        CE2.cmd(f"ip addr add {gw} dev CE2-eth0.{vid} 2>/dev/null || true")

    # Cấu hình VLAN trên OVS: access ports untagged, uplinks trunk
    # Lưu ý: interface name của OVS port có dạng CORE_1-ethX...
    def ovs_set_port_vlan(sw: Node, port: str, access_vlan: Optional[int] = None, trunks: Optional[List[int]] = None) -> None:
        if access_vlan is not None:
            sw.cmd(f"ovs-vsctl set port {port} tag={access_vlan}")
        if trunks is not None:
            trunk_str = ",".join(str(v) for v in trunks)
            sw.cmd(f"ovs-vsctl set port {port} trunks={trunk_str}")

    # VLAN Branch2: cấu hình theo đúng port thực tế (dựa trên link host<->switch)
    vlans = [10, 20, 30]

    def set_access_vlan_by_peer(sw: Node, peer: str, vlan: int) -> None:
        # tìm port trên switch nối tới peer bằng ifconfig của peer
        # peer-eth0 sẽ nối tới sw-ethX
        # dùng mininet: interface phía switch luôn có dạng SWNAME-ethN
        ports = sw.cmd(f"ovs-vsctl list-ports {sw.name}").strip().splitlines()
        for p in ports:
            # check peer name trong output of 'ip link' không ổn; nên dùng cách đơn giản:
            # nếu peer có 1 interface eth0, ta dò link ở phía peer để lấy tên phía switch
            pass

    # cách chắc chắn: lấy tên interface phía switch từ phía host (peer) bằng lệnh 'ip -o link'
    def get_peer_switch_port(peer_node: Node, sw_name: str) -> Optional[str]:
        # peer_node-eth0@... -> ta chỉ cần xem route: "ip -o link" trong namespace của peer
        # Mininet đặt tên veth ở peer đúng "peer-ethX" và ở switch đúng "SWNAME-ethY"
        out = peer_node.cmd("ip -o link | awk -F': ' '{print $2}' | grep -E '^" + peer_node.name + r"-eth'").strip().splitlines()
        if not out:
            return None
        # lấy eth0 rồi hỏi mininet link map bằng cách đọc `ethtool -S` không được; dùng `ip link` trên switch tìm interface UP?
        # đơn giản nhất: trên switch, port nối host sẽ có MAC của host ở learned table sau ping; nhưng chưa ping.
        # Do mô hình branch2 cố định: admin1/admin2 chỉ nối ACC_ADMIN; lab* nối ACC_LAB; guest* nối ACC_GUEST
        # => ta tag ALL ports của ACC_ADMIN/ACC_LAB/ACC_GUEST trừ uplinks (2 port uplink) bằng VLAN tương ứng.
        return None

    def tag_access_switch(sw: Node, vlan: int) -> None:
        ports = sw.cmd(f"ovs-vsctl list-ports {sw.name}").strip().splitlines()
        for p in ports:
            # uplink giữa dist<->access là 2 port cuối (vì ta addLink dist1->a, dist2->a trước host links)
            # host ports là những port còn lại
            if p.startswith(sw.name + "-eth"):
                # bỏ qua 2 uplink: eth1 và eth2 thường là uplink; nhưng không chắc -> dùng heuristic theo số lượng port
                pass

    # Heuristic ổn định theo thứ tự addLink:
    # - dist1->access và dist2->access được tạo trước host->access links
    # => trên access switch: 2 port có số nhỏ nhất là uplink, còn lại là host ports
    def set_access_switch_ports(sw: Node, vlan: int) -> None:
        ports = [p for p in sw.cmd(f"ovs-vsctl list-ports {sw.name}").strip().splitlines() if p.startswith(sw.name + "-eth")]
        def eth_num(p: str) -> int:
            try:
                return int(p.split("-eth", 1)[1])
            except Exception:
                return 999
        ports_sorted = sorted(ports, key=eth_num)
        uplinks = set(ports_sorted[:2])
        for p in ports_sorted[2:]:
            ovs_set_port_vlan(sw, p, access_vlan=vlan)
        for p in uplinks:
            ovs_set_port_vlan(sw, p, trunks=vlans)

    set_access_switch_ports(acc_admin, 10)
    set_access_switch_ports(acc_lab, 20)
    set_access_switch_ports(acc_guest, 30)

    # trunk trên core/dist links + ports nối CE2 (trunk VLAN 10/20/30)
    for sw in [core1, core2, dist1, dist2]:
        ports = [p for p in sw.cmd(f"ovs-vsctl list-ports {sw.name}").strip().splitlines() if p.startswith(sw.name + "-eth")]
        for p in ports:
            ovs_set_port_vlan(sw, p, trunks=vlans)

    # Disable link CE2-eth1 để tránh loop L2 (CE2 đang trunk trên eth0)
    CE2.cmd("ip link set CE2-eth1 down 2>/dev/null || true")

    # Branch 3: gateways trên leaf (để server có default GW); CE3 sẽ route ra backbone sau
    # Leaf trong mô hình đang là router Linux, nên để 2 host cùng VLAN ping nhau
    # ta cần bridge L2 giữa các cổng host-facing trên mỗi leaf.
    def mk_bridge(leaf: Node, br: str, ports: List[str], gw_cidr: str) -> None:
        leaf.cmd(f"ip link add name {br} type bridge 2>/dev/null || true")
        leaf.cmd(f"ip link set {br} up")
        for p in ports:
            leaf.cmd(f"ip link set {p} up")
            leaf.cmd(f"ip addr flush dev {p}")
            leaf.cmd(f"ip link set {p} master {br}")
        leaf.cmd(f"ip addr flush dev {br}")
        leaf.cmd(f"ip addr add {gw_cidr} dev {br} 2>/dev/null || true")

    mk_bridge(leaf_web, "br-web", ["LEAF_WEB-eth2", "LEAF_WEB-eth3"], "10.3.10.1/24")
    mk_bridge(leaf_dns, "br-dns", ["LEAF_DNS-eth2", "LEAF_DNS-eth3"], "10.3.20.1/24")
    mk_bridge(leaf_db, "br-db", ["LEAF_DB-eth2", "LEAF_DB-eth3"], "10.3.30.1/24")

    # Underlay L3 cho Leaf-Spine (để ping chéo VLAN và ECMP hoạt động đúng)
    # - CE3 <-> Spines đi chung L2 qua AGG_EDGE: 10.3.255.0/24
    add_ip(CE3, "CE3-eth0", "10.3.255.1/24")
    add_ip(spine1, "SPINE1-eth0", "10.3.255.11/24")
    add_ip(spine2, "SPINE2-eth0", "10.3.255.12/24")

    # - Spine <-> Leaf: p2p /30
    add_ip(spine1, "SPINE1-eth1", "10.3.0.1/30")
    add_ip(leaf_web, "LEAF_WEB-eth0", "10.3.0.2/30")
    add_ip(spine2, "SPINE2-eth1", "10.3.0.5/30")
    add_ip(leaf_web, "LEAF_WEB-eth1", "10.3.0.6/30")

    add_ip(spine1, "SPINE1-eth2", "10.3.0.9/30")
    add_ip(leaf_dns, "LEAF_DNS-eth0", "10.3.0.10/30")
    add_ip(spine2, "SPINE2-eth2", "10.3.0.13/30")
    add_ip(leaf_dns, "LEAF_DNS-eth1", "10.3.0.14/30")

    add_ip(spine1, "SPINE1-eth3", "10.3.0.17/30")
    add_ip(leaf_db, "LEAF_DB-eth0", "10.3.0.18/30")
    add_ip(spine2, "SPINE2-eth3", "10.3.0.21/30")
    add_ip(leaf_db, "LEAF_DB-eth1", "10.3.0.22/30")

    # =========================
    # CE->PE routing (static)
    # =========================
    # CE default route to PE on /30
    CE1.cmd("ip route add default via 10.0.101.2 2>/dev/null || true")
    CE2.cmd("ip route add default via 10.0.102.2 2>/dev/null || true")
    CE3.cmd("ip route add default via 10.0.103.2 2>/dev/null || true")

    # PE routes back to customer LANs (tối thiểu để reachability; sau sẽ thay bằng VRF/VPLS)
    PE1.cmd("ip route add 10.1.0.0/24 via 10.0.101.1 2>/dev/null || true")
    PE2.cmd("ip route add 10.2.10.0/24 via 10.0.102.1 2>/dev/null || true")
    PE2.cmd("ip route add 10.2.20.0/24 via 10.0.102.1 2>/dev/null || true")
    PE2.cmd("ip route add 10.2.30.0/24 via 10.0.102.1 2>/dev/null || true")
    PE3.cmd("ip route add 10.3.0.0/24 via 10.0.103.1 2>/dev/null || true")
    PE3.cmd("ip route add 10.3.10.0/24 via 10.0.103.1 2>/dev/null || true")
    PE3.cmd("ip route add 10.3.20.0/24 via 10.0.103.1 2>/dev/null || true")
    PE3.cmd("ip route add 10.3.30.0/24 via 10.0.103.1 2>/dev/null || true")

    # =========================
    # OSPF underlay on backbone (P/PE)
    # =========================
    # Router-ID từ loopback /32, chạy area 0 trên tất cả eth + lo.
    def ospf_cfg(router: FrrRouter, rid: str, *, no_passive: Optional[List[str]] = None, extra_ifaces: Optional[List[str]] = None) -> None:
        # enable ospf, passive-interface default; no passive on backbone eth; advertise lo + p2p
        # đơn giản: đưa toàn bộ interface eth* + lo vào OSPF area 0
        raw = router.cmd(
            "ip -o link | awk -F': ' '{print $2}' | grep -E '^" + router.name + r"-eth'"
        ).strip().splitlines()
        # `ip -o link` thường trả dạng `eth0@if123`; FRR cần tên thật trước dấu '@'
        intfs = sorted({i.split('@', 1)[0].strip() for i in raw if i.strip()})
        if extra_ifaces:
            intfs = list(dict.fromkeys(intfs + extra_ifaces))
        lines = ["conf t", "router ospf", f"ospf router-id {rid}", "passive-interface default"]
        if no_passive is None:
            no_passive = intfs
        for i in no_passive:
            lines += [f"no passive-interface {i}"]

        # Tạm thời để liên chi nhánh ping thông: PE quảng bá static/connected vào underlay
        if router.name.startswith("PE"):
            # Lưu ý: các route về LAN khách hàng đang được thêm bằng `ip route add` (kernel),
            # nên cần redistribute kernel để backbone học được.
            lines += ["redistribute connected", "redistribute static", "redistribute kernel"]
        lines += ["exit"]
        for i in intfs:
            lines += [f"interface {i}", "ip ospf area 0", "exit"]
        lines += ["interface lo", "ip ospf area 0", "exit", "end", "write memory"]
        router.vty("\n".join(lines))

    for rname in ("P1", "P2", "P3", "P4", "PE1", "PE2", "PE3"):
        r = net.get(rname)
        rid = LOOPBACKS[rname].split("/")[0]
        ospf_cfg(r, rid)

    # OSPF trong Branch 3 (Leaf-Spine): bật trên underlay ports; bridge VLAN chỉ advertise (passive)
    # CE3 phải hình thành adjacency OSPF với PE3 (CE3-eth1) để học route liên chi nhánh
    ospf_cfg(net.get("CE3"), LOOPBACKS["CE3"].split("/")[0], no_passive=["CE3-eth0", "CE3-eth1"], extra_ifaces=[])
    ospf_cfg(net.get("SPINE1"), LOOPBACKS["SPINE1"].split("/")[0], no_passive=["SPINE1-eth0", "SPINE1-eth1", "SPINE1-eth2", "SPINE1-eth3"])
    ospf_cfg(net.get("SPINE2"), LOOPBACKS["SPINE2"].split("/")[0], no_passive=["SPINE2-eth0", "SPINE2-eth1", "SPINE2-eth2", "SPINE2-eth3"])
    ospf_cfg(net.get("LEAF_WEB"), LOOPBACKS["LEAF_WEB"].split("/")[0], no_passive=["LEAF_WEB-eth0", "LEAF_WEB-eth1"], extra_ifaces=["br-web"])
    ospf_cfg(net.get("LEAF_DNS"), LOOPBACKS["LEAF_DNS"].split("/")[0], no_passive=["LEAF_DNS-eth0", "LEAF_DNS-eth1"], extra_ifaces=["br-dns"])
    ospf_cfg(net.get("LEAF_DB"), LOOPBACKS["LEAF_DB"].split("/")[0], no_passive=["LEAF_DB-eth0", "LEAF_DB-eth1"], extra_ifaces=["br-db"])

    # LDP: bật trên các interface backbone eth* của P/PE
    def ldp_cfg(router: FrrRouter, lsr_id: str) -> None:
        # Chỉ enable LDP trên interface backbone (không enable trên CE-PE)
        intfs = sorted(set(BACKBONE_INTF.get(router.name, [])))
        # FRR 8.x: cấu hình LDP trong address-family ipv4 và enable trên interface
        lines = [
            "conf t",
            "mpls ldp",
            f"router-id {lsr_id}",
            "address-family ipv4",
            f"discovery transport-address {lsr_id}",
        ]
        # Một số phiên bản FRR coi `interface <ifname>` là vào interface-submode.
        # Để đảm bảo tất cả interface đều được apply (không bị "kẹt mode"),
        # ta luôn `exit` sau mỗi interface.
        for i in intfs:
            lines += [f"interface {i}", "exit"]
        lines += ["exit-address-family", "end", "write memory"]
        router.vty("\n".join(lines))

    for rname in ("P1", "P2", "P3", "P4", "PE1", "PE2", "PE3"):
        r = net.get(rname)
        lsr = LOOPBACKS[rname].split("/")[0]
        ldp_cfg(r, lsr)

    # =========================
    # VPLS demo (Linux bridge + GRETAP between PEs)
    # =========================
    # Bật bằng biến môi trường để tránh làm ảnh hưởng phần L3 đang chạy ổn.
    # Ví dụ:
    #   sudo ENABLE_VPLS=1 python3 source/topology.py
    def vpls_gretap_cfg() -> None:
        if os.environ.get("ENABLE_VPLS", "0") != "1":
            info("*** VPLS/GRETAP: disabled (set ENABLE_VPLS=1 to enable)\n")
            return

        info("*** VPLS/GRETAP: enabling full-mesh GRETAP + br-vpls on PE1/PE2/PE3...\n")

        pe_names = ["PE1", "PE2", "PE3"]
        pe_lo = {n: LOOPBACKS[n].split('/')[0] for n in pe_names}
        pe_vpls_ip = {"PE1": "192.168.200.1/24", "PE2": "192.168.200.2/24", "PE3": "192.168.200.3/24"}

        def mk_bridge(pe: FrrRouter) -> None:
            pe.cmd("ip link add br-vpls type bridge 2>/dev/null || true")
            pe.cmd("ip link set dev br-vpls type bridge stp_state 1 2>/dev/null || true")
            pe.cmd("ip link set br-vpls up")
            # gán 1 IP test lên bridge để bạn ping kiểm tra tunnel + học MAC (FDB)
            pe.cmd("ip addr flush dev br-vpls 2>/dev/null || true")
            pe.cmd(f"ip addr add {pe_vpls_ip[pe.name]} dev br-vpls 2>/dev/null || true")

        def mk_gretap(local_pe: FrrRouter, local_ip: str, remote_ip: str, ifname: str) -> None:
            local_pe.cmd(f"ip link del {ifname} 2>/dev/null || true")
            local_pe.cmd(
                f"ip link add {ifname} type gretap local {local_ip} remote {remote_ip} ttl 64 2>/dev/null || true"
            )
            local_pe.cmd(f"ip link set {ifname} up")
            local_pe.cmd(f"ip link set {ifname} master br-vpls")

        for a in pe_names:
            mk_bridge(net.get(a))

        pairs = [("PE1", "PE2"), ("PE1", "PE3"), ("PE2", "PE3")]
        for a, b in pairs:
            a_node = net.get(a)
            b_node = net.get(b)
            a_ip = pe_lo[a]
            b_ip = pe_lo[b]
            mk_gretap(a_node, a_ip, b_ip, f"gt_{a}_{b}")
            mk_gretap(b_node, b_ip, a_ip, f"gt_{b}_{a}")

        info(
            "*** VPLS/GRETAP ready. Test:\n"
            "    PE1 ip link show type gretap\n"
            "    PE1 ip addr show br-vpls\n"
            "    PE1 ping -c 3 192.168.200.2   # PE1->PE2 over VPLS\n"
            "    PE1 bridge fdb show br br-vpls\n"
        )

    vpls_gretap_cfg()

    info("*** Chờ hội tụ OSPF + LDP (45s)...\n")
    time.sleep(45)

    info("*** Topology Phase 1 ready.\n")
    if start_cli:
        CLI(net)
    return net


def run() -> None:
    net = build_net(start_cli=True)
    net.stop()
    cleanup_mininet()


if __name__ == "__main__":
    setLogLevel("info")
    run()

