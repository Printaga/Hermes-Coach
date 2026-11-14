#!/usr/bin/env python3
"""Keep the GitHub Release in sync with the version in ``plugin.yaml``.

One source of truth: the ``version`` in ``plugin.yaml`` and the matching
``## [<version>]`` section of ``changelog.md``. The tag is ``v<version>`` and
the release body is that changelog section.

The operation is idempotent and conservative:

- an existing tag is never moved (a release created later attaches to the
  commit the tag already names);
- a release whose body already matches is left alone;
- a release whose body drifted is rewritten from the changelog;
- a missing tag is created at ``HEAD``.

The ``release`` workflow runs ``--apply`` on a push to ``main`` that touches
``plugin.yaml`` or ``changelog.md``. Run ``--check`` locally to see what it
would do without writing anything.

Exit codes: 0 in sync (or synced), 1 on a missing changelog section or a
failed ``gh``/``git`` call.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import tempfile
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

__all__ = ["main", "release_notes", "sync", "version_of"]

Run = Callable[..., subprocess.CompletedProcess]

_VERSION_LINE = re.compile(r'^version:\s*"?([^"\s#]+)"?\s*(?:#.*)?$', re.MULTILINE)


def version_of(manifest: Path) -> str:
    """The quoted or bare ``version:`` value from a plugin manifest."""
    text = manifest.read_text(encoding="utf-8")
    match = _VERSION_LINE.search(text)
    if match is None or not match.group(1).strip():
        raise ValueError(f"{manifest}: no version: line")
    return match.group(1).strip()


def release_notes(changelog: str, version: str) -> str:
    """The body of ``## [<version>]`` up to the next version heading."""
    marker = f"## [{version}]"
    start = changelog.find(marker)
    if start < 0:
        raise ValueError(f"changelog.md has no '## [{version}]' section")
    heading_end = changelog.find("\n", start)
    body_start = heading_end + 1 if heading_end >= 0 else len(changelog)
    following = changelog.find("\n## [", body_start)
    body = changelog[body_start : following if following >= 0 else len(changelog)]
    notes = body.strip()
    if not notes:
        raise ValueError(f"changelog.md section for {version} is empty")
    return notes


def _run(run: Run, argv: Sequence[str]) -> subprocess.CompletedProcess:
    return run(list(argv), capture_output=True, text=True)


def _completed(run: Run, argv: Sequence[str]) -> subprocess.CompletedProcess:
    result = _run(run, argv)
    if result.returncode != 0:
        message = (result.stderr or result.stdout or "").strip()
        raise RuntimeError(f"{' '.join(argv[:3])} failed: {message[:300]}")
    return result


def _default_repo(run: Run) -> str:
    env = os.environ.get("GITHUB_REPOSITORY", "").strip()
    if env:
        return env
    result = _completed(run, ["gh", "repo", "view", "--json", "nameWithOwner", "-q", ".nameWithOwner"])
    return result.stdout.strip()


def _tag_sha(repo: str, tag: str, run: Run) -> str | None:
    result = _run(run, ["gh", "api", f"repos/{repo}/commits/{tag}", "--jq", ".sha"])
    return result.stdout.strip() if result.returncode == 0 else None


def _release(repo: str, tag: str, run: Run) -> tuple[str, str] | None:
    result = _run(run, ["gh", "release", "view", tag, "--repo", repo, "--json", "body,url"])
    if result.returncode != 0:
        return None
    data = json.loads(result.stdout or "{}")
    return str(data.get("body") or ""), str(data.get("url") or "")


def _head(run: Run) -> str:
    return _completed(run, ["git", "rev-parse", "HEAD"]).stdout.strip()


def _notes_file(notes: str) -> Path:
    handle = tempfile.NamedTemporaryFile(
        "w", encoding="utf-8", prefix="release-notes-", suffix=".md", delete=False
    )
    with handle:
        handle.write(notes + "\n")
    return Path(handle.name)


def sync(
    *,
    version: str,
    repo: str,
    changelog: Path,
    notes: str | None = None,
    run: Run | None = None,
    apply: bool = False,
) -> dict[str, Any]:
    """Bring the tag and release for ``version`` in line with the changelog.

    Reads only when ``apply`` is false. Returns what was found and, with
    ``apply``, what was done — the caller prints it.
    """
    runner = run or subprocess.run
    tag = f"v{version}"
    body = notes if notes is not None else release_notes(
        changelog.read_text(encoding="utf-8"), version
    )
    result: dict[str, Any] = {"repo": repo, "tag": tag, "version": version, "actions": []}
    actions: list[str] = result["actions"]

    existing_tag = _tag_sha(repo, tag, runner)
    existing_release = _release(repo, tag, runner)

    if existing_release is not None:
        current, url = existing_release
        result["release"] = url
        if current.strip() != body.strip():
            actions.append("update release notes")
            if apply:
                path = _notes_file(body)
                try:
                    _completed(
                        runner,
                        ["gh", "release", "edit", tag, "--repo", repo, "--notes-file", str(path)],
                    )
                finally:
                    path.unlink(missing_ok=True)
                finished = _release(repo, tag, runner)
                if finished is None or finished[0].strip() != body.strip():
                    raise RuntimeError(f"{tag}: release notes did not update")
        return result

    if existing_tag is None:
        sha = _head(runner)
        result["tag_sha"] = sha
        actions.append(f"create tag {tag} at {sha[:12]}")
        if apply:
            _completed(
                runner,
                [
                    "gh",
                    "api",
                    "-X",
                    "POST",
                    f"repos/{repo}/git/refs",
                    "-f",
                    f"ref=refs/tags/{tag}",
                    "-f",
                    f"sha={sha}",
                ],
            )
    else:
        result["tag_sha"] = existing_tag

    actions.append("create release")
    if apply:
        path = _notes_file(body)
        try:
            created = _completed(
                runner,
                [
                    "gh",
                    "release",
                    "create",
                    tag,
                    "--repo",
                    repo,
                    "--title",
                    version,
                    "--notes-file",
                    str(path),
                    "--latest",
                ],
            )
        finally:
            path.unlink(missing_ok=True)
        result["release"] = created.stdout.strip()
        finished = _release(repo, tag, runner)
        if finished is None or finished[0].strip() != body.strip():
            raise RuntimeError(f"{tag}: release body does not match the changelog")
    return result


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="report only (default)")
    parser.add_argument("--apply", action="store_true", help="create/update the tag and release")
    parser.add_argument("--repo", default="", help="owner/name; defaults to GITHUB_REPOSITORY")
    parser.add_argument("--version", default="", help="defaults to plugin.yaml")
    parser.add_argument("--manifest", default="plugin.yaml")
    parser.add_argument("--changelog", default="changelog.md")
    args = parser.parse_args(argv)

    try:
        version = args.version.strip() or version_of(Path(args.manifest))
        repo = args.repo.strip() or _default_repo(subprocess.run)
        summary = sync(
            version=version,
            repo=repo,
            changelog=Path(args.changelog),
            apply=args.apply and not args.check,
        )
    except (OSError, ValueError, RuntimeError) as exc:
        print(f"release-sync: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(summary, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
