"""Deploy branch-scoped SQL and Snowpark registrations to Snowflake.

Snowpark Python artifacts are uploaded to an internal stage. Their companion
SQL artifact creates or replaces the procedure with an IMPORTS clause.
"""

from __future__ import annotations

import argparse
import hashlib
from io import StringIO
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


def assert_branch_is_current(target_ref: str) -> None:
    """Reject branches that do not contain the target tip."""
    try:
        run_git("rev-parse", "--verify", target_ref)
        run_git("merge-base", "--is-ancestor", target_ref, "HEAD")
    except subprocess.CalledProcessError as exc:
        raise RuntimeError(
            f"HEAD must contain {target_ref}; rebase or merge it before deployment"
        ) from exc


def branch_files(target_ref: str) -> list[Path]:
    names = run_git("diff", f"{target_ref}...HEAD", "--name-only", "--diff-filter=ACMR")
    return [Path(name) for name in names.splitlines() if name]


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
        if path.suffix.lower() not in SUPPORTED_SUFFIXES or not path.is_file():
            continue
        if any(part in EXCLUDED_PARTS for part in path.parts):
            continue
        resolved = (root / path).resolve()
        if root.resolve() not in resolved.parents:
            raise ValueError(f"Artifact is outside repository: {path}")
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


def deploy(args: argparse.Namespace) -> int:
    root = Path(args.repo_root).resolve()
    assert_branch_is_current(args.target_ref)
    changed = collect_artifacts(branch_files(args.target_ref), root)
    if not changed:
        print("No supported artifacts changed in the branch scope.")
        return 0

    session = create_session(args.connection_name, args.warehouse, args.password_auth)
    deployment_id = str(uuid.uuid4())
    commit = run_git("rev-parse", "HEAD")
    transaction_started = False
    try:
        active_hashes = load_active_hashes(session, args.environment)
        pending = print_plan(changed, active_hashes)

        if args.dry_run:
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
    except Exception:
        if transaction_started:
            session.sql("rollback").collect()
        raise
    finally:
        session.close()
    return 0


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--environment", required=True, choices=["DEV", "SIT", "UAT", "PROD"])
    parser.add_argument("--target-ref", default="origin/main")
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
        help="Show artifacts whose active ledger hash differs without changing Snowflake",
    )
    return parser.parse_args()


if __name__ == "__main__":
    raise SystemExit(deploy(parse_args()))
