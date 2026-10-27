"""Isolated read-only PNIO-DCP 1.2.0 discovery, matching the vendor scanner."""
from __future__ import annotations

import argparse
import contextlib
import importlib.metadata
import json
import socket
import sys


def interfaces():
    import psutil
    from pnio_dcp.l2socket.pcap_wrapper import PcapWrapper

    capture = PcapWrapper()
    devices = capture.get_all_devices()
    if devices is None:
        raise OSError("WinPcap/Npcap could not enumerate capture interfaces.")
    rows = []
    stats = psutil.net_if_stats()
    for name, addresses in psutil.net_if_addrs().items():
        if name not in stats or not stats[name].isup:
            continue
        for address in addresses:
            if address.family != socket.AF_INET or address.address.startswith("127."):
                continue
            capture_name = capture.get_device_name_from_ip(address.address)
            if not capture_name:
                continue
            device = next((item for item in devices if item.name == capture_name), None)
            rows.append({"name": name, "ip": address.address, "netmask": address.netmask,
                         "capture_name": capture_name, "description": device.description if device else "",
                         "virtual": "vmware" in (name + (device.description if device else "")).lower()})
    return rows


def scan(interface_ip, timeout_s):
    from pnio_dcp import DCP

    if interface_ip not in {row["ip"] for row in interfaces()}:
        raise ValueError("Choose an active capture interface returned by list_network_interfaces.")
    dcp = DCP(interface_ip)
    # identify_all otherwise spends the library's full 7s timeout on each idle read.
    dcp.default_timeout = 0.25
    try:
        devices = dcp.identify_all(timeout=timeout_s)
        return [{"name": device.name_of_station, "mac": device.MAC.upper(), "ip": device.IP,
                 "netmask": device.netmask, "gateway": device.gateway, "family": device.family}
                for device in devices]
    finally:
        # Version-pinned library has no public DCP.close; close its L2 capture handle.
        dcp._DCP__socket.close()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("operation", choices=["interfaces", "scan"])
    parser.add_argument("--interface-ip")
    parser.add_argument("--timeout", type=int, default=5)
    args = parser.parse_args()
    try:
        version = importlib.metadata.version("pnio-dcp")
        if version != "1.2.0":
            raise RuntimeError("Discovery requires the inspected vendor scanner's pnio-dcp 1.2.0.")
        if not 2 <= args.timeout <= 15:
            raise ValueError("Timeout must be between 2 and 15 seconds.")
        # Keep library diagnostics off the MCP/JSON data stream.
        with contextlib.redirect_stdout(sys.stderr):
            values = interfaces() if args.operation == "interfaces" else scan(args.interface_ip, args.timeout)
        print(json.dumps({"status": "passed", "backend": "pnio_dcp", "version": version,
                          "operation": args.operation, "items": values,
                          "device_configuration_changed": False, "tcp_connections_opened": False}, ensure_ascii=False))
        return 0
    except Exception as error:
        print(json.dumps({"status": "failed", "error": str(error), "error_type": type(error).__name__}))
        return 1


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    raise SystemExit(main())
