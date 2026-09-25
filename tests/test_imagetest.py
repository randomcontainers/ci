import os
from pathlib import Path

from rc import imagetest


class FakeDocker:
    """Runs nothing; each command string is a Python action on the /work directory."""

    def __init__(self):
        self.calls = []

    def run_test(self, image, command, *, workdir, user, env, timeout=1800):
        self.calls.append({"image": image, "command": command, "user": user, "env": env})
        work = Path(workdir)
        if command.startswith("write "):
            (work / command.split(" ", 1)[1]).write_text("x")
            return 0
        if command == "fail":
            return 3
        return 0


def groups(*commands, require_output=True):
    return [{"package": "yt-dlp", "version": "2026.08.19", "commands": list(commands), "require_output": require_output}]


def test_passing_tests_leave_an_owned_file(tmp_path):
    docker = FakeDocker()
    report = imagetest.run_tests(docker, "local/yt-dlp:slim-ubuntu", groups("true", "write out.txt"), tmp_path)
    assert report.problems == []
    assert report.passed == 2
    assert docker.calls[0]["user"] == f"{os.getuid()}:{os.getgid()}"
    assert docker.calls[0]["env"] == {"VERSION": "2026.08.19"}
    assert list(tmp_path.iterdir()) == []


def test_missing_output_file_fails(tmp_path):
    report = imagetest.run_tests(FakeDocker(), "img", groups("true"), tmp_path)
    assert len(report.problems) == 1
    assert "wrote no file owned by UID" in report.problems[0]


def test_output_is_optional_for_combo_tests(tmp_path):
    report = imagetest.run_tests(FakeDocker(), "img", groups("true", require_output=False), tmp_path)
    assert report.problems == []


def test_first_failure_stops_the_group(tmp_path):
    docker = FakeDocker()
    report = imagetest.run_tests(docker, "img", groups("fail", "write out.txt"), tmp_path)
    assert report.problems == ["yt-dlp test 1 exited with 3: fail"]
    assert len(docker.calls) == 1


def test_keep_leaves_the_work_directory(tmp_path):
    imagetest.run_tests(FakeDocker(), "img", groups("write out.txt"), tmp_path, keep=True)
    kept = list(tmp_path.iterdir())
    assert len(kept) == 1 and (kept[0] / "out.txt").is_file()
