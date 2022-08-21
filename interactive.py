from typing import NamedTuple, Type, TypeVar
from truenas_install.vendor import dialog
from truenas_install.diskutils import Disk, lsdevs

from .utils import write_log

# gui.add_persistent_args(["--title", AVATAR_PROJECT])
T = TypeVar("T")


class Element(NamedTuple):
    label: str
    yl: int
    xl: int
    item: str
    yi: int
    xi: int
    field_length: int
    input_length: int


class Interactive:
    def __init__(self) -> None:
        self._diag = dialog.Dialog()
        self.def_args = {}

    def confirm(
        self, msg: str, default: bool = True, labels=("yes", "no"), **diag_args
    ) -> bool:
        args = {**self.def_args, **diag_args}
        result = (
            self._diag.yesno(
                msg,
                yes_label=labels[0],
                no_label=labels[1],
                defaultno=not default,
                **args,
            )
            == "ok"
        )
        self._diag.clear()
        return result

    def msg(self, msg: str, **diag_args):
        args = {**self.def_args, **diag_args}
        self._diag.msgbox(msg, **args)
        self._diag.clear()

    def checklist(
        self,
        msg: str,
        choices: "list[(str, str, bool)]" = [],
        ctr: Type[T] = None,
        **diag_args,
    ) -> "tuple[bool, list[T]]":
        args = {**self.def_args, **diag_args}
        (retcode, selected) = self._diag.checklist(
            msg,
            choices=choices,
            **args,
        )
        self._diag.clear()
        return retcode == "ok", ([ctr(s) for s in selected] if ctr else selected)

    def menu(
        self,
        msg: str,
        page_size: int = 0,
        choices: "list[(str, str)]" = [],
        labels=("OK", "Cancel"),
        extra: "str" = None,
        **diag_args,
    ) -> "tuple[bool, str]":
        args = {**self.def_args, **diag_args}
        if extra:
            args["extra_button"] = True
            args["extra_label"] = extra
        code, tag = self._diag.menu(
            msg,
            menu_height=page_size,
            choices=choices,
            ok_label=labels[0],
            cancel_label=labels[1],
            **args,
        )
        self._diag.clear()
        return (code == "ok" if code != "extra" else extra), tag

    def radiobox(
        self,
        msg: str,
        choices: "list[(str, str, bool)]" = [],
        labels=("OK", "Cancel"),
        **diag_args,
    ) -> "tuple[bool, str]":
        args = {**self.def_args, **diag_args}
        code, tag = self._diag.radiolist(
            msg,
            choices=choices,
            ok_label=labels[0],
            cancel_label=labels[1],
            **args,
        )
        self._diag.clear()
        return code == "ok", tag

    def password_prompt(self, msg: str, **diag_args):
        args = {**self.def_args, **diag_args}

        pass1 = ""
        pass2 = ""

        while True:
            fields = [
                Element("Password:", 1, 10, pass1, 0, 30, 25, 50),
                Element("Confirm Password:", 2, 10, pass2, 2, 30, 25, 50),
            ]
            (code, passwords) = self._diag.passwordform(
                msg, fields, insecure=True, **args
            )
            pass1, pass2 = passwords
            if code == "ok":
                if pass1 != pass2:
                    self.msg("Passwords do not match.", 7, 60)
                else:
                    if pass1 != "" or self.confirm("Proceed with no password.", 7, 60):
                        self._diag.clear()
                        return pass1
            else:
                self._diag.clear()
                return None

    def form(self, msg: str, fields: "list[tuple[str,str]]" = [], **diag_args):
        args = {**self.def_args, **diag_args}
        while True:
            _fields = [
                Element(f"{name}:", pos + 1, 10, value, pos * 2, 30, 25, 50)
                for (pos, (name, value)) in enumerate(fields)
            ]
            (code, values) = self._diag.form(msg, _fields, *args)
            if code == "ok":
                self._diag.clear()
                return values
            else:
                self._diag.clear()
                return fields

    def devs_select(
        interactive, msg: str, disks: "list[Disk]" = [], min: int = 1, filter=None
    ):
        _first = True
        while len(disks) < min or _first:
            if not _first:
                interactive.msg(f"You need to select at least {min} disk!")
            _first = False
            valid_disks = [
                (disk.__str__(), disk.description(), disk in disks)
                for disk in lsdevs()
                if (not filter or filter(disk))
            ]
            if len(valid_disks) == 0:
                interactive.msg("No drives available", title="Choose destination media")
                continue

            (ok, disks) = interactive.checklist(
                f"{msg}.\n Arrow keys highlight options, spacebar selects.",
                choices=valid_disks,
                title="Choose destination media",
                ctr=Disk,
            )
            if not ok:
                return

        return disks
