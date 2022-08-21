from pathlib import Path
import re
import subprocess
import sys
import json
from typing import Type

_DEBUG = True

if _DEBUG:
    with Path("./commands_ran.log").open("w") as log:
        log.write("---\n")


class Logger:
    def __init__(self, json_output: bool = False) -> None:
        self.json = json_output

    def msg(self, message: str):
        if self.json:
            sys.stdout.write(json.dumps({"message": message}) + "\n")
        else:
            sys.stdout.write(f"{message}\n")
        sys.stdout.flush()

    def progress(self, progress: float, message: str):
        if self.json:
            sys.stdout.write(
                json.dumps({"progress": progress, "message": message}) + "\n"
            )
        else:
            sys.stdout.write(f"[{int(progress * 100)}%] {message}\n")
        sys.stdout.flush()

    def error(self, error: str, raise_:'Type[Exception|bool]'=False):
        if self.json:
            sys.stdout.write(json.dumps({"error": error}) + "\n")
        else:
            sys.stdout.write(f"Error: {error}\n")
        sys.stdout.flush()

        if raise_:
            ctr = Exception if raise_ is True else raise_
            raise ctr(error)



def run_command(
    cmd,
    check=True,
    stdout=subprocess.PIPE,
    stderr=subprocess.PIPE,
    encoding="utf-8",
    errors="ignore",
    **kwargs,
) -> "subprocess.CompletedProcess[str]":
    # try:
    if _DEBUG:
        with Path("./commands_ran.log").open("a") as log:
            log.write(" ".join([json.dumps(str(a)) for a in cmd]) + "\n")
    return subprocess.run(
        cmd,
        check=check,
        stdout=stdout,
        stderr=stderr,
        encoding=encoding,
        errors=errors,
        **kwargs,
    )


# except subprocess.CalledProcessError as e:
#    write_error(f"Command {cmd} failed with exit code {e.returncode}: {e.stderr}")
#     raise


class Command:
    def __init__(self, cmd: "str|Path", *positionals, **args):
        self.cmd: "list[str]" = [cmd]
        self.add_args(*positionals, **args)

    def with_args(self, *positionals, **args):
        return Command(self.cmd[0], *positionals, **args)

    def add_args(self, *positionals, **args):
        self.cmd.extend([f"{p}" for p in positionals])
        last = None
        for arg in args:
            value = args[arg]
            if args is None or arg == "":
                last = value
            else:
                self.add_named_arg(arg, args[arg])

        if last is not None:
            self.add_args(f"{last}")

        return self

    def add_named_arg(self, arg: str, value=None):
        arg = f"{arg}".strip()
        for value in value if isinstance(value, list) else [value]:
            if len(arg.strip("_")) == 0:
                pass
            elif len(arg) == 1:
                self.cmd.append(f"-{arg}")
            else:
                self.cmd.append(f"--{arg}")
            if value is not None:
                self.cmd.append(f"{value}")
        return self

    def run(
        self,
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        encoding="utf-8",
        errors="ignore",
        **kwargs,
    ):
        return run_command(self.cmd, check, stdout, stderr, encoding, errors, **kwargs)


class UnitPrefix:
    # Constants for base 10 and base 2 units
    kB = 1000
    kiB = 1024
    MB = 1000 * kB
    MiB = 1024 * kiB
    GB = 1000 * MB
    GiB = 1024 * MiB
    TB = 1000 * GB
    TiB = 1024 * GiB

    @staticmethod
    def find_unit(num: int):
        num = float(num)
        for unit in [
            (UnitPrefix.TiB, "TiB"),
            (UnitPrefix.GiB, "GiB"),
            (UnitPrefix.MiB, "MiB"),
            (UnitPrefix.kiB, "kiB"),
            (int(1), "B"),
        ]:
            mult, unit = unit
            cap = num / mult
            if cap > 1.0:
                return (cap, unit)

    @staticmethod
    def format_bytes(num: int):
        (cap, unit) = UnitPrefix.find_unit(num)
        return f"{round(cap, 1).__str__().strip('.0')}{unit}"


class Root:
    def __init__(self, root: "Path|None|str") -> None:
        self.path = Path(root)

    def cmd(self, cmd: Command):
        troot = self.path.__str__() if self.path is not None else ""
        if troot.strip() != "":
            return Command("chroot", self.path, *cmd.cmd)
        else:
            return cmd

    def run(self, cmd: Command, **kwargs):
        return self.cmd(cmd).run(**kwargs)


RE_UNSQUASHFS_PROGRESS = re.compile(
    r"\[.+\]\s+(?P<extracted>[0-9]+)/(?P<total>[0-9]+)\s+(?P<progress>[0-9]+)%"
)


def unsquashfs(
    filesystem: "Path|str",
    dest: "Path|str",
    only: "list[Path|str]" = [],
    **options,
):

    cmd = [
        "unsquashfs",
        "-d",
        f"{dest}",
    ]
    for opt in options:
        value = options[opt]
        cmd.append(f"-{opt}")
        if value is not None:
            cmd.append(f"{value}")

    cmd.append(f"{filesystem}")
    cmd.extend([o.__str__() for o in only])
    if _DEBUG:
        with Path("./commands_ran.log").open("a") as log:
            log.write(" ".join([json.dumps(a) for a in cmd]) + "\n")
    p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    stdout = ""
    buffer = b""
    for char in iter(lambda: p.stdout.read(1), b""):
        buffer += char
        if char == b"\n":
            stdout += buffer.decode("utf-8", "ignore")
            buffer = b""

        if buffer and buffer[0:1] == b"\r" and buffer[-1:] == b"%":
            if m := RE_UNSQUASHFS_PROGRESS.match(buffer[1:].decode("utf-8", "ignore")):
                yield int(m.group("extracted")) / int(m.group("total"))
                buffer = b""

    p.wait()
    if p.returncode != 0:
        raise subprocess.CalledProcessError(p.returncode, cmd, stdout)


def booted_as_uefi():
    return Path("/sys/firmware/efi").exists()
