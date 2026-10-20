"""Vendor-aware network configuration parser.

Parses the raw text returned by ``display current-configuration`` (Huawei
VRP) / ``show running-config`` (Cisco IOS XE / Ruijie RGOS) into a
structured ``ParsedConfig`` dict that downstream AI tools can reason
about.

Design constraints:
- Pure Python, no new dependencies. Regex-driven parsing only.
- Best-effort: returns whatever it finds, even if some sections are
  malformed. Missing sections are ``[]`` rather than raising.
- Vendor-specific quirks are isolated in ``_parse_huawei`` /
  ``_parse_cisco`` and dispatched from the top-level ``parse_config``.

Coverage today:
- System info (sysname / hostname)
- VLANs (id + description)
- Interfaces (name, description, IP, mask, vlan, mode)
- BGP (local AS, router-id, peers with remote-AS, descriptions)
- OSPF (process id, router-id, networks, areas)
- ACLs (rules with action + protocol + addresses)
- Static routes

Each parser operates line-by-line and uses indentation to scope blocks.
Huawei uses 1-space indentation inside ``interface`` / ``bgp`` / ``ospf``
blocks; Cisco uses the same in modern IOS XE but the legacy
``router bgp 65000`` block can be flat.

This is intentionally not a full IOS parser — the goal is "give the AI
the structured shape it needs to answer questions", not "ship a perfect
config normalizer". Anything we miss falls back to the raw text the AI
can still grep.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field, asdict
from typing import Iterable


# ── Output dataclass ───────────────────────────────────────────────────────────

@dataclass
class InterfaceInfo:
    name: str = ""                       # e.g. "GigabitEthernet0/0/1" / "Vlanif10"
    description: str = ""
    ip_address: str = ""                 # dotted quad, or "" if L2 / unnumbered
    mask: str = ""                       # e.g. "255.255.255.0" or "24"
    vlan: str = ""                       # trunk/access vlan
    mode: str = ""                       # access / trunk / hybrid / routed
    shutdown: bool = False
    bandwidth: str = ""

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class BgpPeer:
    remote_as: str = ""
    remote_ip: str = ""
    description: str = ""
    # Common on Huawei / Cisco:
    # - "Established" / "Idle" / "Active" / "OpenConfirm" — only for live
    #   operational data (BGP summary), not for "show run". We record the
    #   configured commands only.
    password_set: bool = False


@dataclass
class BgpInfo:
    local_as: str = ""
    router_id: str = ""
    peers: list[BgpPeer] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "local_as": self.local_as,
            "router_id": self.router_id,
            "peers": [asdict(p) for p in self.peers],
        }


@dataclass
class OspfNetwork:
    ip: str = ""
    wildcard: str = ""                   # Huawei uses wildcard; Cisco "area X mask Y"
    area: str = ""


@dataclass
class OspfInfo:
    process_id: str = ""
    router_id: str = ""
    networks: list[OspfNetwork] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "process_id": self.process_id,
            "router_id": self.router_id,
            "networks": [asdict(n) for n in self.networks],
        }


@dataclass
class VlanInfo:
    vlan_id: str = ""
    description: str = ""

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class AclRule:
    rule_no: str = ""
    action: str = ""                     # permit | deny
    protocol: str = ""                   # ip | tcp | udp | icmp
    source: str = ""
    destination: str = ""

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class AclInfo:
    acl_no: str = ""
    acl_type: str = ""                   # basic | advanced | user | link
    rules: list[AclRule] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "acl_no": self.acl_no,
            "acl_type": self.acl_type,
            "rules": [asdict(r) for r in self.rules],
        }


@dataclass
class StaticRoute:
    destination: str = ""
    mask: str = ""
    next_hop: str = ""

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class ParsedConfig:
    vendor: str = ""
    hostname: str = ""
    interfaces: list[InterfaceInfo] = field(default_factory=list)
    vlans: list[VlanInfo] = field(default_factory=list)
    bgp: BgpInfo = field(default_factory=BgpInfo)
    ospf: list[OspfInfo] = field(default_factory=list)   # multiple processes possible
    acls: list[AclInfo] = field(default_factory=list)
    static_routes: list[StaticRoute] = field(default_factory=list)
    # Anything we couldn't classify — exposed so the AI can still grep
    # the original text instead of getting an empty answer.
    raw_line_count: int = 0

    def to_dict(self) -> dict:
        return {
            "vendor": self.vendor,
            "hostname": self.hostname,
            "raw_line_count": self.raw_line_count,
            "interfaces": [i.to_dict() for i in self.interfaces],
            "vlans": [v.to_dict() for v in self.vlans],
            "bgp": self.bgp.to_dict(),
            "ospf": [o.to_dict() for o in self.ospf],
            "acls": [a.to_dict() for a in self.acls],
            "static_routes": [r.to_dict() for r in self.static_routes],
        }


# ── Helpers ──────────────────────────────────────────────────────────────────

def _strip_comments(text: str) -> list[str]:
    """Drop comment lines and blank lines; return normalised stripped text."""
    out = []
    for line in text.splitlines():
        s = line.rstrip()
        if not s:
            continue
        if s.lstrip().startswith("#") or s.lstrip().startswith("!"):
            continue
        out.append(s)
    return out


# ── Huawei VRP ────────────────────────────────────────────────────────────────

_HW_SYSNAME = re.compile(r"^sysname\s+(\S+)", re.IGNORECASE)
_HW_VLAN = re.compile(r"^vlan\s+(?:batch\s+)?(\d+(?:\s+to\s+\d+)?)\s*$", re.IGNORECASE)
_HW_VLAN_DESC = re.compile(r"^\s+description\s+(.+)$", re.IGNORECASE)
_HW_IFACE = re.compile(r"^interface\s+(\S+.*?)\s*$", re.IGNORECASE)
_HW_IF_DESC = re.compile(r"^\s+description\s+(.+)$", re.IGNORECASE)
_HW_IF_IP = re.compile(r"^\s+ip\s+address\s+(\d+\.\d+\.\d+\.\d+)\s+(\S+)", re.IGNORECASE)
_HW_IF_VLAN = re.compile(r"^\s+port\s+(?:default\s+|trunk\s+allow-pass\s+)?vlan\s+(\d+(?:\s+\d+)*)", re.IGNORECASE)
_HW_IF_VLAN_TRUNK_ALLOW = re.compile(r"^\s+port\s+trunk\s+allow-pass\s+vlan\s+(\S+)", re.IGNORECASE)
_HW_IF_MODE_TRUNK = re.compile(r"^\s+port\s+link-type\s+trunk\b", re.IGNORECASE)
_HW_IF_MODE_ACCESS = re.compile(r"^\s+port\s+link-type\s+access\b", re.IGNORECASE)
_HW_IF_MODE_HYBRID = re.compile(r"^\s+port\s+link-type\s+hybrid\b", re.IGNORECASE)
_HW_IF_SHUTDOWN = re.compile(r"^\s+shutdown\b", re.IGNORECASE)
_HW_BGP = re.compile(r"^bgp\s+(\d+)", re.IGNORECASE)
_HW_BGP_RID = re.compile(r"^\s+router-id\s+(\d+\.\d+\.\d+\.\d+)", re.IGNORECASE)
_HW_BGP_PEER = re.compile(r"^\s+peer\s+(\S+)\s+as-number\s+(\d+)", re.IGNORECASE)
_HW_BGP_PEER_DESC = re.compile(r"^\s+peer\s+(\S+)\s+description\s+(.+)$", re.IGNORECASE)
_HW_BGP_PEER_PWD = re.compile(r"^\s+peer\s+(\S+)\s+password\b", re.IGNORECASE)
_HW_OSPF = re.compile(r"^ospf\s+(\d+)(?:\s+router-id\s+(\d+\.\d+\.\d+\.\d+))?", re.IGNORECASE)
_HW_OSPF_AREA = re.compile(r"^\s+area\s+(\S+)\s*$", re.IGNORECASE)
_HW_OSPF_AREA_NET = re.compile(r"^\s+area\s+(\S+)\s+network\s+(\d+\.\d+\.\d+\.\d+)\s+(\S+)", re.IGNORECASE)
_HW_OSPF_NET_PLAIN = re.compile(r"^\s+network\s+(\d+\.\d+\.\d+\.\d+)\s+(\S+)", re.IGNORECASE)
_HW_ROUTE_STATIC = re.compile(r"^ip\s+route-static\s+(\d+\.\d+\.\d+\.\d+)\s+(\S+)\s+(\d+\.\d+\.\d+\.\d+)", re.IGNORECASE)
_HW_ACL_BASIC = re.compile(r"^acl\s+(?:number\s+)?(\d+)\s*$", re.IGNORECASE)
_HW_ACL_RULE = re.compile(r"^\s+rule\s+(\d+)\s+(permit|deny)\s+(source|destination)\s+(\d+\.\d+\.\d+\.\d+)\s+(\d+\.\d+\.\d+\.\d+)", re.IGNORECASE)


def _parse_huawei(lines: list[str]) -> ParsedConfig:
    out = ParsedConfig(vendor="huawei_vrp")
    out.raw_line_count = len(lines)

    i = 0
    cur_iface: InterfaceInfo | None = None
    cur_vlan: VlanInfo | None = None
    cur_bgp_peer_idx: int | None = None
    cur_ospf: OspfInfo | None = None
    cur_acl: AclInfo | None = None

    def commit_iface():
        nonlocal cur_iface
        if cur_iface is not None and (cur_iface.name or cur_iface.description):
            out.interfaces.append(cur_iface)
        cur_iface = None

    def commit_vlan():
        nonlocal cur_vlan
        if cur_vlan is not None and cur_vlan.vlan_id:
            out.vlans.append(cur_vlan)
        cur_vlan = None

    while i < len(lines):
        line = lines[i]

        # system-level
        m = _HW_SYSNAME.match(line)
        if m:
            out.hostname = m.group(1)
            i += 1; continue

        # VLAN block
        m = _HW_VLAN.match(line)
        if m:
            commit_iface(); commit_vlan()
            ids = m.group(1).replace("to", " ")
            for vid in ids.split():
                cur_vlan = VlanInfo(vlan_id=vid)
            i += 1; continue

        if cur_vlan is not None:
            md = _HW_VLAN_DESC.match(line)
            if md:
                cur_vlan.description = md.group(1)
                i += 1; continue
            if not line.startswith(" "):
                commit_vlan()
                # fall through to re-parse line

        # Interface block
        m = _HW_IFACE.match(line)
        if m:
            commit_iface(); commit_vlan()
            cur_iface = InterfaceInfo(name=m.group(1))
            i += 1; continue

        if cur_iface is not None:
            md = _HW_IF_DESC.match(line)
            if md: cur_iface.description = md.group(1)
            mi = _HW_IF_IP.match(line)
            if mi: cur_iface.ip_address, cur_iface.mask = mi.group(1), mi.group(2)
            mv = _HW_IF_VLAN.match(line)
            if mv: cur_iface.vlan = mv.group(1)
            if _HW_IF_MODE_TRUNK.match(line): cur_iface.mode = "trunk"
            elif _HW_IF_MODE_ACCESS.match(line): cur_iface.mode = "access"
            elif _HW_IF_MODE_HYBRID.match(line): cur_iface.mode = "hybrid"
            if _HW_IF_SHUTDOWN.match(line): cur_iface.shutdown = True
            # End of block (line not indented = new top-level statement)
            if not line.startswith(" "):
                commit_iface()
                continue  # reprocess top-level
            i += 1; continue

        # BGP
        m = _HW_BGP.match(line)
        if m:
            commit_iface(); commit_vlan()
            out.bgp.local_as = m.group(1)
            cur_bgp_peer_idx = None
            i += 1; continue

        if out.bgp.local_as:
            mr = _HW_BGP_RID.match(line)
            if mr:
                out.bgp.router_id = mr.group(1)
                i += 1; continue
            mp = _HW_BGP_PEER.match(line)
            if mp:
                peer = BgpPeer(remote_ip=mp.group(1), remote_as=mp.group(2))
                out.bgp.peers.append(peer)
                cur_bgp_peer_idx = len(out.bgp.peers) - 1
                i += 1; continue
            mpd = _HW_BGP_PEER_DESC.match(line)
            if mpd and cur_bgp_peer_idx is not None:
                out.bgp.peers[cur_bgp_peer_idx].description = mpd.group(2).strip()
                i += 1; continue
            mpp = _HW_BGP_PEER_PWD.match(line)
            if mpp and cur_bgp_peer_idx is not None:
                out.bgp.peers[cur_bgp_peer_idx].password_set = True
                i += 1; continue

        # OSPF — block ends only when we hit another top-level statement.
        # Huawei's `area X` line is NOT indented (only `network Y` is),
        # so we can't use "no leading space" as a block-end signal.
        m = _HW_OSPF.match(line)
        if m:
            commit_iface(); commit_vlan()
            cur_ospf = OspfInfo(process_id=m.group(1), router_id=m.group(2) or "")
            out.ospf.append(cur_ospf)
            i += 1; continue

        if cur_ospf is not None:
            mn = _HW_OSPF_AREA_NET.match(line)
            if mn:
                cur_ospf.networks.append(OspfNetwork(area=mn.group(1), ip=mn.group(2), wildcard=mn.group(3)))
                i += 1; continue
            ma = _HW_OSPF_AREA.match(line)
            if ma:
                # remember current area for next 'network' line
                cur_ospf._current_area = ma.group(1)
                i += 1; continue
            mp = _HW_OSPF_NET_PLAIN.match(line)
            if mp:
                area = getattr(cur_ospf, "_current_area", "0.0.0.0")
                cur_ospf.networks.append(OspfNetwork(area=area, ip=mp.group(1), wildcard=mp.group(2)))
                i += 1; continue
            # Block ends only on a top-level statement (no leading space AND
            # not an "area ..." line which is mid-block on Huawei).
            if not line.startswith(" ") and not line.lstrip().startswith("area "):
                cur_ospf = None

        # Static routes
        m = _HW_ROUTE_STATIC.match(line)
        if m:
            out.static_routes.append(StaticRoute(destination=m.group(1), mask=m.group(2), next_hop=m.group(3)))
            i += 1; continue

        # ACL
        m = _HW_ACL_BASIC.match(line)
        if m:
            commit_iface(); commit_vlan()
            # Classify by number range (Huawei convention)
            n = int(m.group(1))
            kind = "basic" if n < 2000 else ("advanced" if n < 3000 else ("user" if n < 4000 else "link"))
            cur_acl = AclInfo(acl_no=m.group(1), acl_type=kind)
            out.acls.append(cur_acl)
            i += 1; continue

        if cur_acl is not None:
            mr = _HW_ACL_RULE.match(line)
            if mr:
                cur_acl.rules.append(AclRule(
                    rule_no=mr.group(1), action=mr.group(2).lower(),
                    protocol="ip", source=mr.group(4), destination=mr.group(5),
                ))
                i += 1; continue
            if not line.startswith(" "):
                cur_acl = None

        i += 1

    commit_iface(); commit_vlan()
    return out


# ── Cisco IOS XE / Ruijie RGOS ────────────────────────────────────────────────

_CS_HOSTNAME = re.compile(r"^hostname\s+(\S+)", re.IGNORECASE)
_CS_VLAN = re.compile(r"^vlan\s+(\d+)\s*$", re.IGNORECASE)
_CS_VLAN_NAME = re.compile(r"^\s+name\s+(.+)$", re.IGNORECASE)
_CS_IFACE = re.compile(r"^interface\s+(\S+.*?)\s*$", re.IGNORECASE)
_CS_IF_DESC = re.compile(r"^\s+description\s+(.+?)\s*$", re.IGNORECASE)
_CS_IF_IP = re.compile(r"^\s+ip\s+address\s+(\d+\.\d+\.\d+\.\d+)\s+(\d+\.\d+\.\d+\.\d+)", re.IGNORECASE)
_CS_IF_VLAN = re.compile(r"^\s+switchport\s+(?:access|trunk\s+allowed\s+vlan)\s+(?:vlan\s+)?(\d+)", re.IGNORECASE)
_CS_IF_MODE_TRUNK = re.compile(r"^\s+switchport\s+mode\s+trunk\b", re.IGNORECASE)
_CS_IF_MODE_ACCESS = re.compile(r"^\s+switchport\s+mode\s+access\b", re.IGNORECASE)
_CS_IF_SHUTDOWN = re.compile(r"^\s+shutdown\b", re.IGNORECASE)
_CS_BGP = re.compile(r"^router\s+bgp\s+(\d+)", re.IGNORECASE)
_CS_BGP_RID = re.compile(r"^\s+bgp\s+router-id\s+(\d+\.\d+\.\d+\.\d+)", re.IGNORECASE)
_CS_BGP_NEIGHBOR = re.compile(r"^\s+neighbor\s+(\S+)\s+remote-as\s+(\d+)", re.IGNORECASE)
_CS_BGP_NEIGHBOR_DESC = re.compile(r"^\s+neighbor\s+(\S+)\s+description\s+(.+)$", re.IGNORECASE)
_CS_BGP_NEIGHBOR_PWD = re.compile(r"^\s+neighbor\s+(\S+)\s+password\b", re.IGNORECASE)
_CS_OSPF = re.compile(r"^router\s+ospf\s+(\d+)", re.IGNORECASE)
_CS_OSPF_RID = re.compile(r"^\s+router-id\s+(\d+\.\d+\.\d+\.\d+)", re.IGNORECASE)
_CS_OSPF_NET = re.compile(r"^\s+network\s+(\d+\.\d+\.\d+\.\d+)\s+(\d+\.\d+\.\d+\.\d+)\s+area\s+(\S+)", re.IGNORECASE)
_CS_ROUTE_STATIC = re.compile(r"^ip\s+route\s+(\d+\.\d+\.\d+\.\d+)\s+(\d+\.\d+\.\d+\.\d+)\s+(\d+\.\d+\.\d+\.\d+)", re.IGNORECASE)
_CS_ACL_STD = re.compile(r"^access-list\s+(\d+)\s+(permit|deny)\s+(\d+\.\d+\.\d+\.\d+)(?:\s+(\d+\.\d+\.\d+\.\d+))?", re.IGNORECASE)
_CS_ACL_EXT = re.compile(r"^access-list\s+(\d+)\s+(permit|deny)\s+(\w+)\s+(\d+\.\d+\.\d+\.\d+)(?:\s+(\d+\.\d+\.\d+\.\d+))?(?:\s+(\d+\.\d+\.\d+\.\d+)(?:\s+(\d+\.\d+\.\d+\.\d+))?)?", re.IGNORECASE)


def _parse_cisco(lines: list[str]) -> ParsedConfig:
    out = ParsedConfig(vendor="cisco_ios_xe")
    out.raw_line_count = len(lines)

    i = 0
    cur_iface: InterfaceInfo | None = None
    cur_vlan: VlanInfo | None = None
    cur_ospf: OspfInfo | None = None

    def commit_iface():
        nonlocal cur_iface
        if cur_iface is not None and cur_iface.name:
            out.interfaces.append(cur_iface)
        cur_iface = None

    def commit_vlan():
        nonlocal cur_vlan
        if cur_vlan is not None and cur_vlan.vlan_id:
            out.vlans.append(cur_vlan)
        cur_vlan = None

    while i < len(lines):
        line = lines[i]

        m = _CS_HOSTNAME.match(line)
        if m:
            out.hostname = m.group(1); i += 1; continue

        m = _CS_VLAN.match(line)
        if m:
            commit_iface(); commit_vlan()
            cur_vlan = VlanInfo(vlan_id=m.group(1)); i += 1; continue
        if cur_vlan is not None:
            mn = _CS_VLAN_NAME.match(line)
            if mn:
                cur_vlan.description = mn.group(1).strip(); i += 1; continue
            if not line.startswith(" "):
                commit_vlan()

        m = _CS_IFACE.match(line)
        if m:
            commit_iface(); commit_vlan()
            cur_iface = InterfaceInfo(name=m.group(1)); i += 1; continue
        if cur_iface is not None:
            md = _CS_IF_DESC.match(line)
            if md: cur_iface.description = md.group(1).strip()
            mi = _CS_IF_IP.match(line)
            if mi: cur_iface.ip_address, cur_iface.mask = mi.group(1), mi.group(2)
            mv = _CS_IF_VLAN.match(line)
            if mv: cur_iface.vlan = mv.group(1)
            if _CS_IF_MODE_TRUNK.match(line): cur_iface.mode = "trunk"
            elif _CS_IF_MODE_ACCESS.match(line): cur_iface.mode = "access"
            if _CS_IF_SHUTDOWN.match(line): cur_iface.shutdown = True
            if not line.startswith(" "):
                commit_iface()
                continue
            i += 1; continue

        m = _CS_BGP.match(line)
        if m:
            commit_iface(); commit_vlan()
            out.bgp.local_as = m.group(1); i += 1; continue
        if out.bgp.local_as:
            handled = False
            mr = _CS_BGP_RID.match(line)
            if mr:
                out.bgp.router_id = mr.group(1); handled = True
            else:
                mn = _CS_BGP_NEIGHBOR.match(line)
                if mn:
                    out.bgp.peers.append(BgpPeer(remote_ip=mn.group(1), remote_as=mn.group(2)))
                    handled = True
                else:
                    mnd = _CS_BGP_NEIGHBOR_DESC.match(line)
                    if mnd:
                        for p in out.bgp.peers:
                            if p.remote_ip == mnd.group(1):
                                p.description = mnd.group(2).strip(); break
                        handled = True
                    else:
                        mnp = _CS_BGP_NEIGHBOR_PWD.match(line)
                        if mnp:
                            for p in out.bgp.peers:
                                if p.remote_ip == mnp.group(1):
                                    p.password_set = True; break
                            handled = True
            if handled:
                i += 1; continue

        m = _CS_OSPF.match(line)
        if m:
            commit_iface(); commit_vlan()
            cur_ospf = OspfInfo(process_id=m.group(1)); out.ospf.append(cur_ospf); i += 1; continue
        if cur_ospf is not None:
            mr = _CS_OSPF_RID.match(line)
            if mr: cur_ospf.router_id = mr.group(1)
            mn = _CS_OSPF_NET.match(line)
            if mn:
                cur_ospf.networks.append(OspfNetwork(ip=mn.group(1), wildcard=mn.group(2), area=mn.group(3)))
            if not line.startswith(" "):
                cur_ospf = None
            i += 1; continue

        m = _CS_ROUTE_STATIC.match(line)
        if m:
            out.static_routes.append(StaticRoute(destination=m.group(1), mask=m.group(2), next_hop=m.group(3)))
            i += 1; continue

        # ACLs (Cisco style: all access-list on one line)
        m = _CS_ACL_EXT.match(line)
        if m and len(m.groups()) >= 4 and not m.group(3).replace(".", "").isdigit():
            # Extended ACL: num, action, proto, src, src-mask, dst, dst-mask
            no = m.group(1)
            acl = next((a for a in out.acls if a.acl_no == no), None)
            if not acl:
                acl = AclInfo(acl_no=no, acl_type="extended")
                out.acls.append(acl)
            acl.rules.append(AclRule(
                rule_no=str(len(acl.rules) + 1), action=m.group(2).lower(),
                protocol=m.group(3).lower(),
                source=f"{m.group(4)}{' '+m.group(5) if m.group(5) else ''}",
                destination=f"{m.group(6) if m.group(6) else ''}{' '+m.group(7) if m.group(7) else ''}",
            ))
            i += 1; continue
        m = _CS_ACL_STD.match(line)
        if m:
            no = m.group(1)
            acl = next((a for a in out.acls if a.acl_no == no), None)
            if not acl:
                acl = AclInfo(acl_no=no, acl_type="standard")
                out.acls.append(acl)
            src = f"{m.group(3)}{' '+m.group(4) if m.group(4) else ''}"
            acl.rules.append(AclRule(
                rule_no=str(len(acl.rules) + 1), action=m.group(2).lower(),
                protocol="ip", source=src, destination="any",
            ))
            i += 1; continue

        i += 1

    commit_iface(); commit_vlan()
    return out


# ── Dispatch ──────────────────────────────────────────────────────────────────

def parse_config(text: str, vendor: str) -> dict:
    """Parse a device's configuration text and return a structured dict.

    Returns the dict form of ``ParsedConfig``. Empty sections are still
    present (so the AI sees consistent shape across devices).
    """
    lines = _strip_comments(text)
    if vendor.lower() in ("huawei_vrp", "huawei", "vrp"):
        parsed = _parse_huawei(lines)
    elif vendor.lower() in ("cisco_ios_xe", "cisco", "ios", "ios_xe", "ruijie_rgos", "ruijie"):
        parsed = _parse_cisco(lines)
    else:
        # Try Huawei as a best-effort default (more permissive about spaces)
        parsed = _parse_huawei(lines)
        parsed.vendor = vendor  # record what we got
    return parsed.to_dict()
