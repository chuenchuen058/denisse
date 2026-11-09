"""File a pull, checkpoint explicit paths, and report every completed attempt."""
from contextlib import contextmanager
import datetime as dt
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import time

from . import conf
from .browser import session
from .scan import discover, eligible


@contextmanager
def exclusive(profile, config):
    """OS lock released on process exit; a crash does not leave a stale lock."""
    profile.mkdir(parents=True, exist_ok=True)
    with (profile / config["paths"]["lock"]).open("a+b") as handle:
        if handle.tell() == 0:
            handle.write(b"\0")
            handle.flush()
        handle.seek(0)
        if os.name == "nt":
            import msvcrt
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        try:
            yield
        finally:
            handle.seek(0)
            if os.name == "nt":
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(handle, fcntl.LOCK_UN)


def clean(name, config):
    c = config["naming"]
    for rule in c["clean"]:
        name = re.sub(rule["pattern"], rule["replacement"], name)
    name = name.strip(c["trim"])
    if re.search(c["reserved"], name):
        name = c["reserved_prefix"] + name
    if not name:
        raise ValueError("Empty cleaned filename")
    return name


def title(folder, config):
    for pattern in config["naming"]["title_remove"]:
        folder = re.sub(pattern, "", folder)
    return clean(folder, config)


def destination_name(resource, config):
    c = config["naming"]
    original = Path(resource.original)
    if original.suffix.lower() not in c["preserve_extensions"]:
        for rule in c["rules"]:
            if re.search(rule["pattern"], resource.label + " " + original.stem):
                return clean(rule["stem"] + original.suffix, config)
    return clean(resource.original, config)


def unique(path, reserved, config):
    candidate, n = path, config["naming"]["collision_start"]
    while candidate.exists() or candidate in reserved:
        candidate = path.with_name(config["naming"]["collision"].format(
            stem=path.stem, suffix=path.suffix, number=n))
        n += 1
    reserved.add(candidate)
    return candidate


def atomic_json(path, data):
    # The temporary file is beside the target so os.replace stays on one filesystem.
    with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent,
                                     delete=False) as handle:
        temporary = Path(handle.name)
        json.dump(data, handle, ensure_ascii=False, indent=2)
        handle.write("\n")
    try:
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def record(vault, config, now, count, dry, seconds, error, touched):
    from atrium import handoff
    from scripts.vault_git import commit_owned
    suffix = config["output"]["dry_suffix"] if dry else ""
    line = config["output"]["handoff"].format(count=count, dry=suffix, seconds=seconds,
                                             status=str(error) if error else "complete")
    note = handoff.path(now.date().isoformat(), vault=vault)
    conf.inside(vault, note.relative_to(vault), config)
    if not handoff.remember(config["output"]["handoff_agent"], [line], vault=vault, now=now):
        raise RuntimeError("Handoff was not written")
    touched.append(note)
    files = sorted({p.relative_to(vault).as_posix() for p in touched})
    return commit_owned(vault, files, config["output"]["commit"].format(
        count=count, date=now.date().isoformat()))


def notify(count, config):
    c = config["schedule"]
    script = Path(__file__).resolve().parents[1] / c["toast_script"]
    subprocess.run([c["powershell"], "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(script),
                    "-Title", config["output"]["toast_title"],
                    "-Message", config["output"]["toast"].format(count=count)],
                   check=True, capture_output=True, timeout=c["toast_timeout_s"],
                   creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))


def start_ingest(vault, profile, modules, config):
    command = [part.format(python=sys.executable, vault=str(vault), modules=json.dumps(modules))
               for part in config["ingest"]["worker_command"]]
    with (profile / config["ingest"]["log"]).open("a", encoding="utf-8") as log:
        subprocess.Popen(command,
                         cwd=Path(__file__).resolve().parents[1], stdin=subprocess.DEVNULL,
                         stdout=log, stderr=log,
                         creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))


def run(config, vault, *, dry=False, ingest=False, unattended=False,
        browser_factory=session, recorder=record, notifier=notify, ingester=start_ingest, say=print):
    started, now = time.perf_counter(), dt.datetime.now()
    vault, profile = Path(vault).resolve(), conf.profile(config)
    manifest_path = conf.inside(vault, config["paths"]["manifest"], config)
    added, planned, touched, new_weeks, reserved = [], [], [], {}, set()
    failure = None
    with exclusive(profile, config):
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8-sig"))
            if (not isinstance(manifest.get("courses"), dict) or not manifest["courses"]
                    or not isinstance(manifest.get("captured"), list)
                    or not all(isinstance(n, str) for n in manifest["captured"])):
                raise ValueError("Manifest needs courses mapping and captured filename list")
            captured = set(manifest["captured"])
            with browser_factory(config, unattended=unattended, say=say) as browser:
                # Discover all courses before writing, so parser/login errors cannot
                # make a partially scanned run appear complete.
                resources = list(discover(browser, manifest, config))
                for resource in resources:
                    if resource.original in captured or not eligible(resource, config):
                        continue
                    folder = conf.inside(vault, manifest_path.parent.relative_to(vault) / resource.module, config)
                    week = conf.inside(vault, folder.relative_to(vault) /
                                       config["paths"]["week"].format(week=resource.week), config)
                    target = unique(conf.inside(vault, week.relative_to(vault) /
                                    destination_name(resource, config), config), reserved, config)
                    planned.append(target)
                    if not dry:
                        # Stage outside Downloads; auto_organize cannot race this file.
                        with tempfile.TemporaryDirectory(dir=profile) as staging:
                            payload = Path(staging) / target.name
                            browser.download(resource.url, payload)
                            if not payload.is_file() or not payload.stat().st_size:
                                raise ValueError("Empty download: " + resource.original)
                            if not week.exists():
                                new_weeks[week] = (resource.module, resource.week)
                            week.mkdir(parents=True, exist_ok=True)
                            with target.open("xb") as output, payload.open("rb") as source:
                                try:
                                    shutil.copyfileobj(source, output)
                                except Exception:
                                    output.close()
                                    target.unlink()
                                    raise
                        touched.append(target)
                        added.append(resource.original)
                    captured.add(resource.original)
        except Exception as exc:
            failure = exc
        try:
            if not dry:
                for week, (module_folder, number) in new_weeks.items():
                    module = title(module_folder, config)
                    name = config["paths"]["note"].format(module=module, week=number)
                    note = conf.inside(vault, week.relative_to(vault) / name, config)
                    links = "\n".join(config["note"]["link"].format(
                        path=p.relative_to(vault).as_posix(), label=p.stem)
                        for p in touched if p.parent == week)
                    text = config["note"]["template"].format(
                        name_yaml=json.dumps(note.stem, ensure_ascii=False), module=module, week=number,
                        previous=number - 1, next=number + 1, date=now.date().isoformat(), links=links)
                    with note.open("x", encoding="utf-8", newline="\n") as output:
                        output.write(text)
                    touched.append(note)
                if added or failure is None:
                    # Read again immediately before editing: retain concurrent edits
                    # and unknown manifest metadata; captured remains original names.
                    latest = json.loads(manifest_path.read_text(encoding="utf-8-sig"))
                    latest["captured"] = list(dict.fromkeys([*latest["captured"], *added]))
                    if failure is None:
                        latest["last_sync"] = now.date().isoformat()
                    atomic_json(manifest_path, latest)
                    touched.append(manifest_path)
            seconds = time.perf_counter() - started
            count = len(planned) if dry else len(added)
            if added:
                try:
                    notifier(len(added), config)
                except Exception as exc:
                    failure = failure or exc
            recorder(vault, config, now, count, dry, seconds, failure, touched)
        except Exception as exc:
            if failure:
                raise RuntimeError(f"{failure}; finalizing pull also failed: {exc}") from exc
            raise
        if failure:
            raise failure
        if not dry and (ingest or config["ingest"]["enabled"]):
            ingester(vault, profile, [title(m, config) for m in manifest["courses"].values()], config)
        result = {"count": count, "seconds": time.perf_counter() - started,
                  "dry": dry, "files": [str(p) for p in planned]}
        say(config["output"]["summary"].format(**{**result,
            "dry": config["output"]["dry_suffix"] if dry else ""}))
        for path in planned:
            say(config["output"]["file"].format(path=path.relative_to(vault).as_posix()))
        return result
