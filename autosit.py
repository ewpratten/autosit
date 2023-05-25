"""
"""

import argparse
import sys
import ipaddress
import requests
import subprocess
from typing import Union

def look_up_ip_addr(hostname: str) -> ipaddress.IPv4Address:
    # Make a DoH request to Cloudflare to get the IP address
    response = requests.get(f"https://cloudflare-dns.com/dns-query?name={hostname}&type=A", headers={"accept": "application/dns-json"})
    
    # Handle errors
    response.raise_for_status()
    response_json = response.json()
    if response_json["Status"] != 0:
        raise RuntimeError(f"DNS lookup failed with status {response_json['Status']}")
    
    # Parse the response
    return ipaddress.IPv4Address(response_json["Answer"][0]["data"])

def needs_interface_recreation(local_ip: ipaddress.IPv4Address, remote_ip: ipaddress.IPv4Address, tun_name: str) -> bool:
    # If the interface doesn't exist, it needs to be created
    if not subprocess.run(["ip", "link", "show", "dev", tun_name], capture_output=True, text=True).returncode == 0:
        print(f"Interface `{tun_name}` does not exist")
        return True
    
    # If the addresses changed, it needs to be recreated
    with open(f"/tmp/autosit_{tun_name}", "r") as fp:
        old_local_ip = ipaddress.IPv4Address(fp.readline())
        old_remote_ip = ipaddress.IPv4Address(fp.readline())
        
    if local_ip != old_local_ip or remote_ip != old_remote_ip:
        print("Interface addresses changed")
        return True
    
    # Otherwise, it's healthy
    return False

def save_tunnel_addrs(local_ip: ipaddress.IPv4Address, remote_ip: ipaddress.IPv4Address, tun_name: str) -> None:
    print(f"Writing tunnel addresses to /tmp/autosit_{tun_name}")
    with open(f"/tmp/autosit_{tun_name}", "w") as fp:
        fp.write(f"{local_ip}\n{remote_ip}")
    

def main() -> int:
    # Handle program arguments
    ap = argparse.ArgumentParser(prog='autosit')
    ap.add_argument("local_hostname", help="DNS name of the local host")
    ap.add_argument("remote_hostname", help="DNS name of the remote host")    
    ap.add_argument("--tun-name", help="Name of the tunnel interface", default="autosit")
    ap.add_argument("--with-prefix", help="Assign an IP prefix to this interface", required=True, nargs="+")
    ap.add_argument("--with-ipv4-route", help="Add a route for this IPv4 prefix", type=ipaddress.IPv4Network, nargs="+")
    ap.add_argument("--with-ipv6-route", help="Add a route for this IPv6 prefix", type=ipaddress.IPv6Network, nargs="+")
    ap.add_argument("--ipv4-mode", help="Packet handling mode for IPv4", choices=["forward", "nat"], default="forward")
    ap.add_argument("--ipv6-mode", help="Packet handling mode for IPv6", choices=["forward", "nat"], default="forward")
    
    # ap.add_argument("--local-ipv6-address", help="IPv6 address for the local side of the tunnel (including CIDR prefix)", type=ipaddress.IPv6Network)
    # ap.add_argument("--remote-ipv6-address", help="IPv6 address for the local side of the tunnel (including CIDR prefix)", type=ipaddress.IPv6Network)
    # ap.add_argument("--local-ipv4-address", help="IPv4 address for the local side of the tunnel (including CIDR prefix)", type=ipaddress.IPv4Network)
    # ap.add_argument("--remote-ipv4-address", help="IPv4 address for the local side of the tunnel (including CIDR prefix)", type=ipaddress.IPv4Network)
    args = ap.parse_args()
    
    # Setting an IP address requires both the local and remote site being set
    # if args.local_ipv6_address and not args.remote_ipv6_address:
    #     ap.error("--local-ipv6-address requires --remote-ipv6-address")
    # if args.local_ipv4_address and not args.remote_ipv4_address:
    #     ap.error("--local-ipv4-address requires --remote-ipv4-address")
        
    # Resolve the local and remote IP addresses
    local_host_ip = look_up_ip_addr(args.local_hostname)
    remote_host_ip = look_up_ip_addr(args.remote_hostname)
    print(f"Local host IP address: {local_host_ip}" "\n" f"Remote host IP address: {remote_host_ip}")
    
    # If the interface needs to be re-created, do it
    if needs_interface_recreation(local_host_ip, remote_host_ip, args.tun_name):
        print(f"(Re)creating interface: {args.tun_name}")
        
        # Save the tunnel addresses
        save_tunnel_addrs(local_host_ip, remote_host_ip, args.tun_name)
        
        # Try bringing down the interface (this is allowed to fail)
        subprocess.run(["ip", "link", "del", args.tun_name], capture_output=True, text=True)
        
        # Create the interface
        print("Creating interface")
        subprocess.run(["ip", "tunnel", "add", args.tun_name, "mode", "sit", "remote", str(remote_host_ip), "local", str(local_host_ip), "mode", "any", "ttl", "255"], check=True)
        
        # Bring up the interface
        print("Bringing up interface")
        subprocess.run(["ip", "link", "set", "dev", args.tun_name, "up"], check=True)
        
        # Add every prefix to the interface
        for prefix in args.with_prefix:
            print(f"Adding prefix {prefix} to interface")
            subprocess.run(["ip", "addr", "add", str(prefix), "dev", args.tun_name], check=True)
            
        # Add appropriate routes
        if args.with_ipv4_route:
            for route in args.with_ipv4_route:
                print(f"Adding IPv4 route: {route}")
                subprocess.run(["ip", "route", "add", str(route), "dev", args.tun_name], check=True)
        if args.with_ipv6_route:
            for route in args.with_ipv6_route:
                print(f"Adding IPv6 route: {route}")
                subprocess.run(["ip", "route", "add", str(route), "dev", args.tun_name], check=True)
                
        # Set appropriate packet handling modes
        if args.ipv4_mode == "nat":
            print("Enabling IPv4 NAT")
            subprocess.run(["iptables", "-t", "nat", "-A", "POSTROUTING", "-o", args.tun_name, "-j", "MASQUERADE"], check=True)
        elif args.ipv4_mode == "forward":
            print("Enabling IPv4 forwarding")
            subprocess.run(["sysctl", "-w", "net.ipv4.ip_forward=1"], check=True)
            subprocess.run(["iptables", "-A", "FORWARD", "-i", args.tun_name, "-j", "ACCEPT"], check=True)
            subprocess.run(["iptables", "-A", "FORWARD", "-o", args.tun_name, "-j", "ACCEPT"], check=True)
        if args.ipv6_mode == "nat":
            print("Enabling IPv6 NAT")
            subprocess.run(["ip6tables", "-t", "nat", "-A", "POSTROUTING", "-o", args.tun_name, "-j", "MASQUERADE"], check=True)
        elif args.ipv6_mode == "forward":
            print("Enabling IPv6 forwarding")
            subprocess.run(["sysctl", "-w", "net.ipv6.conf.all.forwarding=1"], check=True)
            subprocess.run(["iptables", "-A", "FORWARD", "-i", args.tun_name, "-j", "ACCEPT"], check=True)
            subprocess.run(["iptables", "-A", "FORWARD", "-o", args.tun_name, "-j", "ACCEPT"], check=True)        
        
    else:
        print(f"Interface {args.tun_name} is up to healthy. Nothing to do.")

    return 0

if __name__ == "__main__":
    sys.exit(main())