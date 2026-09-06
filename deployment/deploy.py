"""Deploy branch-scoped SQL and Snowpark registrations to Snowflake.

Snowpark Python artifacts are uploaded to an internal stage. Their companion
SQL artifact creates or replaces the procedure with an IMPORTS clause.
"""

from __future__ import annotations

import argparse
import hashlib
from io import StringIO
import logging
import os
import subprocess
import tomllib
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

from snowflake.connector.util_text import split_statements
from snowflake.snowpark import Session

LEDGER_TABLE = "DEPLOYMENT_LEDGER"
SNOWPARK_STAGE = "@DEPLOYMENT_STAGE/SNOWPARK"
SUPPORTED_SUFFIXES = {".sql", ".py"}
EXCLUDED_PARTS = {"__pycache__", ".venv", ".git"}
ARTIFACT_ROOTS = {"SQL", "FILES", "SNOWPARK", "snowflake"}
LOGGER = logging.getLogger("snowflake-deploy")


@dataclass(frozen=True)
class Artifact:
    path: Path
    key: str
    sha256: str


def run_git(*args: str) -> str:
    result = subprocess.run(
        ["git", *args], check=True, capture_output=True, text=True
    )
    return result.stdout.strip()


def environment_tag(environment: str, configured_tag: str | None) -> str:
    return configured_tag or f"env-{environment.lower()}"


def update_environment_tag(
    environment: str, configured_tag: str | None, push: bool
) -> None:
    tag = environment_tag(environment, configured_tag)
    commit = run_git("rev-parse", "HEAD")
    run_git("tag", "--force", tag, commit, "-m", f"{environment} deployed {commit[:12]}")
    LOGGER.info("Updated environment tag %s -> %s", tag, commit)
    if push:
        run_git("push", "origin", f"refs/tags/{tag}:refs/tags/{tag}", "--force")
        LOGGER.info("Pushed environment tag %s to origin", tag)


def ref_exists(ref: str) -> bool:
    result = subprocess.run(
        ["git", "rev-parse", "--verify", "--quiet", ref],
        capture_output=True,
        text=True,
    )
    return result.returncode == 0


def resolve_target_ref(environment: str, requested_ref: str | None) -> str:
    if requested_ref:
        return requested_ref

    environment_name = environment.lower()
    candidates = (
        f"env-{environment_name}",
        f"origin/env-{environment_name}",
        f"origin/{environment_name}",
        f"refs/tags/env-{environment_name}",
        "origin/main",
    )
    for candidate in candidates:
        if ref_exists(candidate):
            LOGGER.info("Using target ref %s for environment %s", candidate, environment)
            return candidate
    raise RuntimeError(
        f"Could not resolve a target ref for {environment}. Tried: {', '.join(candidates)}. "
        "Pass --target-ref explicitly or fetch the target ref first."
    )


def assert_branch_is_current(target_ref: str, skip_ancestor_check: bool = False) -> None:
    """Reject branches that do not contain the target tip."""
    if skip_ancestor_check:
        LOGGER.warning(
            "Skipping ancestor check for target %s. Use only for emergency local testing.",
            target_ref,
        )
        return
    try:
        run_git("rev-parse", "--verify", target_ref)
        run_git("merge-base", "--is-ancestor", target_ref, "HEAD")
    except subprocess.CalledProcessError as exc:
        raise RuntimeError(
            f"HEAD does not contain target ref {target_ref}. Fetch it and update this branch "
            f"before deploying, for example: git fetch origin && git rebase {target_ref}. "
            "Use --skip-ancestor-check only for an intentional emergency local test."
        ) from exc


def branch_files(target_ref: str, include_uncommitted: bool = False) -> list[Path]:
    if include_uncommitted:
        names = run_git("diff", target_ref, "--name-only", "--diff-filter=ACMR")
        untracked = run_git("ls-files", "--others", "--exclude-standard")
        names = "\n".join(filter(None, [names, untracked]))
    else:
        names = run_git("diff", f"{target_ref}...HEAD", "--name-only", "--diff-filter=ACMR")
    return [Path(name) for name in names.splitlines() if name]


def assert_clean_worktree() -> None:
    status = run_git("status", "--porcelain")
    if status:
        raise RuntimeError(
            "Live deployment requires a clean working tree. Commit or stash these "
            f"uncommitted changes first:\n{status}"
        )


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def collect_artifacts(paths: Iterable[Path], root: Path) -> list[Artifact]:
    artifacts = []
    for path in paths:
        if not path.parts or path.parts[0] not in ARTIFACT_ROOTS:
            continue
        if path.suffix.lower() not in SUPPORTED_SUFFIXES:
            continue
        if any(part in EXCLUDED_PARTS for part in path.parts):
            continue
        resolved = (root / path).resolve()
        if root.resolve() not in resolved.parents:
            raise ValueError(f"Artifact is outside repository: {path}")
        if not resolved.is_file():
            continue
        artifacts.append(
            Artifact(
                path=resolved,
                key=path.as_posix(),
                sha256=sha256_file(resolved),
            )
        )
    return sorted(artifacts, key=lambda artifact: artifact.key)


def load_active_hashes(session: Session, environment: str) -> dict[str, str]:
    rows = session.sql(
        f"select object_name, sha256_hash from {LEDGER_TABLE} "
        "where environment = ? and is_active = true",
        params=[environment],
    ).collect()
    return {row[0]: row[1] for row in rows}


def execute_sql_file(session: Session, artifact: Artifact) -> None:
    sql = artifact.path.read_text(encoding="utf-8")
    for statement, _ in split_statements(StringIO(sql)):
        if statement.strip():
            session.sql(statement).collect()


def upload_python_file(session: Session, artifact: Artifact) -> None:
    session.file.put(
        str(artifact.path),
        SNOWPARK_STAGE,
        auto_compress=False,
        overwrite=True,
    )


def retire_and_record(
    session: Session, artifact: Artifact, environment: str, commit: str, deployment_id: str
) -> None:
    session.sql(
        f"update {LEDGER_TABLE} set is_active = false "
        "where environment = ? and object_name = ? and is_active = true",
        params=[environment, artifact.key],
    ).collect()
    session.sql(
        f"insert into {LEDGER_TABLE} "
        "(object_name, sha256_hash, git_commit, environment, is_active, deployment_id) "
        "values (?, ?, ?, ?, true, ?)",
        params=[artifact.key, artifact.sha256, commit, environment, deployment_id],
    ).collect()


def connection_parameters() -> dict[str, str]:
    return {
        "account": os.environ["SNOWFLAKE_ACCOUNT"],
        "user": os.environ["SNOWFLAKE_USER"],
        "password": os.environ["SNOWFLAKE_PASSWORD"],
        "warehouse": os.environ["SNOWFLAKE_WAREHOUSE"],
        "database": os.environ["SNOWFLAKE_DATABASE"],
        "schema": os.environ["SNOWFLAKE_SCHEMA"],
        "role": os.environ["SNOWFLAKE_ROLE"],
    }


def create_session(
    connection_name: str | None, warehouse: str | None, password_auth: bool
) -> Session:
    if connection_name:
        if not warehouse and not password_auth:
            return Session.builder.config("connection_name", connection_name).create()
        config_path = Path.home() / ".snowflake" / "connections.toml"
        with config_path.open("rb") as stream:
            config = tomllib.load(stream)[connection_name].copy()
        if warehouse:
            config["warehouse"] = warehouse
        if password_auth:
            config["authenticator"] = "snowflake"
        return Session.builder.configs(config).create()
    return Session.builder.configs(connection_parameters()).create()


def pending_artifacts(
    changed: list[Artifact], active_hashes: dict[str, str]
) -> list[Artifact]:
    pending = [item for item in changed if active_hashes.get(item.key) != item.sha256]
    pending_keys = {item.key for item in pending}
    for artifact in changed:
        if artifact.path.suffix.lower() != ".py":
            continue
        if active_hashes.get(artifact.key) == artifact.sha256:
            continue
        companion = artifact.path.with_suffix(".sql")
        if not companion.is_file():
            raise ValueError(f"Snowpark file requires companion procedure SQL: {artifact.key}")
        companion_key = Path(artifact.key).with_suffix(".sql").as_posix()
        if companion_key in pending_keys:
            continue
        companion_artifact = next(
            (item for item in changed if item.key == companion_key), None
        )
        if companion_artifact is None:
            companion_artifact = Artifact(
                path=companion,
                key=companion_key,
                sha256=sha256_file(companion),
            )
        pending.append(companion_artifact)
        pending_keys.add(companion_key)
    return sorted(pending, key=lambda artifact: artifact.key)


def print_plan(
    changed: list[Artifact], active_hashes: dict[str, str]
) -> list[Artifact]:
    pending = pending_artifacts(changed, active_hashes)
    print(f"Branch artifacts: {len(changed)}; pending deployments: {len(pending)}")
    if not pending:
        print("Nothing would be deployed.")
        return pending

    print("Deployment plan:")
    for artifact in pending:
        previous = active_hashes.get(artifact.key)
        action = "NEW" if previous is None else "CHANGED"
        if previous == artifact.sha256:
            action = "RECREATE"
        previous_display = previous or "<none>"
        print(
            f"  {action:7} {artifact.path.suffix.lower():4} {artifact.key} "
            f"{previous_display} -> {artifact.sha256}"
        )
    return pending


def print_tag_plan(environment: str, configured_tag: str | None) -> None:
    tag = environment_tag(environment, configured_tag)
    commit = run_git("rev-parse", "HEAD")
    print(f"Environment tag: {tag} -> {commit} (dry run; tag will not change)")


def deploy(args: argparse.Namespace) -> int:
    if args.push_environment_tag and not args.update_environment_tag:
        raise ValueError("--push-environment-tag requires --update-environment-tag")
    root = Path(args.repo_root).resolve()
    target_ref = resolve_target_ref(args.environment, args.target_ref)
    if not args.dry_run:
        assert_clean_worktree()
    assert_branch_is_current(target_ref, args.skip_ancestor_check)

    changed = collect_artifacts(
        branch_files(target_ref, include_uncommitted=args.dry_run), root
    )
    if not changed:
        print("No supported artifacts changed in the branch scope.")
        if args.dry_run and args.update_environment_tag:
            print_tag_plan(args.environment, args.environment_tag)
        elif not args.dry_run and args.update_environment_tag:
            update_environment_tag(
                args.environment, args.environment_tag, args.push_environment_tag
            )
        return 0

    session = create_session(args.connection_name, args.warehouse, args.password_auth)
    deployment_id = str(uuid.uuid4())

    commit = run_git("rev-parse", "HEAD")
    transaction_started = False
    try:
        active_hashes = load_active_hashes(session, args.environment)
        pending = print_plan(changed, active_hashes)

        if args.dry_run:
            if args.update_environment_tag:
                print_tag_plan(args.environment, args.environment_tag)
            print("Dry run complete. No Snowflake artifacts or ledger rows were changed.")
            return 0

        session.sql("begin").collect()
        transaction_started = True

        for artifact in pending:
            if artifact.path.suffix.lower() == ".sql":
                execute_sql_file(session, artifact)
            else:
                upload_python_file(session, artifact)
            retire_and_record(session, artifact, args.environment, commit, deployment_id)
        session.sql("commit").collect()
        transaction_started = False
        if args.update_environment_tag:
            update_environment_tag(
                args.environment, args.environment_tag, args.push_environment_tag
            )
    except Exception:
        if transaction_started:
            session.sql("rollback").collect()
        raise
    finally:
        session.close()
    return 0

def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=__doc__,
        epilog=(
            "--target-ref is the promotion baseline, not the branch to deploy. "
            "When omitted, the engine tries env-<environment>, origin/env-<environment>, "
            "origin/<environment>, env tags, then origin/main. Dry runs may include "
            "uncommitted files; live deployments require a committed, clean worktree."
        ),
    )
    parser.add_argument("--environment", required=True, choices=["DEV", "SIT", "UAT", "PROD"])
    parser.add_argument(
        "--target-ref",
        help="Promotion baseline ref; defaults to an environment ref, then origin/main",
    )
    parser.add_argument("--repo-root", default=".")
    parser.add_argument(
        "--connection-name",
        default=os.environ.get("SNOWFLAKE_CONNECTION_NAME"),
        help="Name from ~/.snowflake/connections.toml; otherwise use SNOWFLAKE_* variables",
    )
    parser.add_argument(
        "--warehouse",
        default=os.environ.get("SNOWFLAKE_WAREHOUSE"),
        help="Override the warehouse in the named connections.toml profile",
    )
    parser.add_argument(
        "--password-auth",
        action="store_true",
        help="Use the password in connections.toml instead of browser/OAuth authentication",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Preview ledger differences and include staged, unstaged, and untracked files",
    )
    parser.add_argument(
        "--skip-ancestor-check",
        action="store_true",
        help="Skip the target ancestry guardrail with a warning; emergency/local testing only",
    )
    parser.add_argument(
        "--environment-tag",
        help="Mutable Git tag used as the environment promotion baseline; defaults to env-<environment>",
    )
    parser.add_argument(
        "--update-environment-tag",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Update the environment tag after a successful live deployment (default: enabled)",
    )
    parser.add_argument(
        "--push-environment-tag",
        action="store_true",
        help="Force-push the updated environment tag to origin; requires --update-environment-tag",
    )
    return parser.parse_args()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
    raise SystemExit(deploy(parse_args()))
