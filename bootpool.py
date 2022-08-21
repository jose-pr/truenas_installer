from .diskutils import EFIVARS, Disk, Mount
from .utils import booted_as_uefi
from pathlib import Path

from .zfs import Dataset, Pool, PoolBuilder
from truenas_install import zfs

KEYSOURCE_PROP = "org.zfsbootmenu:keysource"


class BootPool(Pool):
    def __init__(self, name: str = "zroot", root: str = "ROOT") -> None:
        super().__init__(name or "zroot")
        self.root = root or "ROOT"

    def boot_enviroment(self, name: str = None):
        return BootEnviroment(self, name)

    @property
    def root_dataset(self):
        return Dataset([self.name, self.root])

    @classmethod
    def create(
        cls,
        name: str,
        vdevs: "list[tuple[str,list[Disk|str]]]" = [],
        features: "list[str]" = [],
        root: str = "ROOT",
        props: dict = {},
        fsprops: dict = {},
        encryption: "str|tuple[str,str]" = None,
        keystore: "str|tuple[str,str]" = None,
    ):
        run_args = {}
        if encryption:
            if isinstance(encryption, str):
                passphrase = encryption
                encryption = "on"
            else:
                encryption, passphrase = encryption
            if passphrase:
                features = features + ["encryption"]
                run_args["input"] = passphrase
            if not keystore:
                keystore = "keystore"

        builder = (
            PoolBuilder(name, default_features=False, force=True)
            .enable_features(features)
            .add_fsprops(
                canmount="off",
                mountpoint="none",
            )
            .add_fsprops(**fsprops)
            .add_props(**props)
        )

        for vdev in vdevs:
            builder.add_vdev(*vdev)

        pool = BootPool(builder.build(**run_args).name, root)
        zroot = Dataset(pool.name)

        root_ds = pool.root_dataset
        root_ds.create(canmount="off", mountpoint="none")

        if keystore:
            if isinstance(keystore, str):
                keystore = keystore
                mnt = f"/etc/{keystore}"
            else:
                keystore, mnt = encryption
            zroot.set_props(KEYSOURCE_PROP, keystore)

            keystore: Dataset = pool.keystore()
            keystore.create(False, mountpoint=mnt)
            if passphrase:
                zroot.set_props("keylocation", f"{mnt}/{pool.name}.key")
                with keystore as ks_root:
                    (ks_root.path / f"{pool.name}.key").write_text(passphrase)

        return pool

    def bootfs(self):
        bootfs = self.get_prop("bootfs", str)
        if bootfs:
            return self.boot_enviroment(bootfs.split("/")[-1])

    def keystore(self):
        keystore = self.get_prop(KEYSOURCE_PROP, str)
        if keystore:
            return Dataset(keystore)


class BootEnviroment(Dataset):
    def __init__(self, pool: BootPool, name: str) -> None:
        self.pool = pool
        super().__init__([pool.root_dataset, name])
        self._mnts: "list[Mount]" = []
        for (dev, _mnt, ty) in [
            ("udev", "dev", "devtmpfs"),
            ("none", "proc", "proc"),
            ("none", "sys", "sysfs"),
        ]:
            self._mnts.append(Mount(dev, Path(_mnt), ty))

        if booted_as_uefi():
            self._mnts.append(Mount("efivars", EFIVARS, "efivars"))

    @property
    def be_name(self):
        return self.name.split("/")[-1]

    def keystore(self):
        keystore = self.get_prop(KEYSOURCE_PROP, str)
        if keystore:
            return Dataset(keystore)

    def mount(self, path: Path = None):
        chroot, path = super().mount(path)
        for m in self._mnts:
            m.mount(chroot=self.chroot)
        keystore = self.keystore()
        if keystore:
            chroot.run(zfs("mount", keystore.name))
        return chroot, path

    def unmount(self, check=True):
        keystore = self.keystore()
        if keystore:
            self.chroot.run(zfs("unmount", keystore.name))
        for m in self._mnts:
            m.unmount(True, chroot=self.chroot)
        super().unmount(check)

