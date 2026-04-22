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
        self.enable_ospf = params.pop("enable_ospf", True)
        self.enable_ldp = params.pop("enable_ldp", False)
        super().__init__(name, **params)

    def config(self, **params):
        super().config(**params)

        # Bật forwarding
        sysctl(self, "net.ipv4.ip_forward", "1")

        # Bật ECMP hash L4 (phục vụ Leaf-Spine branch)
        sysctl(self, "net.ipv4.fib_multipath_hash_policy", "1")

        # Nếu có MPLS (P/PE) thì set platform_labels
        if self.enable_ldp:
            sysctl(self, "net.mpls.platform_labels", "100000")
            # bật MPLS input cho mọi interface (an toàn, không cần liệt kê trước)
            self.cmd("for i in $(ls /sys/class/net | grep -v lo); do sysctl -w net.mpls.conf.$i.input=1 >/dev/null 2>&1; done")

        # Chuẩn bị thư mục config FRR
        self.cmd(f"rm -rf {self.frr_conf_dir} && mkdir -p {self.frr_conf_dir}")
        self.cmd(f"chmod 777 {self.frr_conf_dir}")

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
            f"-f {self.frr_conf_dir}/zebra.conf -i {self.frr_conf_dir}/zebra.pid >/dev/null 2>&1"
        )
        if self.enable_ospf:
            self.cmd(
                f"{frr_bin}/ospfd -d -A 127.0.0.1 "
                f"-f {self.frr_conf_dir}/ospfd.conf -i {self.frr_conf_dir}/ospfd.pid >/dev/null 2>&1"
            )
        if self.enable_ldp:
            # ldpd cần cho MPLS/LDP
            self.cmd(
                f"{frr_bin}/ldpd -d -A 127.0.0.1 "
                f"-f {self.frr_conf_dir}/ldpd.conf -i {self.frr_conf_dir}/ldpd.pid >/dev/null 2>&1"
            )

    def vty(self, cmds: str) -> str:
        """
        Chạy batch lệnh vtysh.
        cmds: chuỗi lệnh có xuống dòng, ví dụ:
          conf t
          router ospf
          ...
        """
        safe = cmds.replace('"', '\\"')
        return self.cmd(f'vtysh -c "enable" -c "{safe}" 2>/dev/null')

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
    a_ip: str
    b: str
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
}

# Backbone + CE-PE (tối thiểu để OSPF+LDP chạy)
BACKBONE_LINKS: List[LinkIP] = [
    # P1-P2
    LinkIP("P1", "10.0.12.1/30", "P2", "10.0.12.2/30", "10.0.12.0/30"),
    # P1-PE1
    LinkIP("P1", "10.0.11.1/30", "PE1", "10.0.11.2/30", "10.0.11.0/30"),
    # P3-PE1
    LinkIP("P3", "10.0.13.1/30", "PE1", "10.0.13.2/30", "10.0.13.0/30"),
    # P1-P4 (diagonal)
    LinkIP("P1", "10.0.14.1/30", "P4", "10.0.14.2/30", "10.0.14.0/30"),
    # P3-P1 (extra)
    LinkIP("P3", "10.0.31.1/30", "P1", "10.0.31.2/30", "10.0.31.0/30"),
    # P3-P4
    LinkIP("P3", "10.0.34.1/30", "P4", "10.0.34.2/30", "10.0.34.0/30"),
    # P4-P2 (vertical)
    LinkIP("P4", "10.0.42.1/30", "P2", "10.0.42.2/30", "10.0.42.0/30"),
    # P2-P3 (diagonal shown as 10.0.23.4/30 label in drawio; dùng /30 riêng)
    LinkIP("P2", "10.0.23.5/30", "P3", "10.0.23.6/30", "10.0.23.4/30"),
    # P3-PE2
    LinkIP("P3", "10.0.23.1/30", "PE2", "10.0.23.2/30", "10.0.23.0/30"),
    # PE2-P4
    LinkIP("PE2", "10.0.24.1/30", "P4", "10.0.24.2/30", "10.0.24.0/30"),
    # P2-PE3
    LinkIP("P2", "10.0.27.1/30", "PE3", "10.0.27.2/30", "10.0.27.0/30"),
    # P4-PE3
    LinkIP("P4", "10.0.47.1/30", "PE3", "10.0.47.2/30", "10.0.47.0/30"),
]

CE_PE_LINKS: List[LinkIP] = [
    LinkIP("CE1", "10.0.101.1/30", "PE1", "10.0.101.2/30", "10.0.101.0/30"),
    LinkIP("CE2", "10.0.102.1/30", "PE2", "10.0.102.2/30", "10.0.102.0/30"),
    LinkIP("CE3", "10.0.103.1/30", "PE3", "10.0.103.2/30", "10.0.103.0/30"),
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

    # --- CE routers (chạy IP forwarding; branch3 cần ECMP -> vẫn bật ospf để sau mở rộng)
    CE1 = net.addHost("CE1", cls=FrrRouter, ip="0.0.0.0/32", enable_ospf=False, enable_ldp=False)
    CE2 = net.addHost("CE2", cls=FrrRouter, ip="0.0.0.0/32", enable_ospf=False, enable_ldp=False)
    CE3 = net.addHost("CE3", cls=FrrRouter, ip="0.0.0.0/32", enable_ospf=False, enable_ldp=False)

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
    spine1 = net.addHost("SPINE1", cls=FrrRouter, ip="0.0.0.0/32", enable_ospf=False, enable_ldp=False)
    spine2 = net.addHost("SPINE2", cls=FrrRouter, ip="0.0.0.0/32", enable_ospf=False, enable_ldp=False)
    leaf_web = net.addHost("LEAF_WEB", cls=FrrRouter, ip="0.0.0.0/32", enable_ospf=False, enable_ldp=False)
    leaf_dns = net.addHost("LEAF_DNS", cls=FrrRouter, ip="0.0.0.0/32", enable_ospf=False, enable_ldp=False)
    leaf_db = net.addHost("LEAF_DB", cls=FrrRouter, ip="0.0.0.0/32", enable_ospf=False, enable_ldp=False)
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
    for leaf in (leaf_web, leaf_dns, leaf_db):
        net.addLink(spine1, leaf, intfName1=None, intfName2=f"{leaf.name}-eth0", bw=1000)
        net.addLink(spine2, leaf, intfName1=None, intfName2=f"{leaf.name}-eth1", bw=1000)
    net.addLink(leaf_web, web1, intfName1="LEAF_WEB-eth2", intfName2="web1-eth0", bw=1000)
    net.addLink(leaf_web, web2, intfName1="LEAF_WEB-eth3", intfName2="web2-eth0", bw=1000)
    net.addLink(leaf_dns, dns1, intfName1="LEAF_DNS-eth2", intfName2="dns1-eth0", bw=1000)
    net.addLink(leaf_dns, dns2, intfName1="LEAF_DNS-eth3", intfName2="dns2-eth0", bw=1000)
    net.addLink(leaf_db, db1, intfName1="LEAF_DB-eth2", intfName2="db1-eth0", bw=1000)
    net.addLink(leaf_db, db2, intfName1="LEAF_DB-eth3", intfName2="db2-eth0", bw=1000)

    # --- Links: MPLS backbone (P/PE) + CE-PE (dùng Linux intfName tự động)
    # Kết nối vật lý backbone đúng theo list BACKBONE_LINKS + CE_PE_LINKS
    # Mỗi cặp node chỉ thêm 1 link.
    def link_pair(a: str, b: str) -> None:
        net.addLink(net.get(a), net.get(b), bw=1000)

    for l in BACKBONE_LINKS:
        link_pair(l.a, l.b)
    for l in CE_PE_LINKS:
        link_pair(l.a, l.b)

    net.build()
    info("*** Starting network...\n")
    net.start()

    # =========================
    # Basic IP config
    # =========================

    # Backbone MTU
    BACKBONE_MTU = 1512

    # Loopbacks P/PE
    for rname, lo_cidr in LOOPBACKS.items():
        add_lo(net.get(rname), lo_cidr)

    # Assign /30 to backbone links based on discovered interface pairs
    # Vì Mininet đặt tên interface theo thứ tự link, ta dò theo peer-name trong `ip -o link`.
    def set_link_ip(l: LinkIP, mtu: int) -> None:
        a = net.get(l.a)
        b = net.get(l.b)
        # tìm interface trên a nối tới b: dùng `ip -o link` và grep tên peer
        a_intf = a.cmd(f"ip -o link | awk -F': ' '{{print $2}}' | grep -E '^{l.a}-eth'").strip().splitlines()
        b_intf = b.cmd(f"ip -o link | awk -F': ' '{{print $2}}' | grep -E '^{l.b}-eth'").strip().splitlines()
        # heuristic: interface mới nhất thường ở cuối; gán lần lượt theo thứ tự tạo link
        if not a_intf or not b_intf:
            return
        a_if = a_intf[-1].strip()
        b_if = b_intf[-1].strip()
        add_ip(a, a_if, l.a_ip)
        add_ip(b, b_if, l.b_ip)
        set_intf_mtu(a, a_if, mtu)
        set_intf_mtu(b, b_if, mtu)

    # Backbone
    for l in BACKBONE_LINKS:
        set_link_ip(l, BACKBONE_MTU)
    # CE-PE links MTU không bắt buộc MPLS overhead, để mặc định hoặc set 1500
    for l in CE_PE_LINKS:
        set_link_ip(l, 1500)

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

    # Trunk VLANs giữa core/dist/access; access ports to hosts
    vlans = [10, 20, 30]
    # access ports
    ovs_set_port_vlan(acc_admin, "ACC_ADMIN-eth1", access_vlan=10)
    ovs_set_port_vlan(acc_admin, "ACC_ADMIN-eth2", access_vlan=10)
    ovs_set_port_vlan(acc_lab, "ACC_LAB-eth1", access_vlan=20)
    ovs_set_port_vlan(acc_lab, "ACC_LAB-eth2", access_vlan=20)
    ovs_set_port_vlan(acc_guest, "ACC_GUEST-eth1", access_vlan=30)
    ovs_set_port_vlan(acc_guest, "ACC_GUEST-eth2", access_vlan=30)
    # uplinks trunks (best-effort, set on all non-host ports)
    for sw in [core1, core2, dist1, dist2, acc_admin, acc_lab, acc_guest]:
        ports = sw.cmd("ovs-vsctl list-ports " + sw.name).strip().splitlines()
        for p in ports:
            if p.startswith(sw.name + "-eth"):
                # access ports đã tag -> trunk vẫn ok; set trunks cho tất cả uplinks
                ovs_set_port_vlan(sw, p, trunks=vlans)

    # Branch 3: gateways trên leaf (để server có default GW); CE3 sẽ route ra backbone sau
    add_ip(leaf_web, "LEAF_WEB-eth2", "10.3.10.1/24")
    add_ip(leaf_web, "LEAF_WEB-eth3", "10.3.10.1/24")
    add_ip(leaf_dns, "LEAF_DNS-eth2", "10.3.20.1/24")
    add_ip(leaf_dns, "LEAF_DNS-eth3", "10.3.20.1/24")
    add_ip(leaf_db, "LEAF_DB-eth2", "10.3.30.1/24")
    add_ip(leaf_db, "LEAF_DB-eth3", "10.3.30.1/24")

    # Route từ leaf -> CE3 (qua spine/agg): tạm thời default route về CE3 qua AGG_EDGE L2 domain.
    # Gán IP L3 giữa CE3 và leaf domain (mô phỏng đơn giản, vẫn giữ link vật lý).
    add_ip(CE3, "CE3-eth0", "10.3.0.1/24")
    add_ip(leaf_web, "LEAF_WEB-eth0", "10.3.0.11/24")
    add_ip(leaf_dns, "LEAF_DNS-eth0", "10.3.0.12/24")
    add_ip(leaf_db, "LEAF_DB-eth0", "10.3.0.13/24")
    for leaf in (leaf_web, leaf_dns, leaf_db):
        leaf.cmd("ip route del default 2>/dev/null || true")
        leaf.cmd("ip route add default via 10.3.0.1")

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
    def ospf_cfg(router: FrrRouter, rid: str) -> None:
        # enable ospf, passive-interface default; no passive on backbone eth; advertise lo + p2p
        # đơn giản: đưa toàn bộ interface eth* + lo vào OSPF area 0
        intfs = router.cmd("ip -o link | awk -F': ' '{print $2}' | grep -E '^" + router.name + r"-eth'").strip().splitlines()
        lines = ["conf t", "router ospf", f"ospf router-id {rid}", "passive-interface default"]
        for i in intfs:
            # bật OSPF trên backbone, bỏ passive
            lines += [f"no passive-interface {i}"]
        lines += ["exit"]
        for i in intfs:
            lines += [f"interface {i}", "ip ospf area 0", "exit"]
        lines += ["interface lo", "ip ospf area 0", "exit", "end", "write memory"]
        router.vty("\n".join(lines))

    for rname in ("P1", "P2", "P3", "P4", "PE1", "PE2", "PE3"):
        r = net.get(rname)
        rid = LOOPBACKS[rname].split("/")[0]
        ospf_cfg(r, rid)

    # LDP: bật trên các interface backbone eth* của P/PE
    def ldp_cfg(router: FrrRouter, lsr_id: str) -> None:
        intfs = router.cmd("ip -o link | awk -F': ' '{print $2}' | grep -E '^" + router.name + r"-eth'").strip().splitlines()
        lines = ["conf t", f"mpls ldp", f"router-id {lsr_id}", "exit"]
        for i in intfs:
            lines += [f"interface {i}", "mpls ldp", "exit"]
        lines += ["end", "write memory"]
        router.vty("\n".join(lines))

    for rname in ("P1", "P2", "P3", "P4", "PE1", "PE2", "PE3"):
        r = net.get(rname)
        lsr = LOOPBACKS[rname].split("/")[0]
        ldp_cfg(r, lsr)

    info("*** Chờ hội tụ OSPF + LDP (60s)...\n")
    time.sleep(60)

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

