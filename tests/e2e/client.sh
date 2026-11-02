#!/bin/sh
# Bridges the container's network onto a tap device, then network-boots a VM from it.
# Usage: client.sh <bios-x64|uefi-x64|uefi-arm64> [mac]
set -eu

firmware="$1"
mac="${2:-52:54:00:12:34:56}"
memory="${CLIENT_MEMORY:-1536}"

[ -e /dev/net/tun ] || { mkdir -p /dev/net; mknod /dev/net/tun c 10 200; }
ip link add br0 type bridge
ip link set br0 type bridge forward_delay 0 stp_state 0
ip tuntap add tap0 mode tap
ip link set tap0 master br0
ip addr flush dev eth0
ip link set eth0 master br0
ip link set tap0 up
ip link set br0 up
# Docker's bridge can reflect broadcasts back, which would teach br0 that the VM is out on eth0
# and make it drop every unicast reply. With no learning on eth0, replies flood to the VM instead.
bridge link set dev eth0 learning off

tap="tap,id=net0,ifname=tap0,script=no,downscript=no"

case "$firmware" in
    bios-x64)
        # iPXE only writes to the VGA screen here, so its output won't be in the serial log.
        exec qemu-system-x86_64 -M pc -accel tcg -m "$memory" -nographic \
            -netdev "$tap" -device virtio-net-pci,netdev=net0,mac="$mac",bootindex=0
        ;;
    uefi-x64)
        cp /firmware/OVMF_VARS_4M.fd /tmp/vars.fd
        # An empty romfile makes the firmware's own PXE client do the work, as on real hardware.
        exec qemu-system-x86_64 -M q35 -accel tcg -m "$memory" -nographic \
            -drive if=pflash,format=raw,readonly=on,file=/firmware/OVMF_CODE_4M.fd \
            -drive if=pflash,format=raw,file=/tmp/vars.fd \
            -netdev "$tap" -device virtio-net-pci,netdev=net0,mac="$mac",romfile=,bootindex=0
        ;;
    uefi-arm64)
        # An empty romfile makes the firmware's own PXE client do the work, as on real hardware.
        exec qemu-system-aarch64 -M virt -cpu max -accel tcg -m "$memory" -nographic \
            -bios /usr/share/qemu-efi-aarch64/QEMU_EFI.fd \
            -netdev "$tap" -device virtio-net-pci,netdev=net0,mac="$mac",romfile=,bootindex=0
        ;;
    *)
        echo "Unknown firmware: $firmware" >&2
        exit 2
        ;;
esac
