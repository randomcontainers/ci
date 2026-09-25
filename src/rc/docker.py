"""The docker CLI calls that check-image and test-image make."""

import json
import subprocess
from dataclasses import dataclass

from rc.errors import RcError


@dataclass
class Result:
    returncode: int
    stdout: str
    stderr: str


class Docker:
    def __init__(self, binary: str = "docker"):
        self.binary = binary

    def _run(self, args: list[str], *, capture: bool = True, timeout: float | None = None) -> Result:
        try:
            proc = subprocess.run(
                [self.binary, *args],
                capture_output=capture,
                text=True,
                timeout=timeout,
                check=False,
            )
        except FileNotFoundError:
            raise RcError(f"{self.binary} is not installed") from None
        except subprocess.TimeoutExpired:
            raise RcError(f"docker {args[0]} timed out") from None
        return Result(proc.returncode, proc.stdout or "", proc.stderr or "")

    def inspect(self, image: str) -> dict:
        res = self._run(["image", "inspect", "--format", "{{json .Config}}", image])
        if res.returncode != 0:
            raise RcError(f"cannot inspect {image}: {res.stderr.strip()}")
        return json.loads(res.stdout)

    def shell(self, image: str, script: str, *, user: str = "0:0", network: str = "none", timeout: float = 900) -> Result:
        """Run a POSIX sh script inside a throwaway container of the image."""
        return self._run(
            [
                "run",
                "--rm",
                "--pull",
                "never",
                "--network",
                network,
                "--user",
                user,
                "--entrypoint",
                "sh",
                image,
                "-c",
                script,
            ],
            timeout=timeout,
        )

    def imagetools_raw(self, ref: str) -> dict:
        res = self._run(["buildx", "imagetools", "inspect", "--raw", ref], timeout=300)
        if res.returncode != 0:
            raise RcError(f"cannot inspect {ref}: {res.stderr.strip()}")
        return json.loads(res.stdout)

    def pull(self, image: str) -> None:
        res = self._run(["pull", "--quiet", image], timeout=600)
        if res.returncode != 0:
            raise RcError(f"cannot pull {image}: {res.stderr.strip()}")

    def exists(self, image: str) -> bool:
        return self._run(["image", "inspect", image]).returncode == 0

    def run_test(self, image: str, command: str, *, workdir: str, user: str, env: dict[str, str], timeout: float = 1800) -> int:
        args = ["run", "--rm", "--pull", "never", "--user", user, "-v", f"{workdir}:/work", "-w", "/work"]
        for key, value in env.items():
            args += ["-e", f"{key}={value}"]
        args += ["--entrypoint", "sh", image, "-euc", command]
        return self._run(args, capture=False, timeout=timeout).returncode
