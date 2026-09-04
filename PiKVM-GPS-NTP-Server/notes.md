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
     logdir /tmp
     log measurements statistics tracking
     
     # Allow clients on the local network to use this NTP server
     allow 10.0.0.0/8
     
For now, use the 0.000 offset for the GPS. Comment out any internet NTP servers while we get PPS up and running as well. We also allow devices to use this as an NTP server, change subnet as appropriate.

---

**/usr/lib/systemd/system/chronyd.service**

    PrivateTmp=no
    ReadWritePaths=/tmp /run /var/lib/chrony -/var/log

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
