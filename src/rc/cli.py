"""Command line interface: `rc <command> --help` describes each command."""

import argparse
import json
import os
import sys
from datetime import UTC, datetime
from pathlib import Path

from rc import __version__, bake, catalog, checks, commits, gha, http, imagetest, locks, merge, names, release, render, repofiles, reposetup, tags
from rc import plan as planmod
from rc import reconcile as reconcilemod
from rc import status, upstream
from rc.config import (
    Package,
    load_combo_file,
    load_distros,
    load_package,
    load_package_list,
)
from rc.constants import ORG
from rc.docker import Docker
from rc.errors import RcError, ValidationError
from rc.github import GitHub
from rc.http import UrllibTransport
from rc.registry import Registry
from rc.sources import PackageSource
from rc.state import RegistryState

REPO_ROOT = Path(__file__).resolve().parents[2]


def ci_dir(args) -> Path:
    path = Path(args.ci_dir or os.environ.get("RC_CI_DIR") or REPO_ROOT)
    if not (path / "distros.yml").is_file():
        raise RcError(f"{path} has no distros.yml; pass --ci-dir with a checkout of randomcontainers/ci")
    return path


def _distros(args):
    return load_distros(ci_dir(args) / "distros.yml")


def _listed(args) -> tuple[str, ...]:
    return load_package_list(ci_dir(args) / "packages.yml")


def _source(args, distros) -> PackageSource:
    local = Path(args.packages_dir) if getattr(args, "packages_dir", None) else None
    return PackageSource(distros, local_dir=local, transport=UrllibTransport(), org=_org())


def _org() -> str:
    repository = os.environ.get("GITHUB_REPOSITORY", "")
    owner = repository.split("/", 1)[0].lower() if "/" in repository else ""
    return owner if owner and names.NAME.match(owner) else ORG


def _definition(source_dir: Path) -> tuple[str, Path]:
    found = [(kind, source_dir / f"{kind}.yml") for kind in ("package", "combo") if (source_dir / f"{kind}.yml").is_file()]
    if len(found) != 1:
        raise RcError(f"{source_dir} must contain exactly one of package.yml or combo.yml")
    return found[0]


def _bool(value: str) -> bool:
    if value.lower() in ("true", "1", "yes"):
        return True
    if value.lower() in ("false", "0", "no", ""):
        return False
    raise argparse.ArgumentTypeError(f"expected true or false, got {value!r}")


def _write_json(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, sort_keys=False) + "\n", encoding="utf-8")


# validate


def cmd_validate(args) -> int:
    distros = _distros(args)
    listed = _listed(args)
    problems = 0
    paths = [Path(p) for p in args.paths] or [Path(".")]
    for path in paths:
        if path.is_dir():
            kind, path = _definition(path)
        else:
            kind = "combo" if path.name == "combo.yml" else "package"
        try:
            if kind == "package":
                package = load_package(path, distros)
                for combo in package.combos:
                    for member in combo.with_:
                        if member not in listed:
                            raise ValidationError(str(path), [f"combo {combo.name}: {member} is not listed in packages.yml"])
                if args.files:
                    missing = repofiles.package_problems(package, path.parent, distros)
                    missing += repofiles.distro_problems(path.parent, distros)
                    if missing:
                        raise ValidationError(str(path.parent), missing)
            else:
                load_combo_file(path)
        except ValidationError as exc:
            print(exc, file=sys.stderr)
            problems += 1
            continue
        print(f"ok: {path}")

    if args.catalog:
        if not args.packages_dir:
            raise RcError("--catalog needs --packages-dir")
        source = _source(args, distros)
        packages: dict[str, Package] = {}
        for name in listed:
            try:
                packages[name] = source.load(name)
            except RcError as exc:
                print(exc, file=sys.stderr)
                problems += 1
        issues = catalog.validate_catalog(packages, listed)
        for package in packages.values():
            for combo in package.combos:
                if all(m in packages for m in combo.members):
                    try:
                        render.combo_repo_files(combo, packages, distros)
                    except RcError as exc:
                        issues.append(f"{combo.name}: {exc}")
        for issue in issues:
            print(f"catalog: {issue}", file=sys.stderr)
        problems += len(issues)
        if not issues and len(packages) == len(listed):
            print(f"ok: catalog of {len(packages)} packages")
    return 1 if problems else 0


# plan


def _run_context(args, repository: str) -> planmod.RunContext:
    sha = os.environ.get("GITHUB_SHA", "0" * 40)
    if not names.GIT_SHA.match(sha):
        raise RcError(f"GITHUB_SHA {sha!r} is not a commit id")
    want = args.want or ""
    if not names.WANT.match(want):
        raise RcError(f"want {want!r} must match {names.WANT.pattern}")
    return planmod.RunContext(
        repository=repository,
        sha=sha,
        ref=os.environ.get("GITHUB_REF", "refs/heads/local"),
        event_name=os.environ.get("GITHUB_EVENT_NAME", "local"),
        default_only=args.default_only,
        want=want,
        now=datetime.now(UTC).replace(microsecond=0),
    )


def cmd_plan(args) -> int:
    distros = _distros(args)
    listed = _listed(args)
    source = _source(args, distros)
    kind, path = _definition(Path(args.source))
    outdated: list[str] = []
    if kind == "package":
        definition = load_package(path, distros)
        missing = repofiles.package_problems(definition, path.parent, distros)
        if missing:
            raise ValidationError(str(path.parent), missing)
        outdated = repofiles.distro_problems(path.parent, distros)
    else:
        definition = load_combo_file(path)
    org = _org()
    repository = os.environ.get("GITHUB_REPOSITORY") or f"{org}/{definition.name}"
    if not names.GITHUB_REPO.match(repository):
        raise RcError(f"GITHUB_REPOSITORY {repository!r} is not owner/repo")
    run = _run_context(args, repository)
    member_images = {}
    for item in args.member_image or []:
        name, sep, ref = item.partition("=")
        if not sep or not names.NAME.match(name) or not ref:
            raise RcError(f"--member-image expects NAME=IMAGE, got {item!r}")
        member_images[name] = ref
    registry = None if args.offline else Registry(UrllibTransport())
    planner = planmod.Planner(distros, listed, source, registry, run, org=org, member_images=member_images)
    planner.warnings.extend(outdated)
    if kind == "package":
        document = planner.plan_package(definition)
    else:
        document = planner.plan_combo(definition)
    planmod.check(document)
    if kind == "combo":
        for target in document["targets"]:
            committed = Path(args.source) / f"Dockerfile.{target['distro']}"
            if committed.is_file() and committed.read_text(encoding="utf-8") != target["build"]["dockerfile_text"]:
                planner.notices.append(
                    f"{committed.name} differs from the file rendered from the current package.yml files; "
                    "building the rendered file (the reconciler updates the repository)"
                )

    for message in planner.errors:
        gha.error(message)
    for message in planner.warnings:
        gha.warning(message)
    for message in planner.notices:
        gha.notice(message)

    if args.output_file:
        _write_json(Path(args.output_file), document)
    targets = document["targets"]
    compact = json.dumps(document, separators=(",", ":"))
    if len(compact) > 900_000:
        raise RcError("the plan is too large for a job output")
    outputs = {
        "kind": document["kind"],
        "name": document["name"],
        "image": document["image"],
        "version": document["version"],
        "build": "true" if targets else "false",
        "publish": "true" if document["publish"] and targets else "false",
        "release": "true" if document["release"] else "false",
        "matrix": json.dumps(planmod.build_matrix(document), separators=(",", ":")),
        "merge-matrix": json.dumps(planmod.merge_matrix(document), separators=(",", ":")),
        "base-digests": json.dumps(
            {t["distro"]: t["distro_base"].partition("@")[2] for t in targets}, separators=(",", ":")
        ),
        "member-digests": json.dumps(
            {
                t["distro"]: json.loads(t["labels"]["com.randomcontainers.members"])
                for t in targets
                if t["flavour"] == "default"
            },
            separators=(",", ":"),
        ),
        "default-ready": json.dumps(
            {d.id: any(t["flavour"] == "default" and t["distro"] == d.id for t in targets) for d in distros.items},
            separators=(",", ":"),
        ),
        "tags": json.dumps({t["id"]: t["tags"] for t in targets}, separators=(",", ":")),
        "labels": json.dumps({t["id"]: t["labels"] for t in targets}, separators=(",", ":")),
        "plan": compact,
    }
    for key, value in outputs.items():
        if key != "plan" or os.environ.get("GITHUB_OUTPUT"):
            gha.set_output(key, value)
    gha.summary(planmod.summary_markdown(document, planner.errors + planner.notices))
    return 0


# render


def _combo_and_packages(args, distros):
    source = _source(args, distros)
    if args.combo_file:
        combo_file = load_combo_file(Path(args.combo_file))
        owner = source.load(combo_file.owner)
        combo = owner.combo_by_name(combo_file.name)
        if combo is None:
            raise RcError(f"{combo_file.owner} does not declare the combo {combo_file.name}")
    else:
        package_file = Path(args.package_file) if args.package_file else Path(args.source) / "package.yml"
        owner = load_package(package_file, distros)
        source.add(owner)
        combo = owner.combo_by_name(args.combo) if args.combo else owner.default_combo
        if combo is None:
            raise RcError(f"{owner.name} has no {'combo ' + args.combo if args.combo else 'default combo'}")
    packages = {m: source.load(m) for m in combo.members}
    return combo, packages


def cmd_render(args) -> int:
    distros = _distros(args)
    combo, packages = _combo_and_packages(args, distros)
    problems = catalog.combo_problems(combo, packages)
    if problems:
        raise RcError(f"{combo.name}: " + "; ".join(problems))
    if args.repo_files:
        root = Path(args.repo_files)
        files = render.combo_repo_files(combo, packages, distros, _org())
        bake.write_files(files, root)
        for rel in sorted(files):
            print(root / rel)
        return 0
    selected = [distros.get(args.distro)] if args.distro else list(distros.items)
    if args.output and len(selected) > 1:
        raise RcError("--output needs --distro; use --output-dir for all distros")
    for distro in selected:
        text = render.combo_dockerfile(combo, packages, distro, _org())
        if args.output:
            Path(args.output).parent.mkdir(parents=True, exist_ok=True)
            Path(args.output).write_text(text, encoding="utf-8")
        elif args.output_dir:
            out = Path(args.output_dir) / f"Dockerfile.{distro.id}"
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_text(text, encoding="utf-8")
            print(out)
        else:
            sys.stdout.write(text)
    return 0


# bake and digests


def cmd_bake(args) -> int:
    plan = planmod.load(Path(args.plan))
    doc, files = bake.bake_document(
        plan,
        args.distro,
        args.platform,
        args.mode,
        source_dir=args.source_dir,
        work_dir=args.work_dir,
        tag_prefix=args.tag_prefix,
        jobs=args.jobs,
    )
    bake.write_files(files)
    _write_json(Path(args.output), doc)
    print(args.output)
    return 0


def cmd_digests(args) -> int:
    plan = planmod.load(Path(args.plan))
    push_meta = json.loads(Path(args.push_metadata).read_text(encoding="utf-8"))
    load_meta = json.loads(Path(args.load_metadata).read_text(encoding="utf-8")) if args.load_metadata else None
    written, notes = bake.record_digests(
        plan, args.distro, args.platform, push_meta, load_meta, Path(args.output_dir), Docker().imagetools_raw
    )
    for note in notes:
        gha.notice(note)
    for path in written:
        print(f"{path.name}: {path.read_text().strip()}")
    return 0


# check-image and test-image


def _target_image(args, plan) -> tuple[dict, str]:
    target = planmod.find_target(plan, args.target, args.distro)
    image = args.image or bake.local_tag(plan, args.target, args.distro, args.tag_prefix)
    return target, image


def cmd_check_image(args) -> int:
    plan = planmod.load(Path(args.plan))
    target, image = _target_image(args, plan)
    report = checks.check_image(Docker(), image, target)
    for message in report.warnings:
        gha.warning(f"{image}: {message}")
    for message in report.problems:
        gha.error(f"{image}: {message}")
    if report.problems:
        print(f"{image}: {len(report.problems)} problems", file=sys.stderr)
        return 1
    print(f"{image}: follows the image contract")
    return 0


def cmd_test_image(args) -> int:
    plan = planmod.load(Path(args.plan))
    target, image = _target_image(args, plan)
    work = Path(args.work_dir) if args.work_dir else None
    report = imagetest.run_tests(Docker(), image, target["tests"], work, keep=args.keep)
    for message in report.problems:
        gha.error(f"{image}: {message}")
    if report.problems:
        return 1
    print(f"{image}: {report.passed} tests passed")
    return 0


# tags


def cmd_tags(args) -> int:
    rows: list[tuple[str, str, list[str], list[str]]] = []
    if args.plan:
        plan = planmod.load(Path(args.plan))
        for t in plan["targets"]:
            rows.append((t["flavour"], t["distro"], t["tags"], t["floating"]))
    else:
        distros = _distros(args)
        package_file = Path(args.package_file) if args.package_file else Path(args.source) / "package.yml"
        package = load_package(package_file, distros)
        for distro in distros.items:
            slim = tags.package_tags("slim", package.version, package.versioning, distro, distros.default)
            if package.default_combo is None:
                slim.extend(tags.package_tags("default", package.version, package.versioning, distro, distros.default))
            rows.append(("slim", distro.id, slim.tags, slim.floating))
            if package.default_combo is not None:
                full = tags.package_tags("default", package.version, package.versioning, distro, distros.default)
                rows.append(("default", distro.id, full.tags, full.floating))
    rows = [r for r in rows if (not args.distro or r[1] == args.distro) and (not args.flavour or r[0] == args.flavour)]
    if args.json:
        print(json.dumps([{"flavour": f, "distro": d, "tags": t, "floating": fl} for f, d, t, fl in rows], indent=2))
    else:
        for flavour, distro, tag_list, _ in rows:
            print(f"{flavour} {distro}: {' '.join(tag_list)}")
    return 0


# merge


def cmd_merge(args) -> int:
    plan = planmod.load(Path(args.plan))
    target = planmod.find_target(plan, args.flavour, args.distro)
    transport = UrllibTransport()
    credentials = {}
    user, token = os.environ.get("RC_REGISTRY_USER"), os.environ.get("RC_REGISTRY_TOKEN")
    if user and token:
        credentials["ghcr.io"] = (user, token)
    repository = os.environ.get("GITHUB_REPOSITORY", plan["repository"])
    decision = merge.decide(
        plan,
        target,
        Path(args.digests),
        event_name=os.environ.get("GITHUB_EVENT_NAME", "local"),
        ref=os.environ.get("GITHUB_REF", ""),
        sha=os.environ.get("GITHUB_SHA", plan["revision"]),
        head=lambda: merge.main_head(transport, repository, os.environ.get("GITHUB_TOKEN")),
        registry=Registry(transport, credentials),
    )
    gha.set_output("skip", "true" if decision.skip else "false")
    gha.set_output("ref", decision.ref)
    gha.set_output("image", plan["image"])
    if decision.skip:
        gha.notice(f"Not publishing {args.flavour} {args.distro}: {decision.reason}")
        return 0
    for held in decision.held:
        gha.warning(f"{plan['image']}:{held.split(' ')[0]} points at a newer version; left as is: {held}")
    out = Path(args.output)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_bytes(b"".join(arg.encode() + b"\0" for arg in decision.args))
    gha.set_output("tags", " ".join(decision.tags))
    gha.summary(
        f"#### {plan['image']} {target['variant']} {target['distro']}\n\n"
        + "".join(f"- `{t}`\n" for t in decision.tags)
        + "".join(f"- `{h}` held back\n" for h in decision.held)
    )
    return 0


# source-release


def cmd_source_release(args) -> int:
    plan = planmod.load(Path(args.plan))
    info = plan.get("release")
    if not info:
        raise RcError("this plan has no source release")
    out = Path(args.output_dir)
    files = release.fetch(info, out)
    notes_path = out / "NOTES.md"
    notes_path.write_text(release.notes(plan, info, files), encoding="utf-8")
    gha.set_output("tag", info["tag"])
    gha.set_output("title", info["title"])
    gha.set_output("notes", str(notes_path))
    for f in files:
        print(f.path)
    return 0


# reconcile and catalog


def _previous_catalog(path: Path) -> dict | None:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _reconciler(args, dry_run: bool) -> reconcilemod.Reconciler:
    root = ci_dir(args)
    transport = UrllibTransport()
    now = datetime.now(UTC).replace(microsecond=0)
    gh = GitHub(transport, os.environ.get("RC_GITHUB_TOKEN") or None)
    org = _org()
    return reconcilemod.Reconciler(
        ci_dir=root,
        gh=gh,
        ci_gh=GitHub(transport, os.environ.get("RC_CI_TOKEN") or None),
        registry=RegistryState(Registry(transport), org),
        checker=upstream.Checker(gh, transport, now),
        fetch=lambda url, algorithms: http.stream_digests(url, algorithms),
        lock=lambda recipe, version: locks.compile_lock(recipe, version),
        now=now,
        dry_run=dry_run,
        packages_dir=Path(args.packages_dir) if args.packages_dir else None,
        previous_catalog=_previous_catalog(root / "status" / "catalog.json"),
        org=org,
    )


LATER = {"commit": " (by rc apply-commits)", "create": " (by rc setup-repos)", "topics": " (by rc setup-repos)"}


def cmd_reconcile(args) -> int:
    if args.dry_run and (args.output_dir or args.setup_file or args.commit_file):
        raise RcError("--dry-run writes nothing; leave out --output-dir, --setup-file and --commit-file")
    rec = _reconciler(args, args.dry_run)
    outcome = rec.run()
    for line in outcome.log:
        print(line)
    for line in outcome.notes.lines():
        print(f"attention: {line}")
    dispatched = [a for a in outcome.actions if a.kind == "dispatch" and (a.done or args.dry_run)]
    planned = outcome.commits
    requests = outcome.requests
    print(
        f"{len(dispatched)} builds {'to dispatch' if args.dry_run else 'dispatched'}; "
        f"{len(planned)} commits for rc apply-commits; "
        f"{len(requests)} repository changes for rc setup-repos; "
        f"{rec.gh.requests + rec.ci_gh.requests} GitHub API requests"
    )
    if args.commit_file:
        if planned:
            commits.dump(planned, Path(args.commit_file), rec.listed)
        gha.set_output("commits", "true" if planned else "false")
        gha.set_output("commit-repos", commits.repositories(planned))
    if args.setup_file:
        if requests:
            reposetup.dump(requests, Path(args.setup_file))
        gha.set_output("setup", "true" if requests else "false")
    if args.output_dir:
        out = Path(args.output_dir)
        out.mkdir(parents=True, exist_ok=True)
        if outcome.catalog is not None:
            (out / "catalog.json").write_text(status.dumps(outcome.catalog), encoding="utf-8")
        repository = os.environ.get("GITHUB_REPOSITORY") or f"{rec.org}/ci"
        (out / "issue.md").write_text(status.issue_body(outcome.notes, repository), encoding="utf-8")
        (out / "issue-state").write_text("open\n" if outcome.notes else "closed\n", encoding="utf-8")
        gha.set_output("written", "true")
        gha.summary(
            "### Reconcile\n\n"
            + (
                "".join(f"- {a.title}{LATER.get(a.kind, '')}\n" for a in outcome.actions)
                or "Nothing to do.\n"
            )
            + ("\n" + "".join(f"- {line}\n" for line in outcome.notes.lines()) if outcome.notes else "")
        )
    return 0


def cmd_apply_commits(args) -> int:
    planned = commits.load(Path(args.file), _listed(args))
    token = os.environ.get("RC_WRITE_TOKEN")
    if not token:
        raise RcError("RC_WRITE_TOKEN is not set")
    org = _org()
    for commit in planned:
        print(f"commit \"{commit['message']}\" to {org}/{commit['name']} ({', '.join(sorted(commit['files']))})")
    problems = commits.apply(planned, GitHub(UrllibTransport(), token), org)
    for problem in problems:
        gha.error(problem)
    print(f"{len(planned) - len(problems)} of {len(planned)} commits made")
    return 1 if problems else 0


def cmd_setup_repos(args) -> int:
    requests = reposetup.load(Path(args.file))
    token = os.environ.get("RC_ADMIN_TOKEN")
    if not token:
        raise RcError("RC_ADMIN_TOKEN is not set")
    org = _org()
    for request in requests:
        print(f"{request['kind']} {org}/{request['name']}")
    problems = reposetup.apply(requests, GitHub(UrllibTransport(), token), org)
    for problem in problems:
        gha.error(problem)
    print(f"{len(requests) - len(problems)} of {len(requests)} repository changes made")
    return 1 if problems else 0


def cmd_lock(args) -> int:
    path = Path(args.file)
    if args.requirement:
        recipe, version = locks.new_recipe(
            path.name, args.requirement, args.python_version, tuple(args.no_emit or ()), tuple(args.exempt or ())
        )
    else:
        if not path.is_file():
            raise RcError(f"{path} does not exist; pass --requirement to create it")
        package_file = Path(args.package_file) if args.package_file else path.parent / "package.yml"
        package = load_package(package_file, _distros(args))
        if package.upstream.source != "pypi":
            raise RcError(f"{package.name} is not installed from PyPI")
        recipe = locks.parse_recipe(path.read_text(encoding="utf-8"), path.name, package.upstream.project)
        recipe = locks.with_exempt(recipe, tuple(args.exempt or ()))
        version = package.version
    path.write_bytes(locks.compile_lock(recipe, version))
    print(f"{path}: {recipe.requirement(version)}")
    return 0


def cmd_catalog(args) -> int:
    if args.empty:
        doc = status.empty(_distros(args), _org())
    else:
        doc = _reconciler(args, dry_run=True).catalog_only()
    text = status.dumps(doc)
    if args.output:
        Path(args.output).parent.mkdir(parents=True, exist_ok=True)
        Path(args.output).write_text(text, encoding="utf-8")
        print(args.output)
    else:
        sys.stdout.write(text)
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="rc", description="Build tooling for randomcontainers images.")
    parser.add_argument("--version", action="version", version=f"rc {__version__}")
    parser.add_argument("--ci-dir", help="checkout of randomcontainers/ci holding distros.yml and packages.yml")
    sub = parser.add_subparsers(dest="command", required=True)

    def add(name, func, help_text):
        p = sub.add_parser(name, help=help_text, description=help_text)
        p.set_defaults(func=func)
        return p

    def packages_dir(p):
        p.add_argument(
            "--packages-dir",
            help="read member package.yml files from DIR/<name>/package.yml instead of GitHub",
        )

    p = add("validate", cmd_validate, "Check package.yml and combo.yml files.")
    p.add_argument("paths", nargs="*", help="files, or directories holding one (default: current directory)")
    p.add_argument("--catalog", action="store_true", help="also check every listed package together")
    p.add_argument("--files", action="store_true", help="also check the Dockerfiles and files package.yml refers to")
    packages_dir(p)

    p = add("plan", cmd_plan, "Resolve digests and write the build plan and job outputs.")
    p.add_argument("--source", default=".", help="checkout of the package or combo repository")
    p.add_argument("--default-only", type=_bool, default=False, help="rebuild only the default image")
    p.add_argument("--want", default="", help="request id from the reconciler")
    p.add_argument("--output-file", help="write the plan here")
    p.add_argument("--offline", action="store_true", help="do not contact registries (local work)")
    p.add_argument(
        "--member-image",
        action="append",
        help="NAME=IMAGE: local image for a member with --offline; {distro} in IMAGE is replaced",
    )
    packages_dir(p)

    p = add("render", cmd_render, "Write the combo Dockerfile or all files of a combo repository.")
    p.add_argument("--source", default=".", help="package repository (default: current directory)")
    p.add_argument("--package-file", help="package.yml of the combo owner")
    p.add_argument("--combo-file", help="combo.yml of a combo repository")
    p.add_argument("--combo", help="combo name (default: the package's default combo)")
    p.add_argument("--distro", help="render only this distro")
    p.add_argument("--output", help="write the Dockerfile here instead of stdout")
    p.add_argument("--output-dir", help="write Dockerfile.<distro> files here")
    p.add_argument("--repo-files", metavar="DIR", help="write every file of the combo repository into DIR")
    packages_dir(p)

    p = add("bake", cmd_bake, "Write docker-bake.json for one distro and platform.")
    p.add_argument("--plan", required=True)
    p.add_argument("--distro", required=True)
    p.add_argument("--platform", required=True)
    p.add_argument("--mode", choices=bake.MODES, required=True)
    p.add_argument("--output", required=True)
    p.add_argument("--source-dir", default="src", help="build context of the slim target")
    p.add_argument("--work-dir", default=".rc-build", help="where generated Dockerfiles go")
    p.add_argument("--tag-prefix", default="local/", help="prefix of the local tags in load mode")
    p.add_argument("--jobs", type=int, help="pass JOBS to compiles")

    p = add("digests", cmd_digests, "Record pushed digests for the merge job.")
    p.add_argument("--plan", required=True)
    p.add_argument("--distro", required=True)
    p.add_argument("--platform", required=True)
    p.add_argument("--push-metadata", required=True)
    p.add_argument("--load-metadata")
    p.add_argument("--output-dir", required=True)

    for name, func, text in (
        ("check-image", cmd_check_image, "Check a built image against the image contract."),
        ("test-image", cmd_test_image, "Run the package tests inside a built image."),
    ):
        p = add(name, func, text)
        p.add_argument("--plan", required=True)
        p.add_argument("--target", choices=("slim", "default"), required=True)
        p.add_argument("--distro", required=True)
        p.add_argument("--image", help="image to check (default: the local tag from rc bake)")
        p.add_argument("--tag-prefix", default="local/")
        if name == "test-image":
            p.add_argument("--work-dir", help="parent directory for the per-package /work directories")
            p.add_argument("--keep", action="store_true", help="keep the /work directories")

    p = add("tags", cmd_tags, "Print the tags each image gets.")
    p.add_argument("--plan")
    p.add_argument("--source", default=".")
    p.add_argument("--package-file")
    p.add_argument("--distro")
    p.add_argument("--flavour", choices=("slim", "default"))
    p.add_argument("--json", action="store_true")

    p = add("merge", cmd_merge, "Decide the tags and write imagetools create arguments.")
    p.add_argument("--plan", required=True)
    p.add_argument("--flavour", choices=("slim", "default"), required=True)
    p.add_argument("--distro", required=True)
    p.add_argument("--digests", required=True, help="directory with <flavour>-<arch> digest files")
    p.add_argument("--output", required=True, help="file for the NUL-separated arguments")

    p = add("source-release", cmd_source_release, "Download and verify the upstream sources for the release.")
    p.add_argument("--plan", required=True)
    p.add_argument("--output-dir", required=True)

    p = add(
        "reconcile",
        cmd_reconcile,
        "Update packages, sync combo repositories and dispatch the builds that are due.",
    )
    p.add_argument("--dry-run", action="store_true", help="print what would be done and change nothing")
    p.add_argument("--output-dir", help="write catalog.json, issue.md and issue-state here")
    p.add_argument("--setup-file", help="write the repositories to create and the topics to set here, for rc setup-repos")
    p.add_argument("--commit-file", help="write the planned commits here, for rc apply-commits")
    packages_dir(p)

    p = add(
        "apply-commits",
        cmd_apply_commits,
        "Make the commits rc reconcile planned; needs RC_WRITE_TOKEN.",
    )
    p.add_argument("file", help="the file written by rc reconcile --commit-file")

    p = add(
        "setup-repos",
        cmd_setup_repos,
        "Create combo repositories and set topics as rc reconcile requested; needs RC_ADMIN_TOKEN.",
    )
    p.add_argument("file", help="the file written by rc reconcile --setup-file")

    p = add("lock", cmd_lock, "Write a hash-locked requirements file with uv, the way the reconciler does.")
    p.add_argument("file", help="the lock file, for example requirements.lock")
    p.add_argument("--package-file", help="package.yml whose version is locked (default: next to the lock file)")
    p.add_argument("--requirement", help="create the file for this requirement, for example 'streamlink==8.6.1'")
    p.add_argument("--python-version", default="3.14", help="oldest python3 of the distros (with --requirement)")
    p.add_argument("--no-emit", action="append", help="leave this package out of the file (with --requirement)")
    p.add_argument(
        "--exempt",
        action="append",
        metavar="PACKAGE",
        help="let this dependency skip the 7-day --exclude-newer window, for one the package pins exactly "
        "and releases together with it; kept in the file's header",
    )

    p = add("catalog", cmd_catalog, "Write the catalog of published images.")
    p.add_argument("--output", help="write here instead of stdout")
    p.add_argument("--empty", action="store_true", help="write a catalog with no images")
    packages_dir(p)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except ValidationError as exc:
        for problem in exc.problems:
            gha.fail(f"{exc.origin}: {problem}")
        return 1
    except RcError as exc:
        gha.fail(str(exc))
        return 1
