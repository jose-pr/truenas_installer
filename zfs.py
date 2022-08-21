from os import chroot
from tabnanny import check
from typing import Type, TypeVar, overload
from pathlib import Path

from .utils import Command, Root


T = TypeVar("T")
ZPOOL_CMD = Command("zpool")
ZFS_CMD = Command("zfs")


class ZfsContainer:
    def __init__(self, name: str, mgmt_cmd: Command) -> None:
        self.name = name
        self.mgmt_cmd = mgmt_cmd
    @overload
    def get_prop(self, prop: str) -> str:
        pass
    @overload
    def get_prop(self, prop: str, ctr: "Type[T]") -> 'T|None':
        pass
    def get_prop(self, prop: str, ctr: "Type[T]" = None) -> 'str|T|None':
        value = (
            self.mgmt_cmd.with_args("get", H=None, o="value", p=prop, _=self.name)
            .run()
            .stdout.strip()
        )
        if value is None or value == "" or value == "-":
            return None
        if ctr:
            return ctr(value)
        else:
            return value

    def set_props(self, **props: str):
        for prop in props:
            self.mgmt_cmd.with_args("set", f"{prop}={props[prop]}", self.name).run()


def zfs(*args, **kwargs):
    return ZFS_CMD.with_args(*args, **kwargs)


class Dataset(ZfsContainer):
    def __init__(self, name: "str|list[str]") -> None:
        self.chroot:Root = Root("/")
        super().__init__("/".join(name if isinstance(name, list) else [name]), ZFS_CMD)

    def create(self, _auto=True, _runprops: dict = {}, /, **props):
        cmd = self.mgmt_cmd.with_args("create")
        for prop in props:
            cmd.add_named_arg("o", f"{prop}={props[prop]}")
        if not _auto:
            cmd.add_args("-u")
        cmd.add_args(self.name)
        cmd.run(**_runprops)

    def _mntpoint(self):
        path = self.get_prop("mountpoint")
        if path and path == "-":
            path = None
        return path

    def mount(self, path: Path = None):
        orig_mntpoint = self._mntpoint()
        mountpoint = Path(path or f"/mnt/{self.name}")

        if mountpoint != orig_mntpoint:
            self.set_props(mountpoint=mountpoint, orig_mountpoint=orig_mntpoint)
        self.chroot.run(zfs("mount", self.name))
        return (self.chroot, mountpoint)

    def unmount(self, check = True):
        self.chroot.run(zfs("unmount", self.name), check=check)
        orig_mntpoint = self.get_prop("orig_mountpoint", str)
        if orig_mntpoint:
            self.set_props(orig_mntpoint="", mountpoint=orig_mntpoint)
        
    def __enter__(self):
        return self.mount()

    def __exit__(self, exept_type, exept_value, traceback):
        path = self._mntpoint()
        if path and path != "legacy" and Path(path).is_mount():
            self.unmount(check=False)

from typing import Type, TypeVar

from .diskutils import Disk, Mount

def zpool(*args, **kwargs):
    return ZPOOL_CMD.with_args(*args, **kwargs)


class PoolBuilder:
    def __init__(self, name: "str", default_features=True, force=False) -> None:
        self.name = name
        self.default_features = default_features
        self.force = force
        self.features = {}
        self.fsprops = {}
        self.props = {}
        self.vdevs: "list[(str, list[str])]" = []
        pass

    def add_prop(self, name: str, value: str):
        # -o
        self.props[name] = value
        return self

    def add_props(self, **props):
        self.props.update(**props)
        return self

    def add_fsprop(self, name: str, value: str):
        # -O
        self.fsprops[name] = value
        return self

    def add_fsprops(self, **props):
        self.fsprops.update(**props)
        return self

    def set_feature(self, name: str, action: str):
        self.features[name] = action
        return self

    def enable_features(self, features: "list[str]"):
        for f in features:
            self.features[f] = "enabled"
        return self

    def add_vdev(self, type: str, devices: "list[str|Disk]"):
        self.vdevs.append((type, devices))
        return self

    def build_cmd(self):
        cmd = zpool("create")
        if not self.default_features:
            cmd.add_args("-d")
        if self.force:
            cmd.add_args("-f")
        for prop in self.props:
            cmd.add_named_arg("o", f"{prop}={self.props[prop]}")
        for prop in self.features:
            cmd.add_named_arg("o", f"feature@{prop}={self.features[prop]}")
        for prop in self.fsprops:
            cmd.add_named_arg("O", f"{prop}={self.fsprops[prop]}")

        cmd.add_args(self.name)
        for (type, devices) in self.vdevs:
            if not type or type.strip() == "":
                if len(devices) > 1:
                    type = "mirror"
                else:
                    cmd.add_args(*devices)
                    continue
            cmd.add_args(type, *devices)
        return cmd

    def build(self, **run_args):
        self.build_cmd().run(**run_args)
        return Pool(self.name)

    def __call__(self, **run_args):
        return self.build(**run_args)


class Pool(ZfsContainer):
    def __init__(self, name: str) -> None:
        super().__init__(name, ZPOOL_CMD)

    def import_(self, force=False, mount=False, **run_args):
        cmd = self.mgmt_cmd.with_args("import")
        if force:
            cmd.add_args("-f")
        if not mount:
            cmd.add_args("-N")
        cmd.add_args(self.name)
        return cmd.run(**run_args)

    def destroy(self, force=False, **run_args):
        cmd = self.mgmt_cmd.with_args("destroy")
        if force:
            cmd.add_args("-f")
        cmd.add_args(self.name)
        return cmd.run(**run_args)

    def export(self, force=False, **run_args):
        cmd = self.mgmt_cmd.with_args("export")
        if force:
            cmd.add_args("-f")
        cmd.add_args(self, self.name)
        return cmd.run(**run_args)
