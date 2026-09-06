# Snowflake deployment blueprint

This framework treats Git as the requested change set and Snowflake as the applied-state authority. A file is deployed only when it is in the resolved target-to-`HEAD` scope, is under `SQL/`, `FILES/`, `SNOWPARK/`, or `snowflake/`, and its SHA-256 differs from the active ledger row for the target environment.

## Repository layout

```text
snowflake-dwh-pipelines/
  .github/workflows/deploy.yml
  deployment/
    deploy.py                 # branch scope, hashing, execution, ledger transaction
    ledger.sql                # run once by an administrator
  SNOWPARK/
    00_create_stage.sql       # internal stage used for permanent registrations
    hello_procedure.py        # standalone handler uploaded to the internal stage
    hello_procedure.sql       # CREATE OR REPLACE PROCEDURE with IMPORTS
  snowflake/
    stateless/
      views/                  # CREATE OR REPLACE VIEW
      procedures/             # CREATE OR REPLACE PROCEDURE
      functions/              # CREATE OR REPLACE FUNCTION
    stateful/
      tables/                 # forward-only CREATE/ALTER migrations
    snowpark/
      procedures/              # .py modules exposing register(session)
      functions/               # .py modules exposing register(session)
  requirements.txt
  DEPLOYMENT_BLUEPRINT.md
```

Stateless artifacts describe the current desired definition and may be replaced. Table changes are forward-only migrations: use a new, ordered file for each change and do not edit an applied migration. Each Snowpark Python file is a standalone handler. The engine uploads it with `session.file.put` to `@DEPLOYMENT_STAGE/SNOWPARK`, then executes the companion SQL file, whose `IMPORTS` clause points to the staged file and whose `HANDLER` names the Python function.

## Ledger contract

Run [deployment/ledger.sql](deployment/ledger.sql) once in the deployment schema. The table is append-only. For each `(environment, object_name)`, the engine retires the previous active row and inserts a new active row after the artifact has executed. `object_name` is the repository-relative path, which avoids ambiguous Snowflake names when one file registers more than one object. `deployment_id` groups all rows from one transaction.

The engine performs the ledger read, artifact execution, and ledger version updates in one Snowpark session and explicitly commits or rolls back. Each ledger row records the source path and artifact type, Git branch (`git_branch`), commit, target ref, release tag, previous hash, status, timing, request ID, and runner. Detached-HEAD deployments use `DETACHED:<commit>` as the branch value. The ledger writes are transactionally grouped, but Snowflake DDL can implicitly commit, so a failed DDL deployment cannot be treated as a guaranteed database rollback. The sequence is:

1. Resolve the explicit `--target-ref`, or choose an environment convention and fall back to `origin/main`.
2. Verify the target ref is an ancestor of `HEAD`, unless the emergency override is used.
3. Read committed changes from the three-dot diff; dry runs additionally include staged, unstaged, and untracked files.
4. Hash file bytes with SHA-256.
5. Read active hashes for the target environment.
6. Execute only hash misses: `.sql` through Snowpark SQL, `.py` through stage upload and companion SQL.
7. Set the old ledger row inactive and insert the new active row.
8. Commit the ledger state, or roll back uncommitted ledger work. If DDL has already committed, reconcile the object and ledger before retrying.

Deleted files are intentionally ignored. A deletion is a deployment design decision: use an explicit SQL migration for a table, or a deliberate replacement definition for a stateless object. Never infer a destructive database operation from a branch diff. `FILES/` is an allowed input root, but the current engine executes only `.sql` and `.py`; files such as `FILES/data.csv` require a separate staged-load step and are not silently treated as SQL. Artifacts are sorted by path, so `SNOWPARK/00_create_stage.sql` runs before the Python upload and its companion procedure SQL. If a Python hash changes, the companion SQL is redeployed even when its own hash is unchanged, ensuring the procedure package picks up the new staged source.

## Local execution

```powershell
python -m pip install -r requirements.txt
python deployment/deploy.py --environment DEV --connection-name dev --warehouse DEV_WH --password-auth
# Preview only; reads the ledger but does not execute or mutate anything.
python deployment/deploy.py --environment DEV --connection-name dev --warehouse DEV_WH --password-auth --dry-run
# First promotion: use the repository root as the initial environment baseline.
python deployment/deploy.py --environment UAT --target-ref <initial-commit> --connection-name uat --warehouse UAT_WH --password-auth --dry-run
```

Dry-run output lists every branch-scoped artifact whose active environment hash
differs, labels it `NEW` or `CHANGED`, and prints the previous and target hashes.
It still requires Snowflake credentials because the active ledger is the source
of truth. It does not start a transaction, execute SQL or Python registration
code, or write ledger rows.

For local execution, `--connection-name dev` reads the named connection from the Snowflake connector's `connections.toml` file, normally `~/.snowflake/connections.toml` on Windows. `--password-auth` explicitly switches that profile to username/password authentication, and `--warehouse DEV_WH` overrides a missing or placeholder warehouse. Replace `DEV_WH` with a real warehouse name. Omit `--target-ref` to use `env-dev`, `origin/env-dev`, `origin/dev`, an `env-dev` tag, or `origin/main`, in that order. You can also set `SNOWFLAKE_CONNECTION_NAME=dev` and `SNOWFLAKE_WAREHOUSE`. If no connection name is supplied, the engine uses `SNOWFLAKE_ACCOUNT`, `SNOWFLAKE_USER`, `SNOWFLAKE_PASSWORD`, `SNOWFLAKE_WAREHOUSE`, `SNOWFLAKE_DATABASE`, `SNOWFLAKE_SCHEMA`, and `SNOWFLAKE_ROLE`. In CI, use an environment-scoped secret set and approvals for UAT and PROD. Prefer key-pair or workload identity authentication over a password in production.

Live deployments update the mutable environment tag `env-<environment>` after Snowflake work and ledger updates succeed. Dry runs only print the proposed tag target. Use `--environment-tag uat-current` to choose another tag name. Use `--push-environment-tag` when the deployment identity is allowed to publish the tag to `origin`; this performs a force push because the tag is a moving environment pointer. A failed deployment does not advance the tag.

## Rollback and guardrails

A rollback is a new desired state, never a branch rewind. Use `git revert <deployment-commit>` on a new branch, review the resulting inverse artifact change, and deploy that commit normally. Its content gets a new hash and ledger row, so the rollback is auditable and cannot be mistaken for an old deployment. For stateful tables, create an explicit reverse migration and review data-loss implications; do not rewrite the original migration.

The workflow in [.github/workflows/deploy.yml](.github/workflows/deploy.yml) uses full history, fetches `origin/main`, and fails with `git merge-base --is-ancestor` if the branch is behind. The engine repeats the check because CI checks are not a substitute for runtime protection. A promotion should deploy the exact reviewed commit (or a protected main commit), use environment approvals, and keep Snowflake credentials in GitHub environment secrets.

## Operational queries

```sql
-- Current applied version by environment and artifact.
select environment, object_name, artifact_type, sha256_hash, previous_hash,
       git_commit, git_branch, target_ref, release_tag, deployment_status,
       started_at, completed_at, request_id, runner_name, deployed_at
from deployment_ledger
where is_active
order by environment, object_name;

-- Full history for one artifact.
select *
from deployment_ledger
where object_name = 'snowflake/stateless/views/customer.sql'
order by deployed_at desc;
```

The ledger is not a substitute for Snowflake object metadata or query history. Retain query/access history for forensic detail, and grant the deployment role only the DDL and ledger privileges required for its environment.
