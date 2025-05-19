THIS IS MOSTLY COMPLETE BUT STILL A WORK IN PROGRESS <br>
THIS IS MOSTLY COMPLETE BUT STILL A WORK IN PROGRESS <br>
THIS IS MOSTLY COMPLETE BUT STILL A WORK IN PROGRESS
<br>
<br>
<br>
<br>
<br>

# Self Hosted Overly Secure DNS: <br> Technitium + Tor + RAMFS
## 📑 Table of Contents

- [Introduction](#introduction)
- [Setting up Tor](#setting-up-tor)
- [Setting up Technitium](#setting-up-technitium)
- [Testing and Verifying DNSSEC + Tor](#testing-and-verifying-dnssec--tor-from-another-machine)
- [Changing Tech Settings](#changing-tech-settings)
- [Conclusion](#-conclusion)

## Introduction

This guide will go over setting up a Debian server to run [Technitium](https://technitium.com/dns/) (Tech) to query upstream servers over [Tor](https://www.torproject.org/about/history/), while both are running and logging in RAM only.

The concepts here are basically the same as written in [my other guide](https://github.com/Cactus356/Guides/blob/main/Overly-Secure-DNS/AdGuardHome.md) using AdGuardHome, so go read that if you want.

In this guide, I'll be using a non-root user called `user`, using sudo when required for certain commands. If you only have a root user, log in and run the following to create a new user called `user` and give it sudo privileges:
```
useradd -m user
apt install sudo -y
usermod -aG sudo user
```

## Setting up Tor
We have to manually enable the repo on Debian. The instructions directly from the Tor Project can be found [here](https://support.torproject.org/apt/tor-deb-repo/).

Get our architecture and the name of our Debian release (This is `amd64` and `bookworm` in my case)

```
sudo apt install apt-transport-https -y
dpkg --print-architecture
lsb_release -c
```
Create a new source file
```
sudo nano /etc/apt/sources.list.d/tor.list
```
 And add the following two lines, replacing `bookworm` with your specific release
```
deb     [signed-by=/usr/share/keyrings/deb.torproject.org-keyring.gpg] https://deb.torproject.org/torproject.org bookworm main
deb-src [signed-by=/usr/share/keyrings/deb.torproject.org-keyring.gpg] https://deb.torproject.org/torproject.org bookworm main
```
Then add the gpg key used to sign the packages
```
sudo apt install gnupg -y
wget -qO- https://deb.torproject.org/torproject.org/A3C4F0F979CAA22CDBA8F512EE8CBC9E886DDD89.asc | gpg --dearmor | sudo tee /usr/share/keyrings/deb.torproject.org-keyring.gpg >/dev/null  
```
Finally, install Tor
```
sudo apt update  
sudo apt install tor deb.torproject.org-keyring -y
sudo systemctl enable --now tor
sudo systemctl status tor
```
If Tor is running successfully, move on.

## Setting up Technitium
Create a new user called "technitium" to run the proxy service
```
sudo useradd --system --no-create-home --shell /usr/sbin/nologin technitium
```
Create the ramfs technitium will run from. 
```
sudo mkdir -p /ramdisk-technitium/etc/dns /ramdisk-technitium/opt/technitium/dns /ramdisk-technitium/dotnet
```
Technitium provides an [easy install command](https://blog.technitium.com/2017/11/running-dns-server-on-ubuntu-linux.html)
```
sudo apt install curl -y
curl -sSL https://download.technitium.com/dns/install.sh | sudo bash
```
Once completed, it should say Tech is now running at port 5380, which is used for the web UI. My server is IP is 10.1.2.30, so I'm going to put 10.1.2.30:5380 in my browser and I should see the Technitium page.

We want to configure as much as possible this first time we're logged in. We're going to copy and run from ramfs on startup. This means that once Tech is up and running for good, any changes we make are being saved to the ramfs version and on the next boot, these changes will be lost. You will be able to manually copy some things back from ramfs to the default location, but it's just easier to get everything set as best as you can the first time around.

**The major settings to change:**

Zones
```
Add any custom zones or rewrites here
```
Settings
```
* General *
Enable DNSSEC
* Web service *
Enable HTTPS, use custom cert if available
* Optional Protocols *
Enable DoT, enable DoH, add cert
* Cache *
Uncheck save to disk, enable serve stale, cache size 50000, minimum TTL 600, auto prefetch eligibility 5 hits/hour
* Blocking *
Enable, type NXDOMAIN, add lists, set 12 hour update interval
* Proxy and forwarders *
SOCKS5 proxy, address 127.0.0.1, port 9050.
Forwarder = dns4torpnlfs2ifuz2s2yf3fc7rdmsbhm6rw75euj35pac6ap25zgqad.onion
DNS-Over-TCP
Forwarder Timeout = 5000
* Logging *
Log to file, log all queries, use local time, log folder path "logs"
Max log file days 10, uncheck in memory stats, max stat file days 365
```
Now lets set up Technitium to run from ramfs. First, add a couple entries to fstab.
```
sudo nano /etc/fstab
```
Add the following at the bottom:
```
ramfs /ramdisk-technitium/etc/dns ramfs defaults 0 0  
ramfs /ramdisk-technitium/opt/technitium/dns ramfs defaults 0 0  
ramfs /ramdisk-technitium/dotnet ramfs defaults 0 0
```
Refresh systemd and mount our new ramfs locations
```
sudo systemctl daemon-reload
sudo mount -a
```
We're now going to create a script. I'm going to save this at /home/user/pre-technitium-proxy-start.sh
You can save it somewhere else, just adjust accordingly later.
```
sudo nano /home/user/pre-technitium-start.sh
```
Paste the following:
```
#!/bin/bash  
cp -r /etc/dns/* /ramdisk-technitium/etc/dns/  
cp -r /opt/technitium/dns/* /ramdisk-technitium/opt/technitium/dns/  
cp -r /opt/dotnet/* /ramdisk-technitium/dotnet/  
cp -L /etc/letsencrypt/live/*.yourdomain.com/*.jnstack.com.pfx /ramdisk-technitium/*.yourdomain.com.pfx  
chown -R technitium:technitium /ramdisk-technitium/  
chmod -R 770 /ramdisk-technitium/  
sleep 1
```
And change it's perms
```
sudo chmod 744 /home/user/pre-technitium-start.sh
```
What does this do? Well, before Tech is ran, the Tech folder, it's config, dotnet, and your custom SSL cert if you have one (Deleted that line otherwise) will be copied to the ramfs, then technitium will be set as the owner with read/write/execute permissions. This allows us to run Tech from ram and have any output saved to the ramfs instead of the disk.

Now we'll edit the dns.service to use the new options we have configured.
```
sudo nano /etc/systemd/system/dns.service
```
Replace with the following:
```
[Unit]  
Description=Technitium DNS Server

[Service]  
Environment="DOTNET_CLI_TELEMETRY_OPTOUT=1"  
Environment="DOTNET_SKIP_FIRST_TIME_EXPERIENCE=1"  
Environment="DOTNET_EnableDiagnostics=0"  
Environment="DOTNET_NOLOGO=1"  
Environment="HOME=/ramdisk-technitium/dotnet"  
Environment="TMPDIR=/ramdisk-technitium/dotnet"  
Environment="DOTNET_ROOT=/ramdisk-technitium/dotnet"  
  
StartLimitInterval=5  
StartLimitBurst=10  
User=technitium  
Group=technitium  
ExecStartPre=+/home/user/pre-technitium-start.sh  
ExecStart=/ramdisk-technitium/dotnet/dotnet /ramdisk-technitium/opt/technitium/dns/DnsServerApp.dll /ramdisk-technitium/etc/dns

AmbientCapabilities=CAP_NET_BIND_SERVICE  
CapabilityBoundingSet=CAP_NET_BIND_SERVICE  
ProtectSystem=full  
NoNewPrivileges=true

# Avoid logging to system journal to maintain privacy  
StandardOutput=null  
StandardError=null

WorkingDirectory=/ramdisk-technitium/

Restart=always  
RestartSec=10  
KillSignal=SIGINT

[Install]  
WantedBy=multi-user.target
```
Looking at this, you can see the first thing that runs is our script we made earlier copying everything to /ramdisk-technitium/. Next, Tech is running from it's new location in the ramfs, using the ramfs as the working directory.

Next, we need to delete some things from /etc/dns that we don't need copied to the ramfs on boot
```
sudo rm -rf /etc/dns/stats/*  
sudo rm -rf /etc/dns/logs/*
```
Next, reload the daemon, restart dnscp, and check it's status.
```
sudo systemctl daemon-reload
sudo systemctl enable --now dnscrypt-proxy.service
sudo systemctl status dnscrypt-proxy.service
```
We can confirm DNS is working with DNSSEC by using `dig`.
```
dig @127.0.0.1 +dnssec dnssec-failed.org
```
Should return something that includes SERVFAIL, which is good
```
 ->>HEADER<<- opcode: QUERY, status: SERVFAIL, id: 31263
 ```
And doing
```
dig @127.0.0.1 +dnssec cloudflare.com
```
Should return more info than the previous. Look for the `ad` flag and an RRSIG line.
```
flags: qr rd ra ad
cloudflare.com.         221     IN      RRSIG   A 13 2 300 20250513132918 20250511112918 34505
```
If the first dig failed and the second produced good results, that means a few things: Tech is working like we configured, and DNSSEC is working. We'll verify Tor routing later, but if your query is taking any large amount of time (couple hundred ms+), it's safe to assume it's going through Tor.

## Testing and verifying DNSSEC + Tor from another machine
At this point, everything should be running properly on the DNS server. So far all our testing has been done on the localhost, so time to move to a different machine to finish. This is also a good time to run some sort of packet capture so we can see how our DNS server is resolving our queries.

Let's use a different machine and run the same dig command we have been, except this time specify the IP of our DNS.
```
dig @10.1.2.30 +dnssec cloudflare.com
```
We got back:
```
; <<>> DiG 9.18.33-1~deb12u2-Debian <<>> @10.1.2.30 +dnssec cloudflare.com
; (1 server found)
;; global options: +cmd
;; Got answer:
;; ->>HEADER<<- opcode: QUERY, status: NOERROR, id: 59875
;; flags: qr rd ra ad; QUERY: 1, ANSWER: 3, AUTHORITY: 0, ADDITIONAL: 1

;; OPT PSEUDOSECTION:
; EDNS: version: 0, flags: do; udp: 1232
;; QUESTION SECTION:
;cloudflare.com.                        IN      A

;; ANSWER SECTION:
cloudflare.com.         239     IN      A       104.16.133.229
cloudflare.com.         239     IN      A       104.16.132.229
cloudflare.com.         239     IN      RRSIG   A 13 2 300 20250514032434 20250512012434 34505 cloudflare.com. OJjbLp3nnBEXT5vmhzgwRp0zmgMgMpRVMEW4U3R2lNhQM6C43naXcMP0 UjAHAKsXbuf4GO4xKbieusQ4fTtWNg==

;; Query time: 988 msec
;; SERVER: 10.1.2.30#53(10.1.2.30) (UDP)
;; WHEN: Mon May 12 22:26:02 EDT 2025
;; MSG SIZE  rcvd: 185
```
Perfect, the `ad` flag as before along with a RRSIG response. Now, I can already guess by the 988 msec query time that this went over Tor, but we'll look at the packet capture in Wireshark to verify.

When I queried the DNS server for cloudflare.com, we can see it communicating a lot with 51.210.179.144

![](https://i.imgur.com/Yy4rnFo.png)


None of that traffic shows the DNS query that we made.
Compare that to simply using 9.9.9.9 as the upstream in AGH

![](https://i.imgur.com/BWSc8PW.png)


We can see that normally the query made to Quad9 directly is visible both in the request to Quad9 and their response back. So it appears we're encrypted, but are we using Tor?

When we do a [Tor relay search for 51.210.179.144](https://metrics.torproject.org/rs.html#search/51.210.179.144), we see it lists four different relays operating on different ports.
![enter image description here](https://i.imgur.com/iju3aft.png)

Using DoH to Quad9 would encrypt these requests, but Quad9 would still see the request from our IP. As we mentioned in the intro, DoH only = resolver sees our IP. Tor only = exit node sees our queries. DoH over Tor = Exit node sees an encrypted query to Quad9, who in turn sees the request from the exit node IP. Tor doesn't know what we're doing and Quad9 doesn't know who is doing it.

At this point, we are essentially done and you can start using your new DNS server!

## Changing Tech Settings
As mentioned before, Tech is running in RAM now, along with it's config files, so any change you make like adding a filter list, adding a custom DNS rewrite, etc, will only be stored in the ramfs which is lost on reboot. To make this persistent, any changes you make need to be copied from the ramfs back to `/etc/dns` so that they can be read on the next boot.
