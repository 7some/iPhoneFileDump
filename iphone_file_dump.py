#!/usr/bin/env python3
r"""Scan an iPhone connected over USB and copy its user files to this computer.

Setup (once)
    1. Windows only: install "Apple Devices" from the Microsoft Store (or iTunes).
       It provides the USB driver the script talks through.
    2. pip install -U pymobiledevice3

Run
    python iphone_copy.py                       scan, show a summary, ask, then copy
    python iphone_copy.py --dest "D:\iPhone"    choose where the files go
    python iphone_copy.py --scan-only           only list what is on the phone
    python iphone_copy.py --yes                 copy without asking
    python iphone_copy.py --no-apps             photos and media only
    python iphone_copy.py --everything          also copy thumbnails, caches and databases

Unlock the iPhone, tap "Trust" when it asks, and keep it unlocked while copying.
Running the script again resumes: files that were already copied are skipped.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import csv
import errno
import os
import posixpath
import re
import shutil
import sys
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

try:
    from pymobiledevice3.exceptions import (
        AfcException,
        ConnectionFailedToUsbmuxdError,
        NoDeviceConnectedError,
        NotPairedError,
        NotTrustedError,
        PairingError,
        PasscodeRequiredError,
        PyMobileDevice3Exception,
    )
    from pymobiledevice3.lockdown import create_using_usbmux
    from pymobiledevice3.services.afc import AfcService
    from pymobiledevice3.services.house_arrest import HouseArrestService
    from pymobiledevice3.services.installation_proxy import InstallationProxyService
except ImportError:
    sys.exit("pymobiledevice3 is missing or too old. Run:  pip install -U pymobiledevice3")

if not asyncio.iscoroutinefunction(create_using_usbmux):
    sys.exit("This script needs a newer pymobiledevice3. Run:  pip install -U pymobiledevice3")

CHUNK_SIZE = 4 * 1024 * 1024
MAX_CONSECUTIVE_FAILURES = 25

# Folders in the phone's media area that hold caches and databases rather than your files.
SKIPPED_MEDIA_FOLDERS = {"PublicStaging", "MediaAnalysis", "AirFair"}
# PhotoData is thumbnails and the photo library database, except for this sub-folder,
# which holds the edited versions of your photos.
PHOTODATA_KEEP = "Mutations"

WINDOWS_BAD_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
WINDOWS_RESERVED = {"CON", "PRN", "AUX", "NUL"} | {f"{p}{i}" for p in ("COM", "LPT") for i in range(1, 10)}


class Abort(Exception):
    """Stops the run with a message for the user."""


@dataclass(frozen=True)
class Source:
    label: str
    folder: Path
    root: str
    bundle_id: str | None = None  # None means the shared photos-and-media area


@dataclass
class RemoteFile:
    source: Source
    remote: str
    size: int
    modified: datetime
    local: Path = Path()


def safe_name(name: str) -> str:
    name = WINDOWS_BAD_CHARS.sub("_", name).rstrip(" .") or "_"
    if name.split(".")[0].upper() in WINDOWS_RESERVED:
        name = "_" + name
    return name


def long_path(path: Path) -> Path:
    """Lets Windows handle paths longer than 260 characters."""
    path = path.resolve()
    text = str(path)
    if os.name != "nt" or text.startswith("\\\\?\\"):
        return path
    if text.startswith("\\\\"):
        return Path("\\\\?\\UNC\\" + text[2:])
    return Path("\\\\?\\" + text)


def human_size(size: float) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024:
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} TB"


def reason(error: BaseException) -> str:
    """The first line of an error, without the library's bug-report boilerplate."""
    return (str(error).strip().splitlines() or [type(error).__name__])[0]


def status_line(text: str) -> None:
    width = shutil.get_terminal_size((100, 20)).columns - 1
    sys.stdout.write("\r" + text[:width].ljust(width))
    sys.stdout.flush()


def clear_status_line() -> None:
    status_line("")
    sys.stdout.write("\r")


def wanted_media_path(path: str) -> bool:
    parts = path.strip("/").split("/")
    if parts[0] in SKIPPED_MEDIA_FOLDERS:
        return False
    if parts[0] == "PhotoData":
        return len(parts) == 1 or parts[1] == PHOTODATA_KEEP
    return True


async def connect():
    try:
        return await create_using_usbmux()
    except ConnectionFailedToUsbmuxdError:
        raise Abort(
            "Could not reach Apple's USB service.\n"
            '  Windows: install "Apple Devices" from the Microsoft Store (or iTunes), then replug the phone.\n'
            "  Linux:   install and start usbmuxd."
        ) from None
    except NoDeviceConnectedError:
        raise Abort("No iPhone found. Plug it in with a data-capable cable and unlock it.") from None
    except (PairingError, NotTrustedError, NotPairedError, PasscodeRequiredError):
        raise Abort('Unlock the iPhone, tap "Trust" on the prompt, then run this again.') from None
    except PyMobileDevice3Exception as error:
        raise Abort(f"Could not connect to the iPhone: {reason(error)}") from None


async def find_sources(lockdown, include_apps: bool) -> list[Source]:
    sources = [Source("Photos and media", Path("Media"), "/")]
    if not include_apps:
        return sources

    try:
        async with InstallationProxyService(lockdown) as proxy:
            apps = await proxy.get_apps(application_type="User")
    except PyMobileDevice3Exception as error:
        print(f"Could not list apps ({reason(error)}); continuing with photos and media only.")
        return sources

    taken: set[str] = set()
    shared = {bundle_id: info for bundle_id, info in apps.items() if info.get("UIFileSharingEnabled")}
    for bundle_id, info in sorted(shared.items()):
        name = str(info.get("CFBundleDisplayName") or info.get("CFBundleName") or bundle_id)
        folder = safe_name(name)
        if folder.casefold() in taken:
            folder = safe_name(f"{name} ({bundle_id})")
        taken.add(folder.casefold())
        sources.append(Source(name, Path("Apps") / folder, "/Documents", bundle_id))
    return sources


async def open_source(lockdown, source: Source):
    if source.bundle_id is None:
        return AfcService(lockdown)
    return await HouseArrestService.create(lockdown, source.bundle_id, documents_only=True)


async def scan_source(lockdown, source: Source, everything: bool) -> tuple[list[RemoteFile], int]:
    """Returns the files found and how many entries could not be read."""
    files: list[RemoteFile] = []
    unreadable = 0
    filter_media = source.bundle_id is None and not everything
    last_update = 0.0

    async with await open_source(lockdown, source) as afc:
        pending = [source.root]
        while pending:
            directory = pending.pop()
            try:
                names = sorted(await afc.listdir(directory))
            except AfcException:
                unreadable += 1
                continue

            for name in names:
                if name in ("", ".", ".."):
                    continue
                path = posixpath.join(directory, name)
                if filter_media and not wanted_media_path(path):
                    continue
                try:
                    info = await afc.stat(path)
                except AfcException:
                    unreadable += 1
                    continue

                kind = info.get("st_ifmt")
                if kind == "S_IFDIR":
                    pending.append(path)
                elif kind == "S_IFREG":
                    files.append(RemoteFile(source, path, info["st_size"], info["st_mtime"]))

                if time.monotonic() - last_update > 0.1:
                    last_update = time.monotonic()
                    status_line(f"Scanning {source.label}: {len(files):,} files  {directory}")

    clear_status_line()
    files.sort(key=lambda f: f.remote)
    return files, unreadable


def assign_local_paths(files: list[RemoteFile]) -> None:
    """Gives every file a Windows-safe path, unique even on a case-insensitive disk."""
    taken: set[str] = set()
    for file in files:
        relative = posixpath.relpath(file.remote, file.source.root)
        local = file.source.folder.joinpath(*(safe_name(part) for part in relative.split("/")))
        candidate, counter = local, 2
        while str(candidate).casefold() in taken:
            candidate = local.with_name(f"{local.stem} ({counter}){local.suffix}")
            counter += 1
        taken.add(str(candidate).casefold())
        file.local = candidate


def print_summary(files: list[RemoteFile]) -> None:
    groups: dict[str, list[int]] = {}
    for file in files:
        key = "/".join(file.local.parts[:2]) if len(file.local.parts) > 2 else file.local.parts[0]
        entry = groups.setdefault(key, [0, 0])
        entry[0] += 1
        entry[1] += file.size

    width = max((len(key) for key in groups), default=10)
    for key in sorted(groups):
        count, size = groups[key]
        print(f"  {key.ljust(width)}  {count:>8,} files  {human_size(size):>10}")
    print(f"  {'TOTAL'.ljust(width)}  {len(files):>8,} files  {human_size(sum(f.size for f in files)):>10}")


def write_file_list(files: list[RemoteFile], path: Path) -> None:
    with open(path, "w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.writer(handle)
        writer.writerow(["source", "path_on_iphone", "size_bytes", "modified", "path_on_pc"])
        for file in files:
            writer.writerow(
                [file.source.label, file.remote, file.size, file.modified.isoformat(sep=" ", timespec="seconds"), file.local]
            )


def already_copied(file: RemoteFile, target: Path) -> bool:
    try:
        return target.stat().st_size == file.size
    except OSError:
        return False


async def copy_file(afc, file: RemoteFile, target: Path, on_bytes) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    partial = target.with_name(target.name + ".part")
    handle = await afc.fopen(file.remote)
    try:
        remaining = file.size
        with open(partial, "wb") as out:
            while remaining > 0:
                chunk = await afc.fread(handle, min(CHUNK_SIZE, remaining))
                if not chunk:
                    break
                out.write(chunk)
                remaining -= len(chunk)
                on_bytes(len(chunk))
        if remaining:
            raise AfcException(f"File ended {human_size(remaining)} early", None, file.remote)
        os.replace(partial, target)
    except BaseException:
        with contextlib.suppress(OSError):
            partial.unlink()
        raise
    finally:
        with contextlib.suppress(Exception):
            await afc.fclose(handle)

    timestamp = file.modified.timestamp()
    os.utime(target, (timestamp, timestamp))


async def copy_all(lockdown, files: list[RemoteFile], dest: Path) -> list[tuple[RemoteFile, str]]:
    total_bytes = sum(f.size for f in files)
    done_bytes = 0
    done_files = 0
    failures: list[tuple[RemoteFile, str]] = []
    consecutive_failures = 0
    started = time.monotonic()
    last_update = 0.0
    current = ""

    def show(force: bool = False) -> None:
        nonlocal last_update
        now = time.monotonic()
        if not force and now - last_update < 0.1:
            return
        last_update = now
        percent = 100 * done_bytes / total_bytes if total_bytes else 100
        speed = done_bytes / max(now - started, 0.001)
        status_line(
            f"{percent:5.1f}%  {done_files:,}/{len(files):,} files  "
            f"{human_size(done_bytes)}/{human_size(total_bytes)}  {human_size(speed)}/s  {current}"
        )

    def on_bytes(count: int) -> None:
        nonlocal done_bytes
        done_bytes += count
        show()

    sources = list(dict.fromkeys(f.source for f in files))
    for source in sources:
        try:
            service = await open_source(lockdown, source)
        except PyMobileDevice3Exception as error:
            raise Abort(f'Lost access to "{source.label}": {reason(error)}') from None

        async with service as afc:
            for file in (f for f in files if f.source is source):
                current = file.local.name
                bytes_before = done_bytes
                try:
                    await copy_file(afc, file, dest / file.local, on_bytes)
                    consecutive_failures = 0
                except AfcException as error:
                    failures.append((file, reason(error)))
                    consecutive_failures += 1
                except (ConnectionError, PyMobileDevice3Exception) as error:
                    raise Abort(f"Lost the connection to the iPhone: {reason(error)}") from None
                except OSError as error:
                    if error.errno == errno.ENOSPC:
                        raise Abort("The destination disk is full.") from None
                    failures.append((file, reason(error)))
                    consecutive_failures += 1

                if consecutive_failures >= MAX_CONSECUTIVE_FAILURES:
                    raise Abort(
                        f"{MAX_CONSECUTIVE_FAILURES} files in a row failed; the phone was probably locked or unplugged."
                    )
                done_bytes = bytes_before + file.size
                done_files += 1
                show()

    show(force=True)
    print()
    return failures


async def run(args: argparse.Namespace) -> int:
    lockdown = await connect()
    try:
        with contextlib.suppress(PyMobileDevice3Exception):
            name = await lockdown.get_value(key="DeviceName")
            print(f"Connected to {name} ({lockdown.display_name}, iOS {lockdown.product_version})")

        files: list[RemoteFile] = []
        unreadable = 0
        for source in await find_sources(lockdown, include_apps=not args.no_apps):
            try:
                found, skipped = await scan_source(lockdown, source, args.everything)
            except (ConnectionError, PyMobileDevice3Exception) as error:
                clear_status_line()
                if source.bundle_id is None:
                    raise Abort(f"Could not read the iPhone's photos and media: {reason(error)}") from None
                print(f'Skipped "{source.label}": {reason(error)}')
                continue
            files += found
            unreadable += skipped

        if not files:
            raise Abort("The scan found no files. Make sure the iPhone is unlocked and trusted, then try again.")

        assign_local_paths(files)
        print("\nFound on the iPhone:")
        print_summary(files)
        if unreadable:
            print(f"  ({unreadable:,} entries could not be read and were left out)")

        dest = long_path(Path(args.dest).expanduser())
        dest.mkdir(parents=True, exist_ok=True)
        shown_dest = Path(args.dest).expanduser().resolve()
        try:
            write_file_list(files, dest / "iphone-files.csv")
            print(f"\nFull file list saved to {shown_dest / 'iphone-files.csv'}")
        except OSError as error:
            print(f"\nCould not save the file list (is iphone-files.csv open in another program?): {error}")

        if args.scan_only:
            return 0

        pending = [f for f in files if not already_copied(f, dest / f.local)]
        if len(pending) < len(files):
            print(f"{len(files) - len(pending):,} files are already in {shown_dest} and will be skipped.")
        if not pending:
            print("Everything is already copied.")
            return 0

        needed = sum(f.size for f in pending)
        free = shutil.disk_usage(dest).free
        if needed > free:
            raise Abort(f"Not enough space in {shown_dest}: need {human_size(needed)}, only {human_size(free)} free.")

        if not args.yes:
            try:
                answer = input(f"Copy {len(pending):,} files ({human_size(needed)}) to {shown_dest}? [y/N] ")
            except EOFError:
                answer = ""
            if answer.strip().lower() not in ("y", "yes"):
                print("Nothing copied.")
                return 0

        failures = await copy_all(lockdown, pending, dest)
        print(f"Copied {len(pending) - len(failures):,} files to {shown_dest}")
        if failures:
            log = dest / "iphone-copy-errors.txt"
            log.write_text("".join(f"{file.remote}\t{reason}\n" for file, reason in failures), encoding="utf-8")
            print(f"{len(failures):,} files failed; see {shown_dest / log.name}. Run again to retry them.")
            return 1
        return 0
    finally:
        with contextlib.suppress(Exception):
            await lockdown.close()


def main() -> int:
    parser = argparse.ArgumentParser(description="Scan an iPhone over USB and copy its user files to this computer.")
    parser.add_argument("--dest", default=str(Path.home() / "iPhone Copy"), help="folder to copy into (default: %(default)s)")
    parser.add_argument("--scan-only", action="store_true", help="list the files without copying")
    parser.add_argument("--yes", "-y", action="store_true", help="copy without asking for confirmation")
    parser.add_argument("--no-apps", action="store_true", help="skip app documents; photos and media only")
    parser.add_argument("--everything", action="store_true", help="include thumbnails, caches and databases")
    args = parser.parse_args()

    try:
        return asyncio.run(run(args))
    except Abort as error:
        clear_status_line()
        print(f"\n{error}")
        return 1
    except KeyboardInterrupt:
        clear_status_line()
        print("\nStopped. Run the script again to continue where it left off.")
        return 130


if __name__ == "__main__":
    sys.exit(main())
