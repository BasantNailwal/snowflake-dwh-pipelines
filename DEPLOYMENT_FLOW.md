# Deployment Flow Visual Guide

This framework uses Git to define the requested change set and the Snowflake ledger to define what is already applied in each environment.

## 1. Implementation flow

```mermaid
flowchart TB
  subgraph Repository
    SQL[SQL artifacts\nviews, procedures, tables]
    PY[SNOWPARK/*.py\nstandalone handlers]
    PROC[SNOWPARK/*.sql\nCREATE OR REPLACE PROCEDURE]
    ENGINE[deployment/deploy.py]
    LEDGER_DDL[deployment/ledger.sql]
  end

  subgraph GitControl
    TARGET[Target ref\nenv-uat, env-prod, origin/main]
    HEAD[HEAD commit]
    RELEASE[Immutable release tag\nrelease-2026.09]
    PROMOTION[Environment tag\nenv-uat, env-prod]
  end

  subgraph Snowflake
    STAGE["@DEPLOYMENT_STAGE/SNOWPARK"]
    OBJECTS[Views, tables, procedures]
    LEDGER[DEPLOYMENT_LEDGER]
  end

  TARGET --> ENGINE
  HEAD --> ENGINE
  SQL --> ENGINE
  PY --> ENGINE
  PROC --> ENGINE
  ENGINE -->|hash and execute| OBJECTS
  ENGINE -->|upload Python| STAGE
  ENGINE -->|record state| LEDGER
  LEDGER_DDL --> LEDGER
  RELEASE --> ENGINE
  ENGINE -->|move after success| PROMOTION
```

Responsibilities:

| Component | Responsibility |
|---|---|
| `deployment/deploy.py` | Orchestrates Git scope, hashing, Snowflake execution, ledger writes, and promotion tags |
| `deployment/ledger.sql` | Creates and upgrades the Snowflake state ledger |
| `SQL/` and `snowflake/` | Stores executable SQL artifacts |
| `SNOWPARK/*.py` | Stores standalone Python handlers uploaded to the internal stage |
| `SNOWPARK/*.sql` | Creates procedures that import staged Python files |
| `target_ref` | Defines the Git baseline for the promotion diff |
| `promotion_tag` | Points to the last successful environment deployment |

## 2. Execution flow

```mermaid
flowchart TD
    A[Developer changes SQL or Snowpark files] --> B[Commit changes on feature branch]
    B --> C[Resolve target ref for environment]
    C --> D{Is target ref an ancestor of HEAD?}
    D -- No --> E[Stop: fetch and rebase or merge target ref]
    D -- Yes --> F[git diff target-ref...HEAD]
    F --> G[Keep files under SQL, SNOWPARK, FILES, or snowflake]
    G --> H[Compute SHA-256 hashes]
    H --> I[Read active hashes from DEPLOYMENT_LEDGER]
    I --> J{Hash differs?}
    J -- No --> K[Skip artifact]
    J -- Yes --> L[Add to deployment plan]
    L --> M{Dry run?}
    M -- Yes --> N[Print plan and exit]
    M -- No --> O[Execute artifact]
    O --> P[Update ledger in transaction]
    P --> Q[Commit Snowflake work]
    Q --> R[Update environment promotion tag]
```

Runtime sequence:

```mermaid
sequenceDiagram
    participant CLI as CLI
    participant Engine as deploy()
    participant Git as Git
    participant SF as Snowflake session
    participant Ledger as DEPLOYMENT_LEDGER
    participant Stage as Internal stage

    CLI->>Engine: parse_args()
    Engine->>Git: resolve_target_ref(environment, target_ref)
    Engine->>Git: assert_clean_worktree() for live mode
    Engine->>Git: assert_branch_is_current(target_ref)
    Engine->>Git: branch_files(target_ref, dry_run)
    Engine->>Engine: collect_artifacts() and sha256_file()
    Engine->>SF: create_session()
    Engine->>Ledger: load_active_hashes(environment)
    Ledger-->>Engine: active object hashes
    Engine->>Engine: pending_artifacts() and print_plan()
    alt dry run
      Engine-->>CLI: print plan and tag preview
    else live deployment
      Engine->>SF: BEGIN
      loop each pending artifact
        alt SQL artifact
          Engine->>SF: execute_sql_file()
        else Snowpark Python artifact
          Engine->>Stage: upload_python_file()
          Engine->>SF: execute companion procedure SQL
        end
        Engine->>Ledger: retire old row and insert SUCCESS row
      end
      Engine->>SF: COMMIT
      Engine->>Git: update_environment_tag()
      Engine-->>CLI: deployment complete
    end
  ```

## 3. Feature branch guardrail

```mermaid
gitGraph
   commit id: "A" tag: "main"
   branch feature
   checkout feature
   commit id: "B" tag: "feature change"
   checkout main
   commit id: "C" tag: "new main"
   checkout feature
   commit id: "D" tag: "feature HEAD"
```

The feature branch must first contain the selected target ref:

```powershell
git fetch origin
git rebase <target-ref>
```

The deployment engine then compares the feature branch to the target branch:

```text
git diff <target-ref>...HEAD --name-only
```

This means: deploy changes introduced after the target baseline, not every file changed anywhere in repository history. For a first promotion, the target can be the repository root commit. For later environment promotions, it is normally that environment's mutable tag, such as `env-uat` or `env-prod`.

## 4. Dry-run flow

```mermaid
sequenceDiagram
    participant Git
    participant Engine as deploy.py
    participant SF as Snowflake
    participant Ledger as DEPLOYMENT_LEDGER

    Engine->>Git: Resolve target ref
    Engine->>Git: Verify target ref is ancestor
    Engine->>Git: Read committed or dry-run working-tree files
    Engine->>Engine: Filter artifact roots and calculate SHA-256
    Engine->>SF: Open read-only planning session
    Engine->>Ledger: Read active hashes for environment
    Ledger-->>Engine: Current object hashes
    Engine->>Engine: Compare Git hashes with ledger hashes
    Engine-->>Git: Print NEW and CHANGED plan
    Engine-->>Git: Preview promotion tag update
    Note over Engine,SF: No SQL execution, stage upload, transaction, or ledger write
```

Run it with:

```powershell
python deployment/deploy.py `
  --environment DEV `
  --target-ref origin/main `
  --connection-name dev `
  --warehouse DEV_WH `
  --password-auth `
  --dry-run
```

Example output:

```text
Branch artifacts: 2; pending deployments: 2
Deployment plan:
  CHANGED .py  SNOWPARK/hello_procedure.py old-hash -> new-hash
  CHANGED .sql SNOWPARK/hello_procedure.sql old-hash -> new-hash
Dry run complete. No Snowflake artifacts or ledger rows were changed.
```

## 5. SQL deployment flow

```mermaid
flowchart LR
    A[Changed .sql file] --> B[Read file]
    B --> C[Split into SQL statements]
    C --> D[Execute statements through Snowpark session]
    D --> E[Retire previous active ledger row]
    E --> F[Insert new active ledger row]
```

SQL files are executed in sorted path order. This allows files such as `SNOWPARK/00_create_stage.sql` to run before the Python upload and procedure definition.

## 6. Snowpark deployment flow

The Python file is a standalone handler. It does not call `sproc.register`.

```mermaid
sequenceDiagram
    participant Engine as deploy.py
    participant Stage as @DEPLOYMENT_STAGE/SNOWPARK
    participant SQL as hello_procedure.sql
    participant SF as Snowflake procedure catalog

    Engine->>Stage: PUT hello_procedure.py overwrite=true
    Engine->>SQL: Execute CREATE OR REPLACE PROCEDURE
    SQL->>Stage: IMPORTS staged Python file
    SQL->>SF: Create procedure with HANDLER hello_procedure.hello
    SF-->>Engine: Procedure created
    Engine->>SF: Record active hashes in ledger
```

Repository files:

```text
SNOWPARK/
  00_create_stage.sql
  hello_procedure.py
  hello_procedure.sql
```

Procedure definition:

```sql
create or replace procedure HELLO_FROM_SNOWPARK(name string)
returns string
language python
runtime_version = '3.11'
packages = ('snowflake-snowpark-python')
imports = ('@DEPLOYMENT_STAGE/SNOWPARK/hello_procedure.py')
handler = 'hello_procedure.hello';
```

Call it with:

```sql
call HELLO_FROM_SNOWPARK('Snowflake');
```

If the Python hash changes, the engine automatically includes the companion `.sql` file in the plan so the procedure is recreated against the new staged source.

## 7. Ledger state transition

```mermaid
stateDiagram-v2
    [*] --> NoActiveVersion
    NoActiveVersion --> ActiveV1: First deployment
    ActiveV1 --> ActiveV2: Hash changed
    ActiveV2 --> ActiveV3: Rollback commit
    ActiveV3 --> ActiveV3: Same hash skipped
```

The ledger is append-only. A new deployment:

1. Marks the previous `(environment, object_name)` row inactive.
2. Inserts a new active row with the new hash and Git commit.
3. Records the deployment ID and timestamp.
4. Records source path, artifact type, target ref, release tag, promotion tag, previous hash, timing, runner, and request metadata.

## 8. Rollback flow

```mermaid
flowchart TD
    A[Production commit] --> B[Identify bad commit]
    B --> C[Create rollback branch from latest main]
    C --> D[git revert bad-commit]
    D --> E[Review inverse artifact change]
    E --> F[Run dry run]
    F --> G[Deploy rollback commit]
    G --> H[New hash and ledger history entry]
```

Example:

```powershell
git switch main
git pull origin main
git switch -c rollback/undo-bad-deployment
git revert <bad-commit-sha>
git push -u origin rollback/undo-bad-deployment
```

A rollback is a new desired state. Do not check out an old commit and deploy it directly because it may not contain the latest target branch and may produce an incomplete diff.

For tables, use explicit forward or reverse migrations. Deletions are not inferred from Git deletes by this engine.

## 9. Environment tags and release tags

```mermaid
flowchart LR
  A[HEAD commit] --> B[Exact immutable Git release tag]
  A --> C[Environment promotion tag after success]
  C --> D[env-dev]
  C --> E[env-uat]
  C --> F[env-prod]
  G[target-ref] --> H[Diff baseline]
```

`target_ref` is the baseline used to calculate the diff. `release_tag` is an exact release tag on `HEAD`, such as `release-2026.09`, and may be null. `promotion_tag` is the environment pointer moved after successful deployment, such as `env-prod`. They are intentionally different fields.

## 10. Current capabilities

| Capability | Supported behavior |
|---|---|
| Git scope detection | Three-dot diff between resolved target ref and `HEAD` |
| Branch safety | Stops when target ref is not an ancestor of `HEAD` |
| Hash detection | SHA-256 of changed artifact bytes |
| Environment state | Active hash tracked independently per environment |
| Dry run | Reads ledger and prints the exact pending plan without mutation |
| SQL artifacts | Executes `.sql` files in sorted path order |
| Snowpark source | Uploads standalone `.py` files to an internal stage |
| Procedure creation | Executes companion `CREATE OR REPLACE PROCEDURE` SQL |
| Idempotency | Matching active hashes are skipped |
| Rollback | Supported through a new `git revert` commit |
| Audit history | Git commit, hash, environment, timestamp, and deployment ID |
| Local authentication | Named `connections.toml` profile, password or OAuth mode |
| CI authentication | GitHub environment secrets |
| Environment promotion tags | Creates or updates `env-<environment>` after success |
| Release metadata | Records exact Git release tag separately from promotion tag |

## 11. Current boundaries

- The engine executes `.sql` and `.py`; `FILES/*.csv` is not automatically loaded.
- Deleted files are ignored; destructive changes require explicit SQL.
- Snowflake DDL may implicitly commit, so a failed DDL operation may require reconciliation.
- A `connections.toml` profile must provide a real warehouse, or use `--warehouse`.
- The ledger table must be created before dry runs or deployments.

## 12. FAQ

### Why did the script deploy nothing on `main`?

If `HEAD` equals `origin/main`, the scope `origin/main...HEAD` is empty. For a first environment promotion, use the repository root commit or create an environment baseline tag.

### Does a dry run require Snowflake?

Yes for a ledger-aware dry run. It reads active hashes from the target environment but does not execute SQL, upload Python, update the ledger, or move tags.

### What happens if the working tree has uncommitted files?

Dry runs include staged, unstaged, and untracked artifact files. Live deployments reject a dirty working tree because the ledger records the committed `HEAD` hash.

### What is the difference between `target_ref`, `release_tag`, and `promotion_tag`?

`target_ref` is the comparison baseline, `release_tag` identifies an exact immutable release tag on `HEAD`, and `promotion_tag` identifies the environment pointer updated after success.

### When does the environment tag move?

Only after Snowflake execution and ledger commit succeed. A failed deployment leaves the previous environment tag unchanged.

### Does the engine call `sproc.register`?

No. It uploads standalone Snowpark Python files with `session.file.put`, then executes companion SQL containing `CREATE OR REPLACE PROCEDURE ... IMPORTS`.

### How is rollback performed?

Create a new branch from the latest target branch, run `git revert <bad-commit>`, preview it, and deploy the resulting rollback commit. Do not deploy an old commit directly.

### What does `--skip-ancestor-check` do?

It bypasses the branch freshness guardrail with a warning. Use it only for deliberate local testing or an emergency procedure with explicit review.
