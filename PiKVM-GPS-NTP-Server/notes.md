## Goal
Add a PPS enabled GPS module to my existing PiKVM (RPi4 4GB) and use chrony to create an NTP server.

GT-U7 GPS Module: [Amazon Link](https://www.amazon.com/dp/B08R5K4D4H)
Antenna With Adapter: [Amazon Link](https://www.amazon.com/dp/B083D59N55)

---

**Pin Layout**
Several pins were already in use between a fan, a display with two buttons, and the KVM ATX controller. I connected the GPS via the following pins:

VCC ------------> Pin 2 (5V)

GND -----------> Pin 20 (GND)

TXD ------------> Pin 10 (UARTRX)

RXD ------------> Pin 8 (UARTTX)

PPS -------------> Pin 12 (GPIO18)

---

**Set the PiKVM to read/write and install packages**

    rw
    pacman -S --noconfirm gpsd pps-tools chrony jq tcpdump

---

**/boot/config.txt**

    # See /boot/overlays/README for all available options
    initramfs initramfs-linux.img followkernel
    
    hdmi_force_hotplug=1
    gpu_mem=128
    enable_uart=1
    dtoverlay=tc358743
    dtoverlay=disable-bt
    dtoverlay=dwc2,dr_mode=peripheral
    dtparam=i2c_arm=on
    
    dtoverlay=cma,cma-128
    dtoverlay=pps-gpio,gpiopin=18
    init_uart_baud=115200

---

**/boot/cmdline.txt**

    root=LABEL=PIROOT ro rootwait console=tty1 fsck.repair=yes audit=0 cfg80211.ieee80211_regdom=US

---

**Disable login service using the UART**

    systemctl stop serial-getty@ttyAMA0.service
    systemctl disable serial-getty@ttyAMA0.service

---

**REBOOT and set PiKVM to read/write**

---

**Verify the Pi sees both the tty and pps devices**

    ls -l /dev/ttyA* /dev/pps*

Should output something like

    [root@pikvm ~]# ls -l /dev/ttyA* /dev/pps*
    crw-rw---- 1 root root 250,  0 Jul 24 13:52 /dev/pps0
    crw-rw---- 1 root uucp 204, 64 Sep  3 10:29 /dev/ttyAMA0

If your tty device name is different, change it in the disable login service above

---

**Change baud rate**

    ubxtool -S 115200 -f /dev/ttyAMA0 -P 14
    stty -F /dev/ttyAMA0 115200 cs8 -cstopb -parenb raw -echo

---

**Verify baud rate**

    stty -F /dev/ttyAMA0

Should output something like

    [root@pikvm ~]# stty -F /dev/ttyAMA0
    speed 115200 baud; line = 0;
    intr = <undef>; quit = <undef>; erase = <undef>; kill = <undef>; eof = <undef>; start = <undef>; stop = <undef>;
    susp = <undef>; rprnt = <undef>; werase = <undef>; lnext = <undef>; discard = <undef>; min = 0; time = 0;
    -brkint -icrnl -imaxbel
    -opost -onlcr
    -isig -icanon -iexten -echo -echoe -echok -echoctl -echoke

---

**/etc/default/gpsd**

    # Default settings for gpsd.
    START_DAEMON="true"
    GPSD_OPTIONS="-n"
    DEVICES="/dev/ttyAMA0 /dev/pps0"
    USBAUTO="true"
---

**Make sure chrony and gpsd are enabled**

    systemctl enable chronyd
    systemctl enable gpsd.socket

---

**REBOOT and set PiKVM to read/write**

---

**PPS Test**

    [root@pikvm ~]# ppstest /dev/pps0
    trying PPS source "/dev/pps0"
    found PPS source "/dev/pps0"
    ok, found 1 source(s), now start fetching data...
    source 0 - assert 1788478801.999999418, sequence: 33252 - clear  0.000000000, sequence: 0
    source 0 - assert 1788478802.999998988, sequence: 33253 - clear  0.000000000, sequence: 0
    source 0 - assert 1788478803.999999022, sequence: 33254 - clear  0.000000000, sequence: 0
    source 0 - assert 1788478805.000000167, sequence: 33255 - clear  0.000000000, sequence: 0
    source 0 - assert 1788478806.000000498, sequence: 33256 - clear  0.000000000, sequence: 0

---

**Run** `gpsmon` **to verify incoming GPS connections. Can also run** `gpspipe -w | jq ".uSat| select( . != null )"` **for a quick way to see the number of satellites currently being used**

    [root@pikvm ~]# gpspipe -w | jq ".uSat| select( . != null )"
    9
    9
    9
    9
    9
    9
    9
    9
    9
    9

---

**Create a directory in /tmp for chrony since this will normally run in a read only filesystem**

    mkdir -p /tmp/chrony
    chown chrony:chrony /tmp/chrony
    chmod 700 /tmp/chrony

---

**/etc/chrony.conf**

     # Sources
     
     # Default GPS entry
     refclock SHM 0 refid NMEA offset 0.000 precision 1e-3 poll 3 noselect
     
     # Tuned GPS time
     # refclock SHM 0 refid NMEA offset 0.136 precision 1e-3 poll 3 noselect
     
     refclock PPS /dev/pps0 refid PPS lock NMEA poll 3 prefer
     
     # Backup internet servers
     # server time.nist.gov iburst
     # server time.cloudflare.com iburst nts
     
     # Logging
     logdir /tmp/chrony
     log measurements statistics tracking
     
     # Allow clients on the local network to use this NTP server
     allow 10.0.0.0/8
     
For now, use the 0.000 offset for the GPS. Comment out any internet NTP servers while we get PPS up and running as well. We also allow devices to use this as an NTP server, change subnet as appropriate.

---

**/usr/lib/systemd/system/chronyd.service**

    PrivateTmp=no
    ReadWritePaths=/tmp/chrony /run /var/lib/chrony -/var/log

For now, we'll let chrony log to /tmp since we'll set the PiKVM back to read only once setup is done.

---

**Restart and check both services**

    systemctl restart chronyd  
    systemctl restart gpsd.socket
    systemctl status chronyd --no-pager
    systemctl status gpsd.socket --no-pager

---

**gpspipe -w -n 10**

    {"class":"VERSION","release":"3.27.5","rev":"3.27.5","proto_major":16,"proto_minor":1}
    {"class":"DEVICES","devices":[{"class":"DEVICE","path":"/dev/ttyAMA0","driver":"u-blox","subtype":"SW 1.00 (59842),HW 00070000","subtype1":"PROTVER 14.00,GPS;SBAS;GLO;QZSS","activated":"2026-09-04T03:09:05.103Z","flags":1,"native":1,"bps":115200,"parity":"N","stopbits":1,"cycle":1.00,"mincycle":0.02},{"class":"DEVICE","path":"/dev/pps0","driver":"PPS","activated":"2026-09-04T03:09:05.000Z"}]}
    {"class":"WATCH","enable":true,"json":true,"nmea":false,"raw":0,"scaled":false,"timing":false,"split24":false,"pps":false}
    {"class":"PPS","device":"/dev/ttyAMA0","real_sec":1788491346,"real_nsec":0,"clock_sec":1788491346,"clock_nsec":844,"precision":-20,"shm":"NTP1"}
    {"class":"PPS","device":"/dev/pps0","real_sec":1788491346,"real_nsec":0,"clock_sec":1788491346,"clock_nsec":844,"precision":-20,"shm":"NTP2"}
    {"class":"TPV","device":"/dev/ttyAMA0","status":2,"mode":3,"time":"2026-09-04T03:09:06.000Z",REDACTED}
    {"class":"SKY","device":"/dev/ttyAMA0","time":"2026-09-04T03:09:05.000Z",REDACTED}
    {"class":"SKY","device":"/dev/ttyAMA0","time":"2026-09-04T03:09:06.000Z",REDACTED}
    {"class":"SKY","device":"/dev/ttyAMA0","time":"2026-09-04T03:09:06.000Z",REDACTED}
    {"class":"TPV","device":"/dev/ttyAMA0","status":2,"mode":3,"time":"2026-09-04T03:09:06.000Z",REDACTED}

First two lines verify both GPS and PPS devices, and that the GPS is using baud 115200. Any remaining SKY and TPV entries verify that GPS information is coming in.

---

**ls -l /tmp/***

    -rw-r--r-- 1 chrony       chrony         16714 Sep  3 21:59 /tmp/measurements.log
    -rw-r--r-- 1 chrony       chrony        261378 Sep  3 21:59 /tmp/statistics.log
    -rw-r--r-- 1 chrony       chrony        141900 Sep  3 21:59 /tmp/tracking.log

You should be able to see the chrony logs in /tmp after a few seconds

---

**chronyc sources -v**
 

     .-- Source mode  '^' = server, '=' = peer, '#' = local clock.
     / .- Source state '*' = current best, '+' = combined, '-' = not combined,
    | /             'x' = may be in error, '~' = too variable, '?' = unusable.
    ||                                                 .- xxxx [ yyyy ] +/- zzzz
    ||      Reachability register (octal) -.           |  xxxx = adjusted offset,
    ||      Log2(Polling interval) --.      |          |  yyyy = measured offset,
    ||                                \     |          |  zzzz = estimated error.
    ||                                 |    |           \
    MS Name/IP address         Stratum Poll Reach LastRx Last sample
    ===============================================================================
    #? NMEA                          0   3   377     9  +4298us[+4298us] +/- 1000us
    #* PPS                           0   3   377     8    -95ns[  +62ns] +/-  153ns

PPS should have a * to the left of it, indicating that chrony has selected PPS as its current reference.

---

**chronyc tracking**

    Reference ID    : 50505300 (PPS)
    Stratum         : 1
    Ref time (UTC)  : Fri Sep 04 03:00:19 2026
    System time     : 0.000000009 seconds fast of NTP time
    Last offset     : +0.000000163 seconds
    RMS offset      : 0.000000159 seconds
    Frequency       : 15.745 ppm fast
    Residual freq   : +0.000 ppm
    Skew            : 0.010 ppm
    Root delay      : 0.000000001 seconds
    Root dispersion : 0.000012887 seconds
    Update interval : 8.0 seconds
    Leap status     : Normal

Chrony is now synchronized to the GPS PPS reference. In this example, the system clock is only 9 nanoseconds fast relative to the selected PPS reference, with an RMS offset of 159 nanoseconds.

We'll now add a couple of public NTP servers as a backup to the GPS

---

**/etc/chrony.conf**

    # Sources
    
    # Default GPS entry
    refclock SHM 0 refid NMEA offset 0.000 precision 1e-3 poll 3 noselect
    
    # Tuned GPS time
    # refclock SHM 0 refid NMEA offset 0.136 precision 1e-3 poll 3 noselect
    
    refclock PPS /dev/pps0 refid PPS lock NMEA poll 3 prefer
    
    # Backup internet servers
    server time.cloudflare.com nts iburst
    server time.apple.com iburst
    server time.nist.gov

I added cloudflare, apple, and nist

---

**REBOOT**

---

chronyc sources -v

      .-- Source mode  '^' = server, '=' = peer, '#' = local clock.
     / .- Source state '*' = current best, '+' = combined, '-' = not combined,
    | /             'x' = may be in error, '~' = too variable, '?' = unusable.
    ||                                                 .- xxxx [ yyyy ] +/- zzzz
    ||      Reachability register (octal) -.           |  xxxx = adjusted offset,
    ||      Log2(Polling interval) --.      |          |  yyyy = measured offset,
    ||                                \     |          |  zzzz = estimated error.
    ||                                 |    |           \
    MS Name/IP address         Stratum Poll Reach LastRx Last sample
    ===============================================================================
    #? NMEA                          0   3   377     8    +81ms[  +81ms] +/- 1000us
    #* PPS                           0   3   377     7   -168ns[ -230ns] +/-  114ns
    ^? time-a-wwv.nist.gov           1  10   377   706  +3455us[+3449us] +/-   20ms
    ^- time.cloudflare.com           3  10   377   667   +145us[ +138us] +/-   18ms

PPS is still the current selected source from chrony, and we can see two of our three backups as well. At this point, PiKVM should be back in read-only, and the server is up and running, ready to serve clients.

Lets set up a client.

---

**Example client config**

    # Sources
    server 10.1.1.150 iburst prefer
    
    # Backup internet servers
    server time.cloudflare.com nts iburst
    server time.apple.com iburst
    server time.nist.gov

---

**Example client chronyc sources -v**

      .-- Source mode  '^' = server, '=' = peer, '#' = local clock.
     / .- Source state '*' = current best, '+' = combined, '-' = not combined,
    | /             'x' = may be in error, '~' = too variable, '?' = unusable.
    ||                                                 .- xxxx [ yyyy ] +/- zzzz
    ||      Reachability register (octal) -.           |  xxxx = adjusted offset,
    ||      Log2(Polling interval) --.      |          |  yyyy = measured offset,
    ||                                \     |          |  zzzz = estimated error.
    ||                                 |    |           \
    MS Name/IP address         Stratum Poll Reach LastRx Last sample
    ===============================================================================
    ^* 10.1.1.150                    1   8   377   215    -10us[  -24us] +/-   86us
    ^- time.cloudflare.com           3  10   377   997   -684us[ -735us] +/-   19ms
    ^- uslax1-ntp-003.aaplimg.c>     1  10   377   655  -1528us[-1545us] +/-   29ms
    ^- time-a-b.nist.gov             1  10   377   812  +3174us[+3142us] +/-   20ms

---

**Setting up NTS**

If we're going overkill building a GPS enabled local NTP server, why not set up NTS on it as well. 

---

**PiKVM into read/write mode and create the certs**

    rw
    mkdir /root/chrony
    openssl genrsa -out /root/chrony/ca.key 8192
    openssl req -x509 -new -nodes -key /root/chrony/ca.key -sha512 -days 36500 -out /root/chrony/ca.crt -subj "/CN=PiKVM NTS CA"
    openssl genrsa -out /root/chrony/nts-server.key 8192
    openssl req -new -key /root/chrony/nts-server.key -out /root/chrony/nts-server.csr -subj "/CN=10.1.1.150"

    cat > /root/chrony/nts-server.ext <<'EOF'
    basicConstraints = critical, CA:FALSE
    keyUsage = critical, digitalSignature, keyEncipherment
    extendedKeyUsage = serverAuth
    subjectAltName = IP:10.1.1.150
    EOF

    openssl x509 -req -in /root/chrony/nts-server.csr -CA /root/chrony/ca.crt -CAkey /root/chrony/ca.key -CAcreateserial -out /root/chrony/nts-server.crt -days 36500 -sha512 -extfile /root/chrony/nts-server.ext
    chmod 600 /root/chrony/ca.key
    chmod 600 /root/chrony/nts-server.key
    chmod 644 /root/chrony/ca.crt
    chmod 644 /root/chrony/nts-server.crt
    cp /root/chrony/nts-server.crt /var/lib/chrony/
    cp /root/chrony/nts-server.key /var/lib/chrony/
    chown chrony:chrony /var/lib/chrony/nts-server.crt
    chown chrony:chrony /var/lib/chrony/nts-server.key
    chmod 644 /var/lib/chrony/nts-server.crt
    chmod 600 /var/lib/chrony/nts-server.key

---

**Cert verificaton**

    openssl x509 -in /root/chrony/nts-server.crt -noout -subject -issuer -dates -ext subjectAltName -text | grep -E 'Signature Algorithm|Subject:|Issuer:|Not Before|Not After|IP Address'
    openssl verify -CAfile /root/chrony/ca.crt /root/chrony/nts-server.crt

Should return something like

    [root@pikvm ~]# openssl x509 -in /root/chrony/nts-server.crt -noout -subject -issuer -dates -ext subjectAltName -text | grep -E 'Signature Algorithm|Subject:|Issuer:|Not Before|Not After|IP Address'
        IP Address:10.1.1.150
            Signature Algorithm: sha512WithRSAEncryption
            Issuer: CN=PiKVM NTS CA
                Not Before: Sep  4 10:15:48 2026 GMT
                Not After : Aug 11 10:15:48 2126 GMT
            Subject: CN=10.1.1.150
                    IP Address:10.1.1.150
        Signature Algorithm: sha512WithRSAEncryption
    [root@pikvm ~]# openssl verify -CAfile /root/chrony/ca.crt /root/chrony/nts-server.crt
    /root/chrony/nts-server.crt: OK
    [root@pikvm ~]#

---

**/etc/chrony.conf**

    ntsdumpdir /tmp/chrony
    ntsservercert /var/lib/chrony/nts-server.crt
    ntsserverkey /var/lib/chrony/nts-server.key

---

**chronyd -p**

Verify the current conf, should look close to this

    refclock SHM 0 refid NMEA offset 0.000 precision 1e-3 poll 3 noselect
    refclock PPS /dev/pps0 refid PPS lock NMEA poll 3 prefer
    server time.cloudflare.com iburst
    server time.apple.com iburst
    server time.nist.gov
    driftfile /var/lib/chrony/drift
    ntsdumpdir /tmp/chrony
    ntsservercert /var/lib/chrony/nts-server.crt
    ntsserverkey /var/lib/chrony/nts-server.key
    leapseclist /usr/share/zoneinfo/leap-seconds.list
    makestep 1.0 3
    logdir /tmp/chrony
    log measurements statistics tracking
    allow 10.0.0.0/8
    rtcsync

---

**Restart chrony and check status**

    [root@pikvm tmp]# systemctl restart chronyd
    [root@pikvm tmp]# systemctl status chronyd --no-pager -l
    * chronyd.service - NTP client/server
         Loaded: loaded (/usr/lib/systemd/system/chronyd.service; enabled; preset: disabled)
         Active: active (running) since Fri 2026-09-04 06:48:53 EDT; 4s ago
     Invocation: 8a0a8765f285442db9d0821b904f0668
           Docs: man:chronyd(8)
                 man:chrony.conf(5)
       Main PID: 50658 (chronyd)
          Tasks: 3 (limit: 4309)
            CPU: 524ms
         CGroup: /system.slice/chronyd.service
                 |-50658 /usr/bin/chronyd -n
                 `-50659 /usr/bin/chronyd -n
    
    Sep 04 06:48:53 pikvm systemd[1]: Starting NTP client/server...
    Sep 04 06:48:53 pikvm chronyd[50658]: chronyd version 4.8 starting (+CMDMON +REFCLOCK +RTC +PRIVDROP +SCFILTER +SIGND +NTS +SECHASH +IPV6 -DEBUG)
    Sep 04 06:48:53 pikvm chronyd[50658]: Having write access to /var/lib/chrony/nts-server.key
    Sep 04 06:48:53 pikvm chronyd[50658]: Using leap second list /usr/share/zoneinfo/leap-seconds.list
    Sep 04 06:48:53 pikvm systemd[1]: Started NTP client/server.
    Sep 04 06:48:53 pikvm chronyd[50658]: Frequency 15.669 +/- 0.009 ppm read from /var/lib/chrony/drift
    Sep 04 06:48:53 pikvm chronyd[50658]: Loaded server NTS keys

---

**REBOOT**

---

**Client cert**

Copy the `/root/chrony/ca.crt` to the client at `/etc/chrony/ca.crt`. I just used cat to print the cert, then nano on the client to create it since I don't allow my machines to SSH into each other.

Set the perms on the cert

    sudo  chmod  644 /etc/chrony/ca.crt

---

**Client /etc/chrony/chrony.conf**

    ntstrustedcerts /etc/chrony/ca.crt

---

**sudo chronyd -p**

Verify the current conf, should look close to this

    server 10.1.1.150 iburst nts prefer
    server time.cloudflare.com nts iburst
    server time.apple.com iburst
    server time.nist.gov
    ntstrustedcerts /etc/chrony/ca.crt
    keyfile /etc/chrony/chrony.keys
    driftfile /var/lib/chrony/chrony.drift
    ntsdumpdir /var/lib/chrony
    logdir /var/log/chrony
    maxupdateskew 100.0
    rtcsync
    makestep 1 3
    leapseclist /usr/share/zoneinfo/leap-seconds.list

---

**Restart and verify chrony sources**

    user@pve:~$ sudo systemctl restart chronyd
    user@pve:~$ sudo chronyc ntpdata 10.1.1.150 | grep Authenticated
    Authenticated   : Yes
    user@pve:~$ sudo chronyc -N authdata
    Name/IP address             Mode KeyID Type KLen Last Atmp  NAK Cook CLen
    =========================================================================
    10.1.1.150                   NTS     1   30  128  20m    0    0    8   64
    time.cloudflare.com          NTS     1   30  128 403m    0    0    8   64
    time.apple.com                 -     0    0    0    -    0    0    0    0
    time.nist.gov                  -     0    0    0    -    0    0    0    0
    user@pve:~$ sudo chronyc selectdata 10.1.1.150
    S Name/IP Address        Auth COpts EOpts Last Score     Interval  Leap
    =======================================================================
    * 10.1.1.150                Y -P--- -PTR-    0   1.0   -83us   +86us  N
    P time.cloudflare.com       Y ----- --TR-    1   1.0   -21ms   +21ms  N
    T ussjc2-ntp-002.aaplimg.c> N ----- -----    0   1.0   -29ms   +26ms  N
    T time-b-wwv.nist.gov       N ----- -----    6   1.0   -19ms   +23ms  N

We can see ntpdata reports authenticated, authdata reports NTS, and selectdata says auth Y.

---

Now our PiKVM is a fully running GPS NTP server with NTS enabled.
