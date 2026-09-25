"""rc test-image: run the package tests inside a built image.

Each test entry runs with `sh -euc` as the calling user's UID and GID, in
/work, which is a fresh bind-mounted directory per package. That is the
documented way to run the images, so the tests also prove that an
arbitrary UID with HOME=/ can write its output to the host. Every package
test list must leave at least one file owned by that UID.
"""

import os
import shutil
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

from rc import gha
from rc.docker import Docker


@dataclass
class ImageTestReport:
    problems: list[str] = field(default_factory=list)
    passed: int = 0


def _owned_files(root: Path, uid: int) -> list[Path]:
    found = []
    for path in root.rglob("*"):
        try:
            if path.is_file() and not path.is_symlink() and path.stat().st_uid == uid:
                found.append(path)
        except OSError:
            continue
    return found


def run_tests(docker: Docker, image: str, groups: list[dict], work_root: Path | None = None, keep: bool = False) -> ImageTestReport:
    report = ImageTestReport()
    uid, gid = os.getuid(), os.getgid()
    user = f"{uid}:{gid}"
    if work_root is not None:
        work_root.mkdir(parents=True, exist_ok=True)
    for group in groups:
        package, commands = group["package"], group["commands"]
        workdir = Path(tempfile.mkdtemp(prefix=f"rc-test-{package}-", dir=work_root))
        workdir.chmod(0o755)
        env = {"VERSION": group["version"]}
        failed = False
        for i, command in enumerate(commands, 1):
            gha.group(f"{package} test {i}/{len(commands)}: {command.splitlines()[0][:100]}")
            code = docker.run_test(image, command, workdir=str(workdir), user=user, env=env)
            gha.endgroup()
            if code != 0:
                report.problems.append(f"{package} test {i} exited with {code}: {command}")
                failed = True
                break
            report.passed += 1
        if not failed and group.get("require_output"):
            if not _owned_files(workdir, uid):
                report.problems.append(
                    f"{package} tests wrote no file owned by UID {uid} into /work; "
                    "at least one test must write its output there"
                )
        if not keep:
            shutil.rmtree(workdir, ignore_errors=True)
    return report
