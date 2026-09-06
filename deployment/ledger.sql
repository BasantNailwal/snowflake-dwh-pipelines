-- Run once in the deployment control schema.
-- select current_schema() as schema_name;
create table if not exists deployment_ledger (
    ledger_id number autoincrement start 1 increment 1,
    object_name varchar not null,
    source_path varchar not null,
    artifact_type varchar not null,
    sha256_hash varchar(64) not null,
    previous_hash varchar(64),
    git_commit varchar(40) not null,
    git_branch varchar not null,
    target_ref varchar not null,
    release_tag varchar,
    promotion_tag varchar,
    environment varchar not null,
    is_active boolean not null default true,
    deployment_status varchar not null default 'SUCCESS',
    failure_reason varchar,
    started_at timestamp_ltz,
    completed_at timestamp_ltz,
    request_id varchar,
    runner_name varchar,
    snowflake_query_id varchar,
    deployed_at timestamp_ltz not null default current_timestamp(),
    deployed_by varchar not null default current_user(),
    deployment_id varchar not null,
    primary key (ledger_id),
    constraint deployment_ledger_hash_ck check (regexp_like(sha256_hash, '^[0-9a-f]{64}$'))
);

-- Safe upgrade for ledgers created before branch tracking was added.
alter table deployment_ledger add column if not exists git_branch varchar;
alter table deployment_ledger add column if not exists source_path varchar;
alter table deployment_ledger add column if not exists artifact_type varchar;
alter table deployment_ledger add column if not exists previous_hash varchar(64);
alter table deployment_ledger add column if not exists target_ref varchar;
alter table deployment_ledger add column if not exists release_tag varchar;
alter table deployment_ledger add column if not exists promotion_tag varchar;
alter table deployment_ledger add column if not exists deployment_status varchar default 'SUCCESS';
alter table deployment_ledger add column if not exists failure_reason varchar;
alter table deployment_ledger add column if not exists started_at timestamp_ltz;
alter table deployment_ledger add column if not exists completed_at timestamp_ltz;
alter table deployment_ledger add column if not exists request_id varchar;
alter table deployment_ledger add column if not exists runner_name varchar;
alter table deployment_ledger add column if not exists snowflake_query_id varchar;



