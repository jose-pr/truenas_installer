from enum import Enum
import json
import re
from typing import Callable
from truenas_install import utils
from pathlib import Path
from truenas_install.utils import Command, UnitPrefix

MIN_SWAPSAFE_MEDIASIZE = 60 * UnitPrefix.GB
_sgdisk = Command("sgdisk")

DEV = Path("/dev")
SYS = Path("/sys")
SYSBOCK = SYS / "block"
PROC = Path("/proc")
EFIVARS = Path("sys/firmware/efi/efivars")

class PartitionType(str, Enum):
    BIOS = "EF02"
    EFI = "EF00"
    LinuxSwap = "8200"
    ZFS = "BF01"

    @property
    def id(self):
        return str(self)

    @property
    def guid(self):
        if self == PartitionType.BIOS:
            return "21686148-6449-6E6F-744E-656564454649"
        elif self == PartitionType.EFI:
            return "C12A7328-F81F-11D2-BA4B-00A0C93EC93B"
        elif self == PartitionType.LinuxSwap:
            return "0657FD6D-A4AB-43C4-84E5-0933C84B4F4F"
        elif self == PartitionType.ZFS:
            return "6A898CC3-1DD2-11B2-99A6-080020736631"
        else:
            raise Exception(f"Unknown Partition Type: {self}")

    @staticmethod
    def parse(uid: str):
        uid = uid.upper().strip()
        for t in PartitionType:
            if t == uid:
                return t
        raise Exception(f"Unknown Partition Type: {uid}")

    def __eq__(self, uid: object) -> bool:
        return  self.id == uid or self.guid == uid

class Mount:
    def __init__(
        self, device: "Device|str", path: Path, fs: "str" = None, /, **options: dict
    ):
        self.device = device
        self.path = path
        self.fs = fs
        self.options = options

    def __repr__(self) -> str:
        return f"{self.device} {self.path} {self.fs} {self.options}"

    def mount(self, **run_args):
        self.path.mkdir(parents=True, exist_ok=True)
        cmd = Command(
            "mount",
            self.device,
            self.path,
        )
        if self.fs:
            cmd.add_named_arg("t", self.fs)
        if self.options:
            opts = []
            for name, value in self.options.items():
                if value:
                    if value is not True:
                        name = f"{name}={value}"
                elif value is False:
                    continue
                opts.append(name)
            if len(opts) > 0:
                cmd.add_named_arg("o", ",".join(opts))

        cmd.run(**run_args)
        return self.path

    def unmount(self, force = False, /, **run_args):
        cmd = Command("umount", self.path)
        if force:
            cmd.add_args("-f")
        cmd.run(**run_args)

    def __enter__(self):
        return self.mount()

    def __exit__(self, exept_type, exept_value, traceback):
        if self.path.is_mount():
            self.unmount()


def get_devmounts():
    mounts: "list[Mount]" = []
    for mnt in (PROC / "mounts").read_text().splitlines():
        if mnt.strip() != "":
            dev, mnt, fs, opts, _, _check_order = mnt.split()
            if dev.startswith("/dev/"):
                _opts = {}
                for opt in opts.split(","):
                    parts = opt.split("=", maxsplit=1)
                    name = parts[0]
                    value = True
                    if len(parts) == 2:
                        value = parts[1]
                    _opts[name] = value
                mounts.append(
                    Mount(
                        Device.new(dev.strip("/dev/")),
                        Path(mnt),
                        fs,
                        _opts,
                    )
                )
    return mounts


PART_MATCHERS = [
    re.compile(rf"^([a-zA-Z]+)?([0-9]+)$"),
    re.compile(rf"^([a-zA-Z]+[0-9]+n[0-9]+)p([0-9]+)$"),
]


def lsdevs(nodeps=False):
    args = {}
    if nodeps:
        args["nodeps"] = None

    _devs: "list[Device|Disk|Partition]" = []
    for _dev in (
        utils.Command("lsblk", noheadings=None, output="name", l=None, **args)
        .run()
        .stdout.strip()
        .splitlines()
    ):
        _devs.append(Device.new(_dev))
    return _devs


class Device:
    def __init__(self, name: "str") -> None:
        self.name = name

    @classmethod
    def new(cls, name: str):
        dev = cls(name.__str__())
        if dev.physical_disk():
            part = False
            for match in PART_MATCHERS:
                part = match.match(dev.name)
                if part:
                    return Partition(Device(part[0]), part[1])
            if not part:
                return Disk(dev.name)
        else:
            return dev

    def dev(self):
        return DEV / self.name

    def __repr__(self) -> str:
        return self.name

    def __hash__(self) -> int:
        return self.name.__hash__()

    def __eq__(self, __o: object) -> bool:
        return getattr(__o, "name", None) == self.name

    def mounted(self):
        match = re.compile(rf"^{self.name}(p?[0-9])?")
        for mount in get_devmounts():
            if match.match(mount.device.name):
                return True

    def physical_disk(self):
        return self.name[0:2] not in [
            "md",
            "dm",
            "sr",
            "st",
        ] and not self.name.startswith("loop")


class BlockDev(Device):
    def sysblock(self):
        return self.rootblock()

    def rootblock(self):
        return SYSBOCK / self.name

    def blocks(self):
        return int(self.sysblock().joinpath("size").read_text().strip())

    def block_size(self):
        return int(
            self.rootblock().joinpath("queue/logical_block_size").read_text().strip()
        )

    def capacity(self):
        return self.block_size() * self.blocks()

    def removable(self):
        return self.rootblock().joinpath("removable").read_text().strip() == "1"

    def model(self):
        return (self.rootblock() / "device/model").read_text().strip()

    def lsblk(self, *props: str) -> "list[dict]":
        utils.Command("udevadm", "settle").run()
        return json.loads(
            utils.Command("lsblk", "-fJo", ",".join(props), self.dev()).run().stdout
        )["blockdevices"]


class Disk(BlockDev):
    SYSBOCK = Path("/sys/block")

    def description(self):
        model = self.model()
        # need to settle so that lsblk output is stable
        info = self.lsblk("fstype", "name", "label")[0]
        _root_fstype: "str" = info.get("fstype", "")
        _children: "list[dict]" = info.get("children", [])
        _label = ""

        if _root_fstype is None and len(_children) == 0:
            # no fs info in lsblk output
            _label = ""
        elif _root_fstype is not None:
            _label = _root_fstype[0:15]
        else:
            _fs = None
            for fs in ["zfs_member", "ext4", "xfs", ""]:
                for child in _children:
                    found = child.get("fstype", None)
                    if found == fs or (fs == "" and found is not None):
                        _label = child.get("label", None)
                        _fs = fs
                        break
                if _label != "":
                    break

            if _fs == "zfs_member":
                _label = f"zfs-{_label}"[0:15]
        return f"{model[0:15]} {_label} -- {UnitPrefix.format_bytes(self.capacity())}"

    def create_partition(
        self,
        partnum: int,
        typecode: str,
        start: "str|int|None" = None,
        end: "str|int|None" = None,
        aligment: "list[str]|None" = None,
        attributes: "str|None" = None,
    ):
        cmd = _sgdisk.with_args(
            new=f"{partnum}:{start or 0}:{end or 0}",
            typecode=f"{partnum}:{typecode}",
        )

        if aligment is not None:
            cmd.add_named_arg("set-alignment", aligment)

        if attributes is not None:
            cmd.add_named_arg(
                "attributes", ",".join([f"{partnum}:{attr}" for attr in attributes])
            )
        cmd.add_args(self.dev()).run()

    def get_partition(self, num: int):
        for p in [num, f"p{num}"]:
            name = f"{self}{p}"
            if self.dev().with_name(name).exists():
                return Partition(self.name, name)
        return None

    def partitions(self, filter:Callable[['Partition'], bool] = None):
        for file in self.sysblock().iterdir():
            if file.name.startswith(self.name):
                partition = Partition(self.name, file.name)
                if filter is None or filter(partition):
                    yield Partition(self.name, file.name)

    def not_swap_safe(self):
        return self.capacity() < MIN_SWAPSAFE_MEDIASIZE or self.removable()

    def wipe_partition_table(self, **run_args):
        # Destroy GPT and MBR data structures and then exit
        return _sgdisk.with_args("-Z", self.dev()).run(**run_args).returncode == 0

    def get_partition_guid(self, partnum: int):
        return dict(
            map(
                lambda s: s.split(": ", 1),
                _sgdisk.with_args(i=partnum, _=self.dev()).run().stdout.splitlines(),
            )
        )["Partition GUID code"].split()[0]


class Partition(BlockDev):
    def __init__(self, dev: "str", name: str) -> None:
        self.parent = Disk(dev)
        super().__init__(name)

    def sysblock(self):
        return self.rootblock() / self.name

    def rootblock(self):
        return self.parent.rootblock()

    def partnum(self):
        return int((self.sysblock() / "partition").read_text().strip())

    def get_guid(self):
        return self.parent.get_partition_guid(self.partnum())


def get_physical_disks():
    return [
        Disk(disk)
        for disk in utils.Command("lsblk", nodeps=None, noheadings=None, output="name")
        .run()
        .stdout.strip()
        .splitlines()
        if (disk[0:2] not in ["md", "dm", "sr", "st"] and not disk.startswith("loop"))
    ]
