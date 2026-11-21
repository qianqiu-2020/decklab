# initramfs-tools boot=ramroot handler.
mount_top() { :; }
mount_premount() { :; }
mount_bottom() { :; }

ramroot_stop()
{
    echo "RAMROOT_ERROR $*" >/dev/console
    # Fail closed and wait for a board reset rather than starting the OS or
    # entering initramfs-tools recovery against unexpected media.
    while true; do sleep 60; done
}

mountroot()
{
    # The kernel DTB must leave no persistent block or MTD device available.
    for device in /sys/block/* /sys/class/mtd/mtd*; do
        [ -e "$device" ] || continue
        case "${device##*/}" in
            loop[0-9]*|ram[0-9]*) ;;
            *) ramroot_stop "unexpected storage device ${device##*/}" ;;
        esac
    done

    base=/run/ramroot
    mkdir -p "$base" || ramroot_stop "cannot create RAM root staging directory"
    mount -t tmpfs -o mode=0700,size=85% tmpfs "$base" || ramroot_stop "cannot mount tmpfs"
    mkdir "$base/lower" "$base/upper" "$base/work" || ramroot_stop "cannot create overlay directories"
    if [ -f /ramroot/overlay.ko ]; then
        insmod /ramroot/overlay.ko || ramroot_stop "cannot load overlay module"
    fi
    # gzip checks the checksum and length of the combined stream.
    cat /ramroot/rootfs.* | gzip -dc >"$base/rootfs.ext4" ||
        ramroot_stop "cannot decompress root filesystem"
    rm /ramroot/rootfs.* || ramroot_stop "cannot release compressed root filesystem"
    mount -t ext4 -o loop,ro,noload "$base/rootfs.ext4" "$base/lower" ||
        ramroot_stop "cannot mount RAM-backed ext4"
    mount -t overlay -o "lowerdir=$base/lower,upperdir=$base/upper,workdir=$base/work" overlay "$rootmnt" ||
        ramroot_stop "cannot mount RAM overlay"
    [ -d "$rootmnt/etc" ] && [ ! -L "$rootmnt/etc" ] ||
        ramroot_stop "unexpected etc directory"
    rm -f "$rootmnt/etc/fstab" "$rootmnt/etc/crypttab" ||
        ramroot_stop "cannot clear disk mounts"
    printf '%s\n' '# RAM boot: root contains no persistent disk mounts' >"$rootmnt/etc/fstab" ||
        ramroot_stop "cannot set RAM fstab"
    echo "RAMROOT_MOUNTED lower=$base/lower backing=$base/rootfs.ext4"
    while read -r source target filesystem options _; do
        case "$target" in
            "$base"|"$base/lower"|"$rootmnt")
                echo "RAMROOT_MOUNT $source $target $filesystem $options" ;;
        esac
    done </proc/mounts
    for loop in /sys/block/loop*/loop/backing_file; do
        [ -r "$loop" ] || continue
        read -r backing <"$loop"
        [ "$backing" = "$base/rootfs.ext4" ] && echo "RAMROOT_BACKING $loop $backing"
    done
}
