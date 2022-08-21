import argparse
from pathlib import Path
from truenas_install import (
    DiskUse,
    TrueNASInstaller,
    AVATAR_PROJECT,
    AVATAR_VERSION,
    TruenasDisk,
)
import psutil
from .interactive import Interactive
from .utils import Command, UnitPrefix
from .diskutils import Disk
from .bootpool import BootPool


class InteractiveInstaller:
    TITLE = f"{AVATAR_PROJECT} {AVATAR_VERSION} Console Setup"

    def __init__(self) -> None:
        self.gui = Interactive(False)
        self.installer = TrueNASInstaller()

    def encryption_menu(self, encryption: str, passphrade: str):
        ok = True
        tag = None
        encryption = encryption.lower()
        while ok:
            options = [
                ("off", "No Encryption", encryption == "off" or not encryption),
                ("on", "Default Encryption", encryption == True or encryption == "on"),
            ]
            for method in [
                "aes-128-ccm ",
                "aes-192-ccm",
                "aes-256-ccm",
                "aes-128-gcm",
                "aes-192-gcm",
                "aes-256-gcm",
            ]:
                options.append(
                    (
                        method,
                        method.upper().replace("-", " "),
                        method == str(encryption).lower(),
                    )
                )
            if tag == "Encryption":
                ok, selected = self.gui.radiobox("Select Encryption", choices=options)
                if ok:
                    encryption = selected
                    if encryption == "off":
                        passphrade = None
            elif tag == "Passphrase":
                if encryption:
                    r = self.gui.password_prompt("Enter your passpharse")
                    if r is not None:
                        passphrade = r

            ok, tag = self.gui.menu(
                "Encryption Options. Enabling encryption will also create a keystore dataset.",
                title=self.TITLE,
                choices=[
                    ("Encryption", encryption if encryption else "off"),
                    (
                        "Passphrase",
                        ("Not Set" if passphrade is None else "Set")
                        if encryption
                        else "N/A",
                    ),
                ],
                labels=("Set", "Back"),
            )
        return encryption, passphrade

    def keystore(self, name: str, path: str):
        fields = [
            ("Dataset Name", name or ""),
            ("Mountpoint", path or ""),
        ]

        name, path = self.gui.form(
            "Set Keystore Options. Dataset Name blank to disable",
            fields=fields,
        )
        name = name.strip()
        path = path.strip()
        if name == "":
            name = None
            path = None
        elif path == "":
            path = f"/etc/{name}"

        return name, path

    def main_menu(self):
        ok = True
        tag = None
        bootloader_extra: "list[Disk]" = []

        if not self.pre_install_check():
            return False

        while ok:
            if ok == "Install":
                if self.new_install_verify() and self.installer.install():
                    if self.installer.doing_upgrade:
                        self.gui.msg(
                            f"The installer has preserved your database file.\n{AVATAR_PROJECT} will migrate this file, if necessary, to the current format."
                        )
                    msg = f"The {AVATAR_PROJECT} {self.installer.action} on {self.installer.disks} succeeded!\nPlease reboot and remove the installation media."
                    self.gui.msg(msg)
                    exit(0)
                else:
                    exit(1)
            if tag == "BootPool Devices":
                selected = self.gui.devs_select(
                    f"Install {AVATAR_PROJECT} to a drive. Multiple drives can be selected to provide redundancy. Chosen drives are not available for use in the TrueNAS UI",
                    self.installer.disks,
                    1,
                    filter=lambda dev: isinstance(dev, Disk) and not dev.mounted(),
                )
                if selected is not None:
                    self.installer.disks = selected
                    self.confirm_swap()

            elif tag == "Bootloader only Devices":
                selected = self.gui.devs_select(
                    f"Install bootloader to a drive. Multiple drives can be selected to provide redundancy. Chosen drives are not available for use in the TrueNAS UI",
                    bootloader_extra,
                    1,
                    filter=lambda dev: isinstance(dev, Disk) and not dev.mounted(),
                )
                if selected is not None:
                    bootloader_extra = selected
            elif tag == "Root Password":
                r = self.gui.password_prompt("Enter your root password")
                if r is not None:
                    self.installer.password = r
            elif tag == "BootPool Encryption":
                self.installer.encryption = self.encryption_menu(
                    *self.installer.encryption
                )
            elif tag == "Keystore":
                self.installer.keystore, self.installer.keystore_path = self.keystore(
                    self.installer.keystore, self.installer.keystore_path
                )

            if (
                self.installer.encryption[0] != "off"
                and self.installer.keystore is None
            ):
                self.installer.keystore, self.installer.keystore_path = (
                    "keystore",
                    "/etc/keystore",
                )

            ok, tag = self.gui.menu(
                "Install",
                title=self.TITLE,
                choices=[
                    ("BootPool Devices", f"{self.installer.disks}"),
                    (
                        "Root Password",
                        "Not Set" if self.installer.password is None else "Set",
                    ),
                    ("Bootloader only Devices", f"{bootloader_extra}"),
                    ("BootPool Encryption", self.installer.encryption[0]),
                    (
                        "Keystore",
                        f"{self.installer.keystore}:{self.installer.keystore_path}"
                        if self.installer.keystore
                        else "Not Enabled",
                    ),
                ],
                labels=("Set", "Back"),
                extra="Install",
            )
            print(ok)

    def new_install_verify(self):
        msg = "\nWARNING:\n"
        if self.installer.upgrade_type == "inplace":
            msg += f"- This will install into existing zpool on {self.installer.disks}."
        else:
            msg += (
                f"- This will erase ALL partitions and data on {self.installer.disks}."
            )
        msg += f"""
NOTE:
- Installing on SATA, SAS, or NVMe flash media is recommended.
  USB flash sticks are discouraged.

Proceed with the {self.installer.upgrade_type}?
"""
        return self.gui.confirm(
            msg, title=f"{AVATAR_PROJECT} {self.installer.upgrade_type}"
        )

    def pre_install_check(self):
        # We need at least 8 GB of RAM
        # minus 1 GB to allow for reserved memory
        minmem = 7 * UnitPrefix.GiB
        memsize = psutil.virtual_memory().total
        if memsize < minmem:
            if not self.gui.confirm(
                "This computer has less than the recommended 8 GB of RAM.\n\nOperation without enough RAM is not recommended.  Continue anyway?",
                default=False,
                title=AVATAR_PROJECT,
            ):
                False
        return True

    def confirm_swap(self):
        if self.installer.swap is None:
            # Check every disk in $@, aborting if an unsafe disk is found.
            for disk in self.installer.disks:
                if disk.not_swap_safe():
                    self.installer.swap = False
                    break

        # Make sure we have a valid value for swap.
        # If unset, we didn't find an unsafe disk.
        if self.installer.swap is None or self.installer.swap is True:
            # Confirm swap setup
            #   gui.clear()
            self.installer.swap = self.gui.confirm(
                "Create 16GB swap partition on boot devices?",
                labels=("Create Swap", "No Swap"),
            )
        elif self.installer.swap is not False:
            self.installer.logger.error(
                f"Ignoring invalid value for swap: {self.installer.swap}",
            )
            self.installer.swap = None
            self.confirm_swap()


def main():
    parser = argparse.ArgumentParser(
        description=f"Installer for {AVATAR_PROJECT} {AVATAR_VERSION}."
    )
    parser.add_argument(
        "devices",
        metavar="DEVS",
        action="append",
        type=Disk,
        default=[],
        help="Devices on where to install",
        nargs="*",
    )
    parser.add_argument(
        "--interactive",
        help="Interactive CLI GUI for installation",
        default=False,
        action="store_true",
    )

    hostid = Path("/etc/hostid")
    bootpool = BootPool()
    if not hostid.exists():
        Command("zgenhostid").run()

    Command("depmod").run()
    Command("modprobe", "zfs").run()

    args = parser.parse_args()
    gui = Interactive(False)
    installer = TrueNASInstaller()

    if not args.interactive:
        installer.disks = args.devices
        installer.install(args)
    else:
        ok, tag = gui.menu(
            "Main Menu",
            title=f"{AVATAR_PROJECT} {AVATAR_VERSION} Console Setup",
            choices=[
                ("1", "Install"),
                ("2", "Mount Boot Enviroment"),
                ("3", "Unmount Boot Enviroment"),
                ("4", "Install bootloader on device"),
            ],
        )

        if ok is True:
            if tag == "1":
                InteractiveInstaller().main_menu()
            elif tag == "2":
                bootpool.boot_enviroment().mount()
            elif tag == "3":
                bootpool.boot_enviroment().unmount()
            elif tag == "4":
                disks = gui.devs_select(
                    "Select additional bootable devices",
                    [],
                    filter=lambda dev: isinstance(dev, Disk) and not dev.mounted(),
                )
                installer.disks = [
                    TruenasDisk(disk.name, DiskUse.Bootloader) for disk in disks
                ]
                if gui.confirm("Clear Parition Table"):
                    installer.create_partitions(True)
                    reformat = False
                elif gui.confirm("Reformat Bootloader Filesystems"):
                    reformat = True
                installer.install_bootloader(reformat)

        elif ok == "Install":
            print("Install")


if __name__ == "__main__":
    main()
