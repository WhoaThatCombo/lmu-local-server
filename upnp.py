"""Minimal UPnP Internet Gateway Device client (standard library only).

Opens the server's ports on a home router so friends can connect over the internet without
manual port forwarding:

    gw = upnp.discover()
    gw.add_port_mapping(64298, "TCP", "LMU server HTTP")
    gw.external_ip()
    gw.delete_port_mapping(64298, "TCP")

Run `python upnp.py` to check whether your router answers.
"""

import ipaddress
import re
import socket
import time
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urljoin, urlparse
from urllib.request import Request, urlopen
from xml.etree import ElementTree
from xml.sax.saxutils import escape

SSDP_ADDR = ("239.255.255.250", 1900)
SEARCH_TARGETS = (
    "urn:schemas-upnp-org:device:InternetGatewayDevice:2",
    "urn:schemas-upnp-org:device:InternetGatewayDevice:1",
    "urn:schemas-upnp-org:service:WANIPConnection:1",
)
WAN_SERVICES = (
    "urn:schemas-upnp-org:service:WANIPConnection:2",
    "urn:schemas-upnp-org:service:WANIPConnection:1",
    "urn:schemas-upnp-org:service:WANPPPConnection:1",
)


class UPnPError(Exception):
    pass


def _local_ip_towards(host):
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect((host, 1900))
        return s.getsockname()[0]
    finally:
        s.close()


def _strip_ns(tag):
    return tag.rsplit("}", 1)[-1]


class Gateway:
    def __init__(self, location, control_url, service, local_ip):
        self.location = location
        self.control_url = control_url
        self.service = service
        self.local_ip = local_ip

    def __repr__(self):
        return f"Gateway({urlparse(self.location).hostname}, {self.service.rsplit(':', 2)[-2]}, lan {self.local_ip})"

    def _soap(self, action, args=()):
        body = ('<?xml version="1.0"?>'
                '<s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/" '
                's:encodingStyle="http://schemas.xmlsoap.org/soap/encoding/"><s:Body>'
                f'<u:{action} xmlns:u="{self.service}">'
                + "".join(f"<{k}>{escape(str(v))}</{k}>" for k, v in args)
                + f"</u:{action}></s:Body></s:Envelope>")
        req = Request(self.control_url, data=body.encode(), method="POST", headers={
            "Content-Type": 'text/xml; charset="utf-8"',
            "SOAPAction": f'"{self.service}#{action}"',
        })
        try:
            with urlopen(req, timeout=6) as r:
                xml = r.read()
        except HTTPError as e:
            text = e.read().decode(errors="ignore")
            code = re.search(r"<errorCode>(\d+)</errorCode>", text)
            desc = re.search(r"<errorDescription>([^<]*)</errorDescription>", text)
            raise UPnPError(f"{action} failed: {code.group(1) if code else e.code} "
                            f"{desc.group(1) if desc else e.reason}") from None
        except (URLError, OSError) as e:
            raise UPnPError(f"{action} failed: {e}") from None
        out = {}
        for el in ElementTree.fromstring(xml).iter():
            if len(el) == 0 and el.text is not None:
                out[_strip_ns(el.tag)] = el.text.strip()
        return out

    def external_ip(self):
        return self._soap("GetExternalIPAddress").get("NewExternalIPAddress", "")

    def add_port_mapping(self, port, protocol, description, internal_port=None, lease=0):
        args = [("NewRemoteHost", ""), ("NewExternalPort", port), ("NewProtocol", protocol),
                ("NewInternalPort", internal_port or port), ("NewInternalClient", self.local_ip),
                ("NewEnabled", 1), ("NewPortMappingDescription", description), ("NewLeaseDuration", lease)]
        try:
            self._soap("AddPortMapping", args)
        except UPnPError as e:
            # 725 OnlyPermanentLeasesSupported is the reverse case; some IGDv2 routers refuse lease 0.
            if lease == 0 and "725" not in str(e):
                args[-1] = ("NewLeaseDuration", 7 * 24 * 3600)
                self._soap("AddPortMapping", args)
            else:
                raise

    def delete_port_mapping(self, port, protocol):
        self._soap("DeletePortMapping", [("NewRemoteHost", ""), ("NewExternalPort", port), ("NewProtocol", protocol)])


def _gateway_from_description(location):
    with urlopen(location, timeout=4) as r:
        root = ElementTree.fromstring(r.read())
    base = location
    for el in root.iter():
        if _strip_ns(el.tag) == "URLBase" and el.text and el.text.strip():
            base = el.text.strip()
    found = {}
    for svc in root.iter():
        if _strip_ns(svc.tag) != "service":
            continue
        fields = {_strip_ns(c.tag): (c.text or "").strip() for c in svc}
        if fields.get("serviceType") in WAN_SERVICES and fields.get("controlURL"):
            found[fields["serviceType"]] = urljoin(base, fields["controlURL"])
    for service in WAN_SERVICES:
        if service in found:
            return Gateway(location, found[service], service, _local_ip_towards(urlparse(location).hostname))
    return None


CACHE_FILE = Path(__file__).with_name("upnp_gateway.txt")


def default_lan_ip():
    """IP of the adapter that carries the default route (skips VirtualBox/VPN adapters)."""
    try:
        return _local_ip_towards("10.255.255.255")
    except OSError:
        return "0.0.0.0"


def _ssdp_socket(lan_ip):
    """UDP socket for the search. It shares port 1900 with Windows' SSDP service so the router's
    replies get through Windows Firewall (replies to a random port are dropped there), and it is
    joined to the multicast group so the router's periodic NOTIFY announcements count too."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        s.bind(("0.0.0.0", SSDP_ADDR[1]))
    except OSError:
        s.bind(("0.0.0.0", 0))
    iface = socket.inet_aton(lan_ip) if lan_ip != "0.0.0.0" else socket.inet_aton("0.0.0.0")
    try:
        s.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP, socket.inet_aton(SSDP_ADDR[0]) + iface)
    except OSError:
        pass
    s.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 2)
    if lan_ip != "0.0.0.0":
        s.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_IF, iface)
    s.settimeout(0.5)
    return s


def discover(timeout=15.0, lan_ip=None, use_cache=True):
    """Find the router's UPnP internet gateway. Raises UPnPError if none answers.

    The search goes out of the default-route adapter (`lan_ip`); with VirtualBox, Hyper-V or VPN
    adapters present Windows would otherwise pick any of them for multicast. It is also sent
    straight to x.x.x.1. The last gateway found is cached in upnp_gateway.txt and tried first."""
    lan_ip = lan_ip or default_lan_ip()
    if use_cache and CACHE_FILE.exists():
        try:
            gw = _gateway_from_description(CACHE_FILE.read_text().strip())
            if gw and gw.local_ip == lan_ip:
                return gw
        except (OSError, ElementTree.ParseError):
            pass
    s = _ssdp_socket(lan_ip)
    targets = [SSDP_ADDR]
    if lan_ip != "0.0.0.0":
        targets.append((lan_ip.rsplit(".", 1)[0] + ".1", SSDP_ADDR[1]))
    seen, errors = set(), []
    try:
        deadline = time.monotonic() + timeout
        next_search = 0.0
        while time.monotonic() < deadline:
            if time.monotonic() >= next_search:
                next_search = time.monotonic() + 2.0
                for st in SEARCH_TARGETS:
                    msg = ("M-SEARCH * HTTP/1.1\r\nHOST: 239.255.255.250:1900\r\n"
                           f'MAN: "ssdp:discover"\r\nMX: 2\r\nST: {st}\r\n\r\n')
                    for target in targets:
                        try:
                            s.sendto(msg.encode(), target)
                        except OSError:
                            pass
            try:
                data, _addr = s.recvfrom(8192)
            except (socket.timeout, ConnectionResetError):
                continue
            if data.startswith(b"M-SEARCH"):
                continue
            m = re.search(rb"(?im)^location:\s*(\S+)", data)
            if not m:
                continue
            location = m.group(1).decode(errors="ignore")
            if location in seen:
                continue
            seen.add(location)
            try:
                gw = _gateway_from_description(location)
            except (OSError, ElementTree.ParseError) as e:
                errors.append(f"{location}: {e}")
                continue
            if gw:
                try:
                    CACHE_FILE.write_text(location)
                except OSError:
                    pass
                return gw
    finally:
        s.close()
    detail = f" ({'; '.join(errors)})" if errors else ""
    raise UPnPError("no UPnP internet gateway answered; check that UPnP is enabled on the router" + detail)


def is_public(ip):
    """False for private, CGNAT (100.64/10) and other non-routable addresses."""
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return False
    return addr.is_global and addr not in ipaddress.ip_network("100.64.0.0/10")


if __name__ == "__main__":
    try:
        gw = discover()
    except UPnPError as e:
        raise SystemExit(str(e))
    ext = gw.external_ip()
    print(gw)
    print("control URL:", gw.control_url)
    print("external IP:", ext, "" if is_public(ext) else "(not public: the router is behind another NAT/CGNAT, "
                                                        "so port mapping alone will not make you reachable)")
