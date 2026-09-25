"""docker buildx bake files for one build leg (one distro on one platform).

The same targets are built twice. The load build tags the images locally
for check-image and test-image. The push build runs after the tests with
the same inputs, so BuildKit serves it from the cache of the first build,
and pushes the platform image by digest with provenance and an SBOM.
"""

from collections.abc import Callable
from pathlib import Path

from rc import names
from rc.errors import RcError

GROUP = "build"
MODES = ("load", "push")


def local_tag(plan: dict, flavour: str, distro: str, prefix: str) -> str:
    return f"{prefix}{plan['name']}:{flavour}-{distro}"


def bake_document(
    plan: dict,
    distro: str,
    platform: str,
    mode: str,
    *,
    source_dir: str = "src",
    work_dir: str = ".rc-build",
    tag_prefix: str = "local/",
    jobs: int | None = None,
) -> tuple[dict, dict[str, str]]:
    """Return the bake document and the files it needs written, keyed by path."""
    if mode not in MODES:
        raise RcError(f"unknown mode {mode!r}")
    if platform not in {p["platform"] for p in plan["platforms"]}:
        raise RcError(f"{platform} is not a platform of this plan")
    targets = [t for t in plan["targets"] if t["distro"] == distro]
    if not targets:
        raise RcError(f"the plan has nothing to build for {distro}")

    files: dict[str, str] = {}
    doc_targets: dict[str, dict] = {}
    for target in targets:
        build = target["build"]
        args = {**build.get("args", {}), "SOURCE_DATE_EPOCH": plan["epoch"]}
        if jobs:
            args["JOBS"] = str(jobs)
        spec: dict = {
            "platforms": [platform],
            "args": args,
            "labels": target["labels"],
        }
        if target["flavour"] == "slim":
            spec.update(context=source_dir, dockerfile=build["dockerfile"], target=build["target"])
        else:
            context = f"{work_dir}/{distro}-default"
            files[f"{context}/Dockerfile"] = build["dockerfile_text"]
            spec.update(context=context, dockerfile="Dockerfile", contexts=dict(build["contexts"]))
            if "target:slim" in spec["contexts"].values() and not any(t["flavour"] == "slim" for t in targets):
                raise RcError("the default image needs the slim target, which this plan does not build")
        if mode == "load":
            spec["tags"] = [local_tag(plan, target["flavour"], distro, tag_prefix)]
            spec["output"] = ["type=docker"]
            spec["attest"] = ["type=provenance,disabled=true"]
        else:
            spec["output"] = [f"type=image,name={plan['image']},push-by-digest=true,name-canonical=true,push=true"]
            spec["attest"] = ["type=provenance,mode=max", "type=sbom"]
        doc_targets[target["flavour"]] = spec
    doc = {"group": {GROUP: {"targets": list(doc_targets)}}, "target": doc_targets}
    return doc, files


def record_digests(
    plan: dict,
    distro: str,
    platform: str,
    push_meta: dict,
    load_meta: dict | None,
    out_dir: Path,
    inspect_raw: Callable[[str], dict] | None = None,
) -> tuple[list[Path], list[str]]:
    """Write one file per pushed image for the merge job: <flavour>-<arch> holding the digest.

    When the pushed index can be read, also check that its image manifest
    is the one that was loaded and tested. Both builds use the same inputs
    and SOURCE_DATE_EPOCH, so they must produce the same manifest.
    """
    arch = next(p["arch"] for p in plan["platforms"] if p["platform"] == platform)
    os_name, _, cpu = platform.partition("/")
    out_dir.mkdir(parents=True, exist_ok=True)
    written, notes = [], []
    for target in (t for t in plan["targets"] if t["distro"] == distro):
        flavour = target["flavour"]
        digest = (push_meta.get(flavour) or {}).get("containerimage.digest", "")
        if not names.DIGEST.match(digest):
            raise RcError(f"the push build reported no digest for {flavour}")
        tested = (load_meta or {}).get(flavour, {}).get("containerimage.descriptor", {})
        if inspect_raw and tested.get("mediaType", "").endswith("manifest.v1+json"):
            index = inspect_raw(f"{plan['image']}@{digest}")
            pushed = [
                m["digest"]
                for m in index.get("manifests", [])
                if (m.get("platform") or {}).get("os") == os_name and (m.get("platform") or {}).get("architecture") == cpu
            ]
            if pushed != [tested["digest"]]:
                raise RcError(f"the pushed {flavour} image {pushed} is not the image that was tested ({tested['digest']})")
        else:
            notes.append(f"could not compare the tested and pushed {flavour} images")
        path = out_dir / f"{flavour}-{arch}"
        path.write_text(digest + "\n", encoding="utf-8")
        written.append(path)
    return written, notes


def write_files(files: dict[str, str], root: Path = Path(".")) -> None:
    for rel, content in files.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
