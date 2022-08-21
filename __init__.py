#!/root/dev/.venv/bin/python
import contextlib
from enum import Flag
import itertools
import sqlite3
import stat
from pathlib import Path
import platform
import shutil
import json
import subprocess
import sys
from typing import Literal

from truenas_install.zfs import Dataset, zfs

from .bootpool import BootEnviroment, BootPool
from .utils import (
    Command,
    Logger,
    Root,
    UnitPrefix,
    booted_as_uefi,
    unsquashfs,
)
from .diskutils import EFIVARS, Disk, Mount, PartitionType

AVATAR_PROJECT = "TrueNAS"
_VERSION_PATH = Path("/etc/version")
AVATAR_VERSION = _VERSION_PATH.read_text() if _VERSION_PATH.exists() else ""
BOOT_POOL = "boot-pool"

IS_FREEBSD = platform.system().upper() == "FREEBSD"
FREEBSD_UPDATE_SENTINEL = "data/freebsd-to-scale-update"
CD_UPGRADE_SENTINEL = "data/cd-upgrade"
NEED_UPDATE_SENTINEL = "data/need-update"
# create a sentinel file for post-fresh-install boots
FIRST_INSTALL_SENTINEL = "data/first-boot"
TRUENAS_EULA_PENDING_SENTINEL = "data/truenas-eula-pending"
FREENAS_DB = "data/factory-v1.db"
USER_SERVICES = "data/user-services.json"

MIN_ZFS_PARTITION_SIZE = 8 * UnitPrefix.GB

UPDATE_FILE = Path("/cdrom/TrueNAS-SCALE.update")
UPGRADE_DATA = Path("/tmp/data_preserved")
EFI_BOOTLOADER = Path("EFI/zfsbootmenu.EFI")


class DiskUse(Flag):
    NONE = 0
    Bootloader = 1
    BootPool = 3


class TruenasDisk(Disk):
    def __init__(self, name: str, use: DiskUse) -> None:
        self.use = use
        super().__init__(name)


class TrueNASInstaller:
    def __init__(self, json_output=False, base_mount="/mnt") -> None:
        self.base_mount = Path(base_mount)
        self.disks: "list[TruenasDisk]" = []
        self.swap: "None|bool" = None
        self.boot_size: "int|None" = None
        self.uefi: bool = Path("/sys/firmware/efi").exists()
        self.old_pool = BootPool(BOOT_POOL)
        self.boot_pool = BootPool(BOOT_POOL)
        self.encryption = ("off", None)
        self.keystore_path = None
        self.keystore = None
        self.logger = Logger(json_output)
        self.password = None
        self.action = "installation"
        self.upgrade_type: 'Literal["format","inplace"]' = "format"
        self.eula_accepted = False

    def create_partitions(self, wipe: bool):
        # Create and destroy existing pool (if exists)
        self.boot_pool.import_(force=True, check=False)
        self.boot_pool.destroy(force=True, check=False)
        alignment_multiple = 4096
        for disk in self.disks:
            if wipe:
                self.logger.msg(f"Wiping partition table on disk:{disk}")
                disk.wipe_partition_table()
            if DiskUse.Bootloader in disk.use:
                if (
                    next(
                        disk.partitions(
                            lambda p: p.get_guid() == PartitionType.BIOS.guid
                        ),
                        None,
                    )
                    == None
                ):
                    self.logger.msg(f"Creating bios boot partition on disk:{disk}")
                    # Create BIOS boot partition should be 1 on OS disks
                    disk.create_partition(
                        0,
                        PartitionType.BIOS,
                        end="+1024K",
                        attributes=["set:2"],
                        aligment=alignment_multiple,
                    )
                # Create EFI partition (Even if not used, allows user to switch to UEFI later)
                # Should be 2 on OS disks
                if (
                    next(
                        disk.partitions(
                            lambda p: p.get_guid() == PartitionType.EFI.guid
                        ),
                        None,
                    )
                    == None
                ):
                    self.logger.msg(f"Creating EFI boot partition on disk:{disk}")
                    disk.create_partition(0, PartitionType.EFI, end="+524288K")

            if DiskUse.BootPool in disk.use:
                if (
                    self.swap
                    and next(
                        disk.partitions(
                            lambda p: p.get_guid() == PartitionType.LinuxSwap.guid
                        ),
                        None,
                    )
                    == None
                ):
                    self.logger.msg(f"Creating swap partition on disk:{disk}")
                    disk.create_partition(4, PartitionType.LinuxSwap, end="+16777216K")
                    part = disk.get_partition(4)
                    if part:
                        Command("wipefs", "-a", "-t", "zfs_member", part).run()

                if (
                    next(
                        disk.partitions(
                            lambda p: p.get_guid() == PartitionType.ZFS.guid
                        ),
                        None,
                    )
                    == None
                ):
                    # Create boot pool
                    self.logger.msg(
                        f"Creating {self.boot_pool} partition on disk:{disk}",
                    )
                    disk.create_partition(
                        3,
                        PartitionType.ZFS,
                        end=f"+{self.boot_size}G" if self.boot_size else 0,
                    )

                    if disk.get_partition(3).capacity() < MIN_ZFS_PARTITION_SIZE:
                        self.logger.error(
                            f"Disk is too small to install {AVATAR_PROJECT}",
                            raise_=True,
                        )

    def create_bootpool(self):
        pool_members = [
            next(disk.partitions(lambda p: p.get_guid() == PartitionType.ZFS.guid))
            for disk in self.disks
            if DiskUse.BootPool in disk.use
        ]
        self.logger.msg(f"Creating BootPool:{self.boot_pool} on {pool_members}")
        return BootPool.create(
            self.boot_pool,
            vdevs=[(None, pool_members)],
            features=[
                "bookmarks",
                "embedded_data",
                "async_destroy",
                "empty_bpobj",
                "enabled_txg",
                "extensible_dataset",
                "filesystem_limits",
                "hole_birth",
                "large_blocks",
                "lz4_compress",
                "spacemap_histogram",
                "userobj_accounting",
            ],
            props={"cachefile": "/tmp/zpool.cache", "ashift": 12},
            fsprops={
                "acltype": "off",
                "compression": "lz4",
                "devices": "off",
                "normalization": "formD",
                "relatime": "on",
                "xattr": "sa",
            },
        )

    def install_bootloader(self, format_efi: bool):
        bootfs = self.boot_pool.bootfs()
        bootfs.base_mount = self.base_mount
        with bootfs as chroot:
            if booted_as_uefi():
                # Clean up dumps from NVRAM to prevent
                # "failed to register the EFI boot entry: No space left on device"
                for item in chroot.path.joinpath(EFIVARS).iterdir():
                    if item.name.startswith("dump-"):
                        with contextlib.suppress(Exception):
                            item.unlink()
            for disk in set(
                [disk for disk in self.disks if DiskUse.Bootloader in disk.use]
            ):
                partition = disk.get_partition(2)
                if DiskUse.BootPool not in disk.use:
                    partition = next(
                        disk.partitions(
                            lambda p: p.get_guid() == PartitionType.EFI.guid
                        ),
                        partition,
                    )
                if partition == None:
                    self.logger.error(f"Couldnt find efi partition for disk {disk}")
                    continue
                if format_efi or partition.get_guid() != PartitionType.EFI.guid:
                    Command(
                        "mkdosfs",
                        F=32,
                        s=1,
                        n="EFI",
                        _=partition.dev(),
                    ).run()

                with Mount(partition.dev(), chroot.path / "boot/efi", "vfat") as efi:
                    bootloader = efi / EFI_BOOTLOADER
                    bootloader.parent.mkdir(exist_ok=True)
                    if not bootloader.exists():
                        shutil.copy(
                            Path(__file__).parent / EFI_BOOTLOADER.name, bootloader
                        )
                    if booted_as_uefi():
                        chroot.run(
                            Command(
                                "efibootmgr",
                                c=None,
                                d=disk.dev(),
                                p=partition.partnum(),
                                L="ZFS Bootloader",
                                l=f"/{EFI_BOOTLOADER}",
                            )
                        )

    def cleanup(self):
        if self.old_pool:
            self.old_pool.export(force=True, check=False)
        self.boot_pool.export(force=True, check=False)

    def install(self, upgrade=False):
        # Make sure we are working from a clean slate.
        self.cleanup()

        if not upgrade:
            # With the new partitioning, disk_is_freenas may
            # copy /data.  So if we don't need it, remove it,
            # or else it'll do an update anyway.  Oops.
            shutil.rmtree(UPGRADE_DATA, ignore_errors=True)
        # Start critical section.
        try:
            if self.upgrade_type == "inplace":
                self.boot_pool.import_(True, False)
            else:
                self.logger.msg(f"Creating Partitions for {self.disks}")
                # We repartition on fresh install, or old upgrade_style
                # This destroys all of the pool data, and
                # ensures a clean filesystems.
                self.create_partitions(True)
                self.create_bootpool()
                return True

            self.logger.msg(f"Installing {AVATAR_PROJECT} to {self.boot_pool}")
            self.install_update(UPDATE_FILE, UPGRADE_DATA)
            self.install_bootloader()
            self.boot_pool.export()

        except Exception as e:
            self.logger.error(e)
            raise

        return

    def install_update(self, src: Path, preserved_data: "Path|None", cleanup=True):
        src = Path(src)
        sql = None
        if src.is_file():
            _src = self.base_mount / "update"
            self.logger.msg(f"Mounting install media: {src} at {_src}")
            update_mnt = Mount(src, _src, "squashfs", loop=True)
            update_mnt.unmount(check=False)
            src = update_mnt.mount()
        else:
            update_mnt = None

        manifest: dict = json.loads((src / "manifest.json").read_text())
        space_required: int = manifest["size"]
        req = UnitPrefix.format_bytes(space_required)
        free_space = self.boot_pool.get_prop("free", int)
        free = UnitPrefix.format_bytes(free_space)
        self.logger.msg(
            f"Require {req} for installing {AVATAR_PROJECT}, {free} avaliable in {self.boot_pool}",
        )
        if free_space < space_required:
            msg = f"Insufficient disk space available. TrueNAS requires {req} but only {free} are available"
            self.logger.error(msg, raise_=True)

        self.logger.msg(f"Installing update from {src}")
        bootenv = self.boot_pool.boot_enviroment(manifest["version"])
        self.logger.progress(0, f"Creating dataset: {bootenv.name}")
        old_bootfs_prop = self.boot_pool.get_prop("bootfs", str)
        existing_datasets = set(
            filter(None, zfs("list", "-H", o="name").run().stdout.split("\n"))
        )
        if bootenv.name in existing_datasets:
            for i in itertools.count(1):
                probe_dataset_name = f"{bootenv.name}-{i}"
                if probe_dataset_name not in existing_datasets:
                    bootenv.name = probe_dataset_name
                    break
        bootenv.create(
            mountpoint="/",
            canmount="noauto",
            **{
                "truenas:kernel_version": manifest["kernel_version"],
                "zectl:keep": "False",
            },
        )
        bootenv.base_mount = self.base_mount
        try:

            with bootenv as chroot:
                self.logger.progress(0, "Extracting")
                try:
                    for progress in unsquashfs(
                        src / "rootfs.squashfs",
                        chroot.path,
                        f=None,
                        da=16,
                        fr=16,
                    ):
                        self.logger.progress(
                            progress * 0.9,
                            "Extracting",
                        )
                except subprocess.CalledProcessError as e:
                    self.logger.error(
                        f"unsquashfs failed with exit code {e.returncode}: {e.output}"
                    )
                    raise

                self.logger.progress(0.9, "Performing post-install tasks")

                # We want to remove this for fresh installation + upgrade both
                # In this case, /etc/machine-id would be treated as the valid
                # machine-id which it will be otherwise as well if we use
                # systemd-machine-id-setup --print to confirm but just to be cautious
                # we remove this as it will be generated automatically by systemd then
                # complying with /etc/machine-id contents
                (chroot.path / "var/lib/dbus/machine-id").unlink(missing_ok=True)

                is_freebsd_upgrade = False
                setup_machine_id = configure_serial = False
                if preserved_data:
                    is_freebsd_upgrade = (
                        preserved_data / "bin/freebsd-version"
                    ).exists()
                    rsync = Command(
                        "rsync",
                        "-aRx",
                        exclude=[
                            FREENAS_DB,
                            "data/manifest.json",
                            "data/sentinels",
                        ],
                    )
                    paths = [
                        "etc/hostid",
                        "data",
                        "root",
                    ]
                    if is_freebsd_upgrade:
                        if not IS_FREEBSD:
                            setup_machine_id = True
                    else:
                        paths.append("etc/machine-id")
                    rsync.add_args(*rsync, _=f"{chroot.path}/").run(
                        cwd=preserved_data,
                    )

                    (chroot.path / NEED_UPDATE_SENTINEL).touch()

                    if is_freebsd_upgrade:
                        (chroot.path / FREEBSD_UPDATE_SENTINEL).touch()
                    else:
                        enable_system_user_services(chroot, preserved_data)
                else:
                    Command("cp", "/etc/hostid", chroot.path / "etc/").run()
                    if not self.eula_accepted:
                        (chroot.path / TRUENAS_EULA_PENDING_SENTINEL).touch()
                    (chroot.path / FIRST_INSTALL_SENTINEL).touch()
                    setup_machine_id = configure_serial = True

                if setup_machine_id:
                    (chroot.path / "/etc/machine-id").unlink(missing_ok=True)
                    Command("systemd-machine-id-setup", root=chroot.path).run()

                if IS_FREEBSD:
                    pass
                else:
                    # Remove GRUB from fstab
                    fstab = (
                        chroot.path
                        / "usr/lib/python3/dist-packages/middlewared/etc_files/fstab.mako"
                    )
                    orig = fstab.read_text()
                    with fstab.open("w") as f:
                        for line in orig.splitlines(True):
                            if "/boot/grub" not in line:
                                f.write(line)

                    if self.password:
                        chroot.run(
                            Command("/etc/netcli", "reset_root_pw", self.password)
                        )

                    if sql:
                        chroot.run(Command("sqlite3", FREENAS_DB), input=sql)

                    if configure_serial:
                        configure_serial_port(chroot, chroot / FREENAS_DB)

                    self.boot_pool.set_props(bootfs=bootenv.name)
                    cp = chroot.run(
                        Command("/usr/local/bin/truenas-initrd.py", "/"),
                        check=False,
                    )
                    if cp.returncode > 1:
                        raise subprocess.CalledProcessError(
                            cp.returncode,
                            f"Failed to execute truenas-initrd: {cp.stderr}",
                        )
                    keystore = bootenv.keystore()
                    if keystore:
                        keystore_mnt = keystore.get_prop("mountpoint", str)
                        zol_conf = (
                            chroot.path / "usr/share/initramfs-tools/hooks/keystore"
                        )
                        zol_conf.write_text(
                            f"""#!/bin/sh
    mkdir -p "${{DESTDIR}}/{keystore_mnt}"
    cp {keystore_mnt}/{self.boot_pool.name}.key "${{DESTDIR}}/{keystore_mnt}/{self.boot_pool.name}.key"
    exit 0
    """
                        )
                        zol_conf.chmod(zol_conf.stat().st_mode | stat.S_IEXEC)
                        chroot.run(Command("update-initramfs", k="all", u=None))
        except Exception:
            if old_bootfs_prop != "-":
                self.boot_pool.set_props(bootfs=old_bootfs_prop)
            if cleanup:
                zfs("destroy", bootenv.name).run()
            raise
        finally:
            if update_mnt:
                update_mnt.unmount()


def dict_factory(cursor, row):
    d = {}
    for idx, col in enumerate(cursor.description):
        d[col[0]] = row[idx]
    return d


def query_config_table(table, database_path, prefix=None):
    database_path = database_path
    conn = sqlite3.connect(database_path)
    try:
        conn.row_factory = dict_factory
        c = conn.cursor()
        try:
            c.execute(f"SELECT * FROM {table}")
            result: "dict[str,str]" = c.fetchone()
        finally:
            c.close()
    finally:
        conn.close()
    if prefix:
        result = {k.replace(prefix, ""): v for k, v in result.items()}
    return result


def enable_system_user_services(root: Path, old_root: Path):
    configure_serial_port(root, Path(old_root) / FREENAS_DB)
    enable_user_services(root, old_root)


def configure_serial_port(root: Path, db_path: Path):
    if not db_path.exists():
        return

    # We would like to explicitly enable/disable serial-getty in the new BE based on db configuration
    advanced = query_config_table("system_advanced", db_path, prefix="adv_")
    if advanced["serialconsole"]:
        Root(root).run(
            Command(
                "systemctl", "enable", f"serial-getty@{advanced['serialport']}.service"
            ),
            check=False,
        )


def enable_user_services(root: Path, old_root: Path):
    user_services_file = Path(old_root) / USER_SERVICES
    if not user_services_file.exists():
        return

    systemd_units = [
        srv
        for srv, enabled in json.loads(user_services_file.read_text()).items()
        if enabled
    ]

    if systemd_units:
        Root(root).run(Command("systemctl", "enable", *systemd_units), check=False)
