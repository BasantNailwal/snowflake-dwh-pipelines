-- Run once in the deployment control schema.
-- select current_schema() as schema_name;
create or replace table deployment_ledger (
    ledger_id number autoincrement start 1 increment 1,
    object_name varchar not null,
    sha256_hash varchar(64) not null,
    git_commit varchar(40) not null,
    environment varchar not null,
    is_active boolean not null default true,
    deployed_at timestamp_ltz not null default current_timestamp(),
    deployed_by varchar not null default current_user(),
    deployment_id varchar not null,
    primary key (ledger_id),
    constraint deployment_ledger_hash_ck check (regexp_like(sha256_hash, '^[0-9a-f]{64}$'))
);

-- The engine writes a new version and retires the previous active version
-- in the same transaction as the artifact deployment.
comment on table deployment_ledger is
    'Append-only deployment history. The active row per environment/object is the applied version.';

