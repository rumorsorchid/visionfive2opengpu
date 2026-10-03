#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""qemu-test.py - boot a VisionFive 2 image in QEMU before flashing it.

QEMU has no JH7110, so this checks everything except the board drivers:
mainline U-Boot's standard boot finds the bootable partition and its
extlinux.conf, loads the compressed kernel and initramfs, the root
filesystem mounts by UUID, first boot grows the partition to fill a larger
disk, the GPU self-test skips cleanly, greetd logs the user into labwc
(software rendering on virtio-gpu), and the kernel and services come up
without failed units.

  sudo image/qemu-test.py IMAGE.img[.gz] --uboot u-boot.bin [--opensbi fw_jump.bin]

U-Boot: mainline qemu-riscv64_smode_defconfig with CONFIG_BOOTSTD_DEFAULTS,
CONFIG_BOOTCOMMAND="bootflow scan -lb" (as on the VisionFive 2).
The test works on a copy: only the copy's extlinux.conf loses its fdt line
(the JH7110 device tree would not boot QEMU's virt machine).
"""

import argparse
import gzip
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import zlib
import struct


def sh(*cmd, **kw):
    return subprocess.run(cmd, check=True, text=True, capture_output=True, **kw).stdout


def prepare(image, work, grow_gib):
    test = os.path.join(work, "test.img")
    if image.endswith(".gz"):
        with gzip.open(image, "rb") as src, open(test, "wb") as dst:
            shutil.copyfileobj(src, dst, 16 << 20)
    else:
        shutil.copyfile(image, test)
    table = json.loads(sh("sfdisk", "-J", test))["partitiontable"]
    part = table["partitions"][0]
    sector = table.get("sectorsize", 512)
    start, size = part["start"] * sector, part["size"] * sector
    print(f"partition 1: offset {start}, {size >> 20} MiB, attrs {part.get('attrs')}")
    fs = os.path.join(work, "part.ext4")
    with open(test, "rb") as f, open(fs, "wb") as o:
        f.seek(start)
        left = size
        while left:
            buf = f.read(min(left, 16 << 20))
            o.write(buf)
            left -= len(buf)
    conf = sh("debugfs", "-R", "cat /boot/extlinux/extlinux.conf", fs, stderr=subprocess.DEVNULL)
    print("extlinux.conf on the image:\n" + conf)
    edited = "\n".join(l for l in conf.splitlines() if not l.strip().startswith(("fdt ", "fdtdir ")))
    local = os.path.join(work, "extlinux.conf")
    with open(local, "w") as f:
        f.write(edited + "\n")
    sh("debugfs", "-w", "-R", "rm /boot/extlinux/extlinux.conf", fs)
    sh("debugfs", "-w", "-R", f"write {local} /boot/extlinux/extlinux.conf", fs)
    with open(fs, "rb") as i, open(test, "r+b") as f:
        f.seek(start)
        shutil.copyfileobj(i, f, 16 << 20)
    os.unlink(fs)
    os.truncate(test, os.path.getsize(test) + (grow_gib << 30))
    return test, size


class Serial:
    def __init__(self, proc, logpath):
        self.proc, self.buf, self.lock = proc, "", threading.Lock()
        self.log = open(logpath, "w")
        threading.Thread(target=self._reader, daemon=True).start()

    def _reader(self):
        while True:
            b = self.proc.stdout.read1(4096) if hasattr(self.proc.stdout, "read1") else self.proc.stdout.read(1)
            if not b:
                return
            s = b.decode("utf-8", "replace")
            self.log.write(s)
            self.log.flush()
            with self.lock:
                self.buf += s

    def wait(self, text, timeout):
        end = time.time() + timeout
        while time.time() < end:
            with self.lock:
                i = self.buf.find(text)
                if i >= 0:
                    self.buf = self.buf[i + len(text):]
                    return True
            if self.proc.poll() is not None:
                return False
            time.sleep(0.5)
        return False

    def send(self, s):
        self.proc.stdin.write(s.encode())
        self.proc.stdin.flush()

    def run(self, cmd, timeout=120):
        # The markers are split by '' on the command line, so only the
        # shell's output contains them whole (whether or not it echoes).
        n = int(time.time() * 1000)
        begin, done = f"__begin_{n}__", f"__done_{n}__"
        with self.lock:
            self.buf = ""
        self.send(f"echo __beg''in_{n}__; {cmd}; echo __do''ne_{n}__\n")
        end = time.time() + timeout
        while time.time() < end:
            with self.lock:
                i, j = self.buf.find(begin), self.buf.find(done)
                if 0 <= i < j:
                    out = self.buf[i + len(begin):j]
                    self.buf = self.buf[j + len(done):]
                    return out.strip("\r\n")
            time.sleep(0.5)
        return None


def ppm_to_png(ppm, png):
    with open(ppm, "rb") as f:
        data = f.read()
    parts = data.split(maxsplit=4)
    w, h, pixels = int(parts[1]), int(parts[2]), parts[4]
    raw = b"".join(b"\x00" + pixels[y * w * 3:(y + 1) * w * 3] for y in range(h))

    def chunk(t, d):
        return struct.pack(">I", len(d)) + t + d + struct.pack(">I", zlib.crc32(t + d) & 0xFFFFFFFF)
    with open(png, "wb") as f:
        f.write(b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0))
                + chunk(b"IDAT", zlib.compress(raw, 9)) + chunk(b"IEND", b""))


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("image")
    ap.add_argument("--uboot", required=True)
    ap.add_argument("--opensbi", default="/usr/lib/riscv64-linux-gnu/opensbi/generic/fw_jump.bin")
    ap.add_argument("--user", default="vf2")
    ap.add_argument("--password", default="vf2")
    ap.add_argument("--grow", type=int, default=4, help="GiB added to the disk to test growing")
    ap.add_argument("--timeout", type=int, default=2400, help="seconds to wait for the login prompt")
    ap.add_argument("--out", default=".", help="directory for serial log and screenshot")
    a = ap.parse_args()

    work = tempfile.mkdtemp(prefix="vf2-qemu-", dir=os.environ.get("TMPDIR", "/var/tmp"))
    fails = []

    def check(ok, what):
        print(("PASS  " if ok else "FAIL  ") + what)
        if not ok:
            fails.append(what)

    try:
        disk, part_size = prepare(a.image, work, a.grow)
        mon = os.path.join(work, "monitor.sock")
        cmd = ["qemu-system-riscv64", "-M", "virt", "-smp", "4", "-m", "4G",
               "-accel", "tcg,thread=multi", "-nographic",
               "-bios", a.opensbi, "-kernel", a.uboot,
               "-drive", f"file={disk},format=raw,if=none,id=hd0", "-device", "virtio-blk-device,drive=hd0",
               "-netdev", "user,id=n0", "-device", "virtio-net-device,netdev=n0",
               "-device", "virtio-gpu-device", "-device", "virtio-keyboard-device",
               "-device", "virtio-tablet-device",
               "-monitor", f"unix:{mon},server,nowait"]
        print(" ".join(cmd))
        proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        ser = Serial(proc, os.path.join(a.out, "qemu-serial.log"))
        t0 = time.time()
        check(ser.wait("extlinux", 300), "U-Boot standard boot found the extlinux bootflow")
        check(ser.wait("Linux version", 600), "kernel started")
        check(ser.wait("login:", a.timeout), f"login prompt on the serial console ({int(time.time() - t0)} s)")
        ser.send(a.user + "\n")
        ser.wait("assword:", 60)
        ser.send(a.password + "\n")
        time.sleep(15)
        ser.send("export PS1='$ ' TERM=dumb; stty -echo\n")
        time.sleep(3)
        state = ser.run("systemctl is-system-running --wait", 900) or ""
        check(state.strip().endswith(("running", "degraded")), f"system state: {state.strip()}")
        failed = ser.run("systemctl --failed --no-legend --plain | awk '{print $1}' | tr '\\n' ' '")
        check(not (failed or "").strip(), f"failed units: {(failed or '').strip() or 'none'}")
        size = ser.run("df -B1M --output=size / | tail -n 1") or "0"
        check(int(size.strip() or 0) > (part_size >> 20) + 1024,
              f"root filesystem grown to {size.strip()} MiB (image partition {part_size >> 20} MiB)")
        fb = ser.run("journalctl -b -u vf2-firstboot --no-pager -o cat | tail -n 6")
        print(fb)
        st = ser.run("cat /var/lib/vf2/gpu-status")
        check((st or "").strip() == "skipped", f"GPU self-test result on QEMU: {(st or '').strip()} (expected skipped)")
        check(bool((ser.run("pgrep -u " + a.user + " -x labwc") or "").strip()), "labwc runs for the autologin user")
        print(ser.run("tail -n 5 ~/.local/state/vf2-session.log"))
        print(ser.run("cat /proc/cmdline"))
        kfw = ser.run("journalctl -k -b --no-pager | grep -iE 'firmware|direct firmware load' | head -n 5")
        print("kernel firmware messages:", kfw or "none")
        time.sleep(20)
        ppm = os.path.join(work, "screen.ppm")
        try:
            s = socket.socket(socket.AF_UNIX)
            s.connect(mon)
            s.sendall(f"screendump {ppm}\n".encode())
            time.sleep(3)
            s.close()
            png = os.path.join(a.out, "qemu-screen.png")
            ppm_to_png(ppm, png)
            print("screenshot:", png)
        except OSError as e:
            print("no screenshot:", e)
        ser.send("sudo -S poweroff\n")
        time.sleep(2)
        ser.send(a.password + "\n")
        try:
            proc.wait(180)
        except subprocess.TimeoutExpired:
            proc.kill()
    finally:
        shutil.rmtree(work, ignore_errors=True)
    print(f"\n{len(fails)} failed check(s)")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
