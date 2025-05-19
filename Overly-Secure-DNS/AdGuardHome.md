# Self Hosted Overly Secure DNS: <br> AdGuardHome + dnscrypt-proxy + Tor + RAMFS
## 📑 Table of Contents

- [Introduction](#introduction)
- [Setting up Tor](#setting-up-tor)
- [Setting up dnscrypt-proxy](#setting-up-dnscrypt-proxy)
- [Setting up AdGuardHome](#setting-up-adguardhome)
- [Testing and Verifying DNSSEC + Tor](#testing-and-verifying-dnssec--tor-from-another-machine)
- [Bonus: DNS Prefetch Script](#bonus---prefetch-script)
- [Changing AGH Settings](#changing-agh-settings)
- [Conclusion](#-conclusion)

## Introduction

This guide will go over setting up a Debian server to run [AdGuardHome](https://github.com/AdguardTeam/AdGuardHome) (AGH) using [dnscrypt-proxy](https://github.com/DNSCrypt/dnscrypt-proxy) (dnscp) to query upstream servers over [Tor](https://www.torproject.org/about/history/), while both are running and logging in RAM only.

Before we start - This is an extreme setup for privacy. Yes, most of us can get by just simply setting Quad9 or similar as our upstream DNS and call it a day. They say they don't log, but there's always the question - *What if they do*? What started as an small idea for me grew into a larger project, learning about ramfs and dnscrypt-proxy (and several other failed solutions). This is not entirely practical for most, but that doesn't mean it's not fun to set up.

The main goal is to route DNS over Tor. Simple enough, right? Just set DNSPort 9053 in torrc and call it a day. Well, there's a couple issues there - DNSSEC usually doesn't work over Tor, and the exit node is able to see your request. So instead, what we'll do is set up dnscp to forward to a service like Cloudflare using DNS-Over-HTTPS (DoH), and route this all over the standard Tor SOCKS5 proxy on port 9050. We will now get DNSSEC from Cloudflare, Cloudflare will see the exit node IP instead of your real IP, and the exit node will not be able to see our query to Cloudflare thanks to DoH.

The biggest problem with this will be speed. If you've used Tor before, you know latency is a luxury. To help with this, we're going to cache our requests on this server using AGH. Over half of my DNS requests in my home get a response from the cache, so those will be resolved nearly instantly. Speaking of AGH, we're also going to use that to filter spam, tacking, malware, and more. Yes, dnscp can cache and filter, but I like being able to use a UI, so AGH is a nice frontend for it all.

To top it all off, we don't want anything saved to disk. What use is routing everything over Tor if the records can just be found in `/opt/AdGuardHome/query.log`? We're going to set up two ramfs mounts for AGH and dnscp to run from and write any cache or log files to. Typically the downside to ramfs is that everything is lost on reboot or other power loss, but this is exactly what we want! Yes, this means we lose the cache if the server is turned off, but how often are you restarting your DNS server?

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

## Setting up dnscrypt-proxy
Create a new user called "dnscrypt" to run the proxy service
```
sudo useradd --system --no-create-home --shell /usr/sbin/nologin dnscrypt
```
Create the ramfs dnscp will run from. We'll make the one for AGH now as well.
```
sudo mkdir -p /ramdisk-dnscrypt-proxy /ramdisk-adguardhome
```
Download the latest dnscp from [here](https://github.com/DNSCrypt/dnscrypt-proxy/releases/tag/2.1.8) and copy it to /opt/ or a folder of your choosing.
```
wget https://github.com/DNSCrypt/dnscrypt-proxy/releases/download/2.1.8/dnscrypt-proxy-linux_x86_64-2.1.8.tar.gz
tar -xzvf dnscrypt-proxy-linux_x86_64-2.1.8.tar.gz  
sudo cp -r linux-x86_64/ /opt/dnscrypt-proxy
```
Enter the new dnscp directory. Delete several files that we will not need.
```
cd /opt/dnscrypt-proxy
sudo rm example-*.txt
```
We are now going to create the config for dnscp
```
sudo nano dnscrypt-proxy.toml
```

This config will download a list of servers from the source at the bottom. If you want to use specific ones (Cloudflare and mullvad-doh) in my example, specify them at `server_names`, otherwise dnscp will pick from any the source downloads to `public-resolvers.md`
```
# dnscrypt-proxy Configuration for Privacy and Security (using DoH only)
server_names = ['cloudflare', 'mullvad-doh', 'adguard-dns-unfiltered-doh']  # Leave blank to use any from the sources

listen_addresses = ['127.0.0.1:5353']
force_tcp = true
proxy = 'socks5://127.0.0.1:9050'
timeout = 5000
netprobe_timeout = 60
netprobe_address = '9.9.9.9:53'
bootstrap_resolvers = ['1.1.1.1:53', '9.9.9.9:53']

log_level = 1
log_file = 'dnscrypt-proxy.log'

log_files_max_size = 10  # Maximum log files size in MB - Set to 0 for unlimited.

log_files_max_age = 7  # How long to keep backup files, in days

cache = false

dnscrypt_servers = false
doh_servers = true
require_dnssec = true
require_nolog = true
ipv6_servers = false

[sources.public-resolvers]
    urls = ['https://raw.githubusercontent.com/DNSCrypt/dnscrypt-resolvers/master/v3/public-resolvers.md', 'https://download.dnscrypt.info/resolvers-list/v3/public-resolvers.md']
    cache_file = 'public-resolvers.md'
    minisign_key = 'RWQf6LRCGA9i53mlYecO4IzT51TGPpvWucNSCh1CBM0QTaLn73Y7GFO3'
    refresh_delay = 73
    prefix = ''
```
Now lets set up dnscp to run from ramfs. First, add an entry to fstab. We'll do one for AGH while we're here too.
```
sudo nano /etc/fstab
```
Add the following at the bottom:
```
ramfs   /ramdisk-dnscrypt-proxy ramfs   defaults        0       0
ramfs   /ramdisk-adguardhome    ramfs   defaults        0       0
```
Refresh systemd and mount our new ramfs locations
```
sudo systemctl daemon-reload
sudo mount -a
```
We're now going to create a script. I'm going to save this at /home/user/pre-dnscrypt-proxy-start.sh
You can save it somewhere else, just adjust accordingly later.
```
sudo nano /home/user/pre-dnscrypt-proxy-start.sh
```
Paste the following:
```
#!/bin/bash
cp -r /opt/dnscrypt-proxy/* /ramdisk-dnscrypt-proxy/
chown -R dnscrypt:dnscrypt /ramdisk-dnscrypt-proxy/
chmod -R 770 /ramdisk-dnscrypt-proxy/
sleep 1
```
And change it's perms
```
sudo chmod 744 /home/user/pre-dnscrypt-proxy-start.sh
```
What does this do? Well, before dnscp is ran, the dnscp folder and all it's contents will be copied to the ramfs, then dnscrypt will be set as the owner with read/write/execute permissions. This allows us to run dnscp from ram and have any output saved to the ramfs instead of the disk.

Now we'll edit the dnscrypt-proxy.service to use the new options we have configured.
```
sudo nano /etc/systemd/system/dnscrypt-proxy.service
```
Replace with the following:
```
[Unit]
Description=Encrypted/authenticated DNS proxy

[Service]
StartLimitInterval=5
StartLimitBurst=10
User=dnscrypt
Group=dnscrypt
ExecStartPre=+/home/user/pre-dnscrypt-proxy-start.sh
ExecStart=/ramdisk-dnscrypt-proxy/dnscrypt-proxy "-config" "dnscrypt-proxy.toml"

ProtectSystem=full
NoNewPrivileges=true

# Avoid logging to system journal to maintain privacy
StandardOutput=null
StandardError=null

WorkingDirectory=/ramdisk-dnscrypt-proxy/

Restart=always
RestartSec=120

[Install]
WantedBy=multi-user.target
```
Looking at this, you can see the first thing that runs is our script we made earlier copying everything to /ramdisk-dnscrypt-proxy/. Next, dnscp is running from it's new location in the ramfs, using the ramfs as the working directory.

Next, reload the daemon, restart dnscp, and check it's status.
```
sudo systemctl daemon-reload
sudo systemctl enable --now dnscrypt-proxy.service
sudo systemctl status dnscrypt-proxy.service
```
If something isn't working, check the logs
```
sudo cat /ramdisk-dnscrypt-proxy/dnscrypt-proxy.log
```
You can also try running it manualy from the ramfs location and any errors should print to the cli
```
cd /ramdisk-dnscrypt-proxy/
./dnscrypt-proxy -config dnscrypt-proxy.toml
```

Otherwise, we can confirm DNS is working with DNSSEC by using `dig`.
```
dig @127.0.0.1 -p 5353 +dnssec dnssec-failed.org
```
Should return something that includes SERVFAIL, which is good
```
 ->>HEADER<<- opcode: QUERY, status: SERVFAIL, id: 31263
 ```
And doing
```
dig @127.0.0.1 -p 5353 +dnssec cloudflare.com
```
Should return more info than the previous. Look for the `ad` flag and an RRSIG line.
```
flags: qr rd ra ad
cloudflare.com.         221     IN      RRSIG   A 13 2 300 20250513132918 20250511112918 34505
```
If the first dig failed and the second produced good results, that means a few things: dnscp is working and listening on port 5353 like we configured, and DNSSEC is working. We'll verify Tor routing later, but if your query is taking any large amount of time (couple hundred ms+), it's safe to assume it's going through Tor.

## Setting up AdGuardHome
Create a new user called "adguard" to run the proxy service
```
sudo useradd --system --no-create-home --shell /usr/sbin/nologin adguard
```
AdGuardHome provides a [nice automated install command](https://github.com/AdguardTeam/AdGuardHome?tab=readme-ov-file#automated-install-linux-and-mac)
```
sudo apt install curl -y
curl -s -S -L https://raw.githubusercontent.com/AdguardTeam/AdGuardHome/master/scripts/install.sh | sh -s -- -v
```
Once completed, it should say AGH is now running at port 3000, which is used for the initial configuration. My server is IP is 10.1.2.26, so I'm going to put 10.1.2.26:3000 in my browser and I should see the AGH page.

We want to configure as much as possible this first time we're logged in. Just like dnscp, we're going to copy and run from ramfs on startup. This means that once AGH is up and running for good, any changes we make are being saved to the ramfs version and on the next boot, these changes will be lost. You will be able to manually copy some things back from ramfs to the default location, but it's just easier to get everything set as best as you can the first time around.

**The major settings to change:**

Settings - General (All optional)
```
Filter update > 12 hours
Logs > Enable and set rotation timeframe > Save
Stats > Enable and set retention timeframe > Save
```
Settings - DNS Settings
```
Upstream DNS servers > 127.0.0.1:5353 > Test > Apply
DNS server configuration > Enable DNSSEC, Blocking mode = NXDOMAIN > Save
DNS cache configuration > Cache size 41943040, Override minimum TTL 3600, Override maximum TTL 86400, enable  optimistic caching > Save
```
Settings - Encryption settings (Optional)
```
Encryption > Enable encryption and enter your cert info > Save configuration
```
Filter - DNS blocklists (Optional)
Enable whatever lists you want under here. I personally recommend:
```
AdGuard DNS filter
AdAway Default Blocklist
1Hosts (Lite)
AdGuard DNS Popup Hosts filter
AWAvenue Ads Rule
HaGeZi's Normal Blocklist
OISD Blocklist Small
Peter Lowe's Blocklist

Everything under the security section
```
That should be everything in the UI. Again, not impossible to change more later, just a pain.

Lets open the AdGuardHome.yaml file to edit a couple other things we can't change from the UI
```
sudo nano /opt/AdGuardHome/AdGuardHome.yaml
```
Find querylog, statistics, and log, and change their path to our ramfs
```
querylog:  
	dir_path: /ramdisk-adguardhome/data  
statistics:  
	dir_path: /ramdisk-adguardhome/data  
log:
	file: /ramdisk-adguardhome/data/log.log
```
We're now going to create a script for AGH that basically does what the previous script did for dnscp. I'm going to save this at /home/user/pre-ag-start.sh
```
sudo nano /home/user/pre-ag-start.sh
```
Paste the following:
```
#!/bin/bash
cp -r /opt/AdGuardHome/* /ramdisk-adguardhome/
chown -R adguard:adguard /ramdisk-adguardhome
chmod -R 770 /ramdisk-adguardhome
sleep 1
```
And change it's perms
```
sudo chmod 744 /home/user/pre-ag-start.sh
```
Just like the script for dnscp, this copies everything from the AGH folder to it's ramfs folder so it will run from ramfs and save any other files there.

Now we'll edit the AdGuardHome.service to use the new options we have configured.
```
sudo nano /etc/systemd/system/AdGuardHome.service
```
Replace with the following:
```
[Unit]
Description=AdGuard Home: Network-level blocker
After=syslog.target network-online.target

[Service]
StartLimitInterval=5
StartLimitBurst=10
User=adguard
Group=adguard
ExecStartPre=+/home/user/pre-ag-start.sh
ExecStart=/ramdisk-adguardhome/AdGuardHome "-s" "run"

AmbientCapabilities=CAP_NET_BIND_SERVICE
CapabilityBoundingSet=CAP_NET_BIND_SERVICE
ProtectSystem=full
NoNewPrivileges=true

# Avoid logging to system journal to maintain privacy
StandardOutput=null
StandardError=null

WorkingDirectory=/ramdisk-adguardhome

Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
```
Next, we need to delete some things from /opt/AdGuardHome that we don't need copied to the ramfs on boot
```
sudo rm -rf /opt/AdGuardHome/data/querylog*
sudo rm /opt/AdGuardHome/data/sessions.db
sudo rm /opt/AdGuardHome/data/stats.db
```
This should leave only the `filters` folder, which is fine, we want those copied over.

Next, reload the daemon, restart AGH, and check it’s status.

```
sudo systemctl daemon-reload
sudo systemctl restart AdGuardHome.service
sudo systemctl status AdGuardHome.service
```

If something isn’t working, check the logs
```
sudo cat /ramdisk-adguardhome/data/log.log
```
We can also check if it's working the same way we did with dnscp, just checking on port 53 for AGH
```
dig @127.0.0.1 -p 53 +dnssec dnssec-failed.org
dig @127.0.0.1 -p 53 +dnssec cloudflare.com
```
dnssec-failed should return a SERVFAIL, Cloudflare should return an `ad` flag and RRSIG

## Testing and verifying DNSSEC + Tor from another machine
At this point, everything should be running properly on the DNS server. So far all our testing has been done on the localhost, so time to move to a different machine to finish. This is also a good time to run some sort of packet capture so we can see how our DNS server is resolving our queries.

Let's use a different machine and run the same dig command we have been, except this time specify the IP of our DNS.
```
dig @10.1.2.26 +dnssec cloudflare.com
```
We got back:
```
; <<>> DiG 9.18.33-1~deb12u2-Debian <<>> @10.1.2.26 +dnssec cloudflare.com
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
;; SERVER: 10.1.2.26#53(10.1.2.26) (UDP)
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

## Bonus - Prefetch Script
AGH doesn't really have a prefetch configuration. Since we're going over Tor and latency is going to be an issue, we want to try and keep our cache loaded with responses so they don't need to go over Tor every single time. If you enabled optimistic caching, this should help. Setting the min and max TTL values to higher settings should help as well. We're going to help it even more by creating a script that performs it's own sort of prefetch, automatically querying the top X visited domains. 
```
sudo nano dns-prefetch.sh
```
```
#!/bin/bash

# Define locations
LOG_FILE="/ramdisk-adguardhome/data/querylog.json"
TOP_DOMAINS_FILE="/ramdisk-adguardhome/data/top-domains.json"
DNS_SERVER="127.0.0.1"

# Step 1: Extract top 500 queried domains, excluding filtered (blocked) ones and unwanted entries
jq -r 'select(.Result.IsFiltered != true) | .QH' "$LOG_FILE" \
  | grep -ivE '\.arpa$' \
  | grep -vE '127\.0\.0\.1' \
  | grep -vE 'duckduckgo\.com' \
  | sort | uniq -c | sort -nr | head -n 500 \
  | awk '{print $2}' \
  > "$TOP_DOMAINS_FILE"

# Step 2: Loop through the domain list and dig each one
while IFS= read -r domain; do
  dig @"$DNS_SERVER" "$domain" +short +timeout=2 > /dev/null 2>&1
  sleep 0.1  # Increase if you want a bigger delay between queries
done < "$TOP_DOMAINS_FILE"

exit 0
```
This reads the querylog file and starts by ignoring any domains that blocked by a filter since we don't need to refresh those. It then uses grep to remove any specified entries, like requests from localhost and to duckduckgo.com for example. It then sorts them, counts how many times the domain appears, sorts them again numerically in descending order, then keeps the top X requested domains, where X is defined at `head -n 500`; This would make a list of the top 500 requested domains. You can set this higher or lower. It then uses dig to the localhost to query that list of 500 at a rate of 10 per second, waiting a maximum of two seconds per query. This list is kept in the ramfs, but you could add a line to delete it when it's done if you wanted.

Once that's done, we'll set a cron job to do this every X hours. I'll choose every 4 hours.
```
sudo nano /etc/crontab
```
```
0	*/4	* * *	root	/home/user/dns-prefetch.sh
```
Since root is running this script, make sure root owns it and the perms are 744
```
sudo chown root:root /home/user/dns-prefetch.sh
sudo chmod 744 /home/user/dns-prefetch.sh
```
Reload cron
```
sudo service cron reload
```
So now every four hours, the server is going to refresh the top 500 queried domain names, helping make sure that more addresses are cached when our local devices send a request.

## Changing AGH Settings
As mentioned before, ADG is running in RAM now, along with it's config files, so any change you make like adding a filter list, adding a custom DNS rewrite, etc, will only be stored in the ramfs which is lost on reboot. To make this persistent, any changes you make need to be copied from the ramfs back to /opt/AdGuardHome so that they can be read on the next boot. For most settings, just copy `/ramdisk-adguardhome/AdGuardHome.yaml` to `/opt/AdGuardHome/AdGuardHome.yaml`. If you change any of the filter lists, either adding or removing, make sure the contents of the `filters` folder match between /opt/ and the ramfs.

## Conclusion
This setup is a deep dive into privacy-focused DNS infrastructure—far from plug-and-play, but built for those who care about control, anonymity, and learning how the pieces fit together.

By combining AGH, dnscp, and Tor, all running in RAM-only filesystems, we've created a DNS server that:

-   Encrypts your queries (via DoH)
    
-   Anonymizes the source (via Tor)
    
-   Validates authenticity (via DNSSEC)
    
-   Avoids persistent logs (via `ramfs`)
    
-   Filters unwanted content (via AGH)
    

This isn't meant for everyone, but the trade-off is a DNS resolver that leaks nothing, stores nothing, and shares nothing. It’s privacy on your own terms.

Thanks for reading.
