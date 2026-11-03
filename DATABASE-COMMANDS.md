# Database Reference — Commands for Inspecting ForgeOps Data

A copy-paste reference for showing the data the platform actually stores. Every command below
was run against the live stack before being written down; the output shown is real.

> **Container names are fixed.** Compose derives them from the project name, so they are
> `forgeops-postgres-1` and `forgeops-redis-1` regardless of which host ports are in use.

---

## Quick start — the two commands most likely to be asked for

```bash
# Every table in the database, with its row count
docker exec forgeops-postgres-1 psql -U forgeops -d forgeops -c "\dt+"

# Every key in Redis
docker exec forgeops-redis-1 redis-cli KEYS '*'
```

---

## 1. Entering the databases interactively

### PostgreSQL

```bash
docker exec -it forgeops-postgres-1 psql -U forgeops -d forgeops
```

You land at a `forgeops=#` prompt. Useful meta-commands once inside:

| Command           | What it does                                         |
| :---------------- | :--------------------------------------------------- |
| `\dt`             | list all tables                                      |
| `\dt+`            | list tables with size and description                |
| `\d users`        | describe the `users` table — columns, types, indexes |
| `\d+ change_sets` | the same, with storage details                       |
| `\l`              | list all databases                                   |
| `\du`             | list database roles                                  |
| `\x`              | toggle expanded output (readable for wide rows)      |
| `\q`              | quit                                                 |

### Redis

```bash
docker exec -it forgeops-redis-1 redis-cli
```

You land at `127.0.0.1:6379>`. Useful commands:

| Command            | What it does                                     |
| :----------------- | :----------------------------------------------- |
| `KEYS *`           | every key — fine here, but see the warning below |
| `SCAN 0 COUNT 100` | paginated key listing, safe on a large keyspace  |
| `DBSIZE`           | number of keys                                   |
| `TYPE <key>`       | the data type of a key                           |
| `GET <key>`        | read a string value                              |
| `TTL <key>`        | seconds until expiry, `-1` if none               |
| `INFO keyspace`    | per-database key counts                          |
| `QUIT`             | leave                                            |

---

## 2. PostgreSQL — tables in this platform

The database holds **46 tables**. The ones worth showing:

| Table                         | What it holds                                                 |
| :---------------------------- | :------------------------------------------------------------ |
| `users`                       | accounts synced from the identity provider, with their role   |
| `sessions`                    | live login sessions — one row per authenticated browser       |
| `projects`                    | imported repositories                                         |
| `file_tree`                   | every indexed path in a project                               |
| `file_contents`               | the content of each indexed file (what the AI is grounded on) |
| `embeddings`                  | vector embeddings per file chunk, for semantic search         |
| `analysis_reports`            | readiness scores and their five-category breakdown            |
| `generation_runs`             | each AI generation run — prompt, model tier, tokens, outcome  |
| `change_sets`                 | a proposed modification, awaiting or past approval            |
| `change_items`                | the individual file operations inside a change set            |
| `approvals`                   | who approved or rejected which change set, and when           |
| `audit_events`                | the hash-linked, tamper-evident audit chain                   |
| `agent_devices`               | paired agents, their status and last heartbeat                |
| `deployments`                 | deployment attempts and their outcome                         |
| `environments`                | deployment targets                                            |
| `incidents`                   | failures and their analyses                                   |
| `policies` / `policy_bundles` | OPA policies and their published bundles                      |
| `secrets`                     | secret metadata (never plaintext values)                      |
| `alembic_version`             | the applied migration revision                                |

### List every table

```bash
docker exec forgeops-postgres-1 psql -U forgeops -d forgeops -c "\dt"
```

### Row count for every table at once (the good demo)

```bash
docker exec forgeops-postgres-1 psql -U forgeops -d forgeops -c "
SELECT relname AS table, n_live_tup AS rows
FROM pg_stat_user_tables
ORDER BY n_live_tup DESC, relname;"
```

### Row count from exact counts, not statistics

`n_live_tup` above is an estimate maintained by autovacuum. For exact numbers:

```bash
docker exec forgeops-postgres-1 psql -U forgeops -d forgeops -c "
SELECT 'users' AS table, count(*) FROM users
UNION ALL SELECT 'projects', count(*) FROM projects
UNION ALL SELECT 'file_tree', count(*) FROM file_tree
UNION ALL SELECT 'file_contents', count(*) FROM file_contents
UNION ALL SELECT 'embeddings', count(*) FROM embeddings
UNION ALL SELECT 'analysis_reports', count(*) FROM analysis_reports
UNION ALL SELECT 'generation_runs', count(*) FROM generation_runs
UNION ALL SELECT 'change_sets', count(*) FROM change_sets
UNION ALL SELECT 'approvals', count(*) FROM approvals
UNION ALL SELECT 'audit_events', count(*) FROM audit_events
UNION ALL SELECT 'agent_devices', count(*) FROM agent_devices
UNION ALL SELECT 'deployments', count(*) FROM deployments
ORDER BY 2 DESC;"
```

### Structure of a single table

```bash
docker exec forgeops-postgres-1 psql -U forgeops -d forgeops -c "\d change_sets"
```

### Sample rows — the tables that show something meaningful

```bash
# Users and their roles
docker exec forgeops-postgres-1 psql -U forgeops -d forgeops -c \
  "SELECT id, email, role, is_active, created_at FROM users ORDER BY created_at LIMIT 10;"

# Projects, with the path each was imported from
docker exec forgeops-postgres-1 psql -U forgeops -d forgeops -c \
  "SELECT id, name, path, created_at FROM projects ORDER BY created_at DESC LIMIT 10;"

# Indexed file tree
docker exec forgeops-postgres-1 psql -U forgeops -d forgeops -c \
  "SELECT path FROM file_tree ORDER BY path LIMIT 30;"

# Readiness reports with their scores
docker exec forgeops-postgres-1 psql -U forgeops -d forgeops -c \
  "SELECT id, project_id, score, created_at FROM analysis_reports ORDER BY created_at DESC LIMIT 5;"

# Generation runs: which model tier, how many tokens, whether a provider served it
docker exec forgeops-postgres-1 psql -U forgeops -d forgeops -c \
  "SELECT id, tier, served_from, prompt_tokens, completion_tokens, created_at
     FROM generation_runs ORDER BY created_at DESC LIMIT 5;"

# Change sets, with the blast-radius verdict that gates them
docker exec forgeops-postgres-1 psql -U forgeops -d forgeops -c \
  "SELECT id, status, blast_radius_score, blast_radius_verdict, created_at
     FROM change_sets ORDER BY created_at DESC LIMIT 5;"

# The individual file operations inside change sets
docker exec forgeops-postgres-1 psql -U forgeops -d forgeops -c \
  "SELECT change_set_id, file_path, action, ordinal FROM change_items ORDER BY file_path LIMIT 20;"

# Who approved what, and when
docker exec forgeops-postgres-1 psql -U forgeops -d forgeops -c \
  "SELECT change_set_id, approver_id, status, created_at
     FROM approvals ORDER BY created_at DESC LIMIT 10;"

# The audit chain, most recent first
docker exec forgeops-postgres-1 psql -U forgeops -d forgeops -c \
  "SELECT seq, actor_kind, action, resource_kind, outcome, created_at
     FROM audit_events ORDER BY seq DESC LIMIT 5;"

# The audit chain's hash links — each row commits to the one before it
docker exec forgeops-postgres-1 psql -U forgeops -d forgeops -c \
  "SELECT seq, encode(prev_hash,'hex') AS prev, encode(hash,'hex') AS hash
     FROM audit_events ORDER BY seq DESC LIMIT 3;"

# Paired agents and whether they are live
docker exec forgeops-postgres-1 psql -U forgeops -d forgeops -c \
  "SELECT id, status, agent_version, last_seen FROM agent_devices ORDER BY created_at DESC LIMIT 5;"

# Vector embeddings stored (0 until an embedding provider is reachable)
docker exec forgeops-postgres-1 psql -U forgeops -d forgeops -c \
  "SELECT count(*) AS embeddings FROM embeddings;"
```

Every query above was run against the live stack before being written down. If a column is
ever renamed, `\d <table>` is the authority — check it rather than trusting this list.

### Wide rows are easier to read vertically

Add `-x` to any `-c` command to print one column per line:

```bash
docker exec forgeops-postgres-1 psql -U forgeops -d forgeops -x -c \
  "SELECT * FROM generation_runs ORDER BY created_at DESC LIMIT 1;"
```

### Which migration is applied

```bash
docker exec forgeops-postgres-1 psql -U forgeops -d forgeops -c \
  "SELECT version_num FROM alembic_version;"
```

### Database roles

The platform uses three, deliberately separated so the application cannot alter its own schema:

```bash
docker exec forgeops-postgres-1 psql -U forgeops -d forgeops -c "\du"
```

| Role                | Used by                                     |
| :------------------ | :------------------------------------------ |
| `forgeops`          | the container's superuser (`POSTGRES_USER`) |
| `forgeops_app`      | the running backend — data access only      |
| `forgeops_migrator` | Alembic migrations — owns the schema        |

---

## 3. Redis — keys in this platform

Redis holds ephemeral state: caches, rate-limit buckets, and the in-flight envelope
bookkeeping. **Nothing here is the system of record** — losing it is survivable, which is why
it is a cache and not a database.

| Prefix                   | Holds                                                              |
| :----------------------- | :----------------------------------------------------------------- |
| `ai:cache:l2:`           | semantic cache entries — the L2 vector cache for model responses   |
| `forgeops:agentcmd:`     | commands in flight, awaiting an agent's result                     |
| `forgeops:cmdchangeset:` | the change set a delivered command belongs to                      |
| `forgeops:agentsession:` | which agent session owns an in-flight operation                    |
| `forgeops:nonce:`        | envelope nonces, so a replayed command is refused                  |
| `forgeops:envseq:`       | per-device envelope sequence numbers, so `seq` cannot go backwards |
| `forgeops:health-check`  | the readiness probe's own liveness marker                          |

> That list is measured, not assumed: `redis-cli --scan` on a running stack shows exactly these
> six `forgeops:` groups plus `ai:cache`. If the code adds a namespace, this table is what goes
> stale — check with the command below rather than trusting it.

### Every key

```bash
docker exec forgeops-redis-1 redis-cli KEYS '*'
```

> **`KEYS` blocks the server while it scans.** It is fine on a development keyspace of this
> size (tens to hundreds of keys). On anything large, use `SCAN 0 COUNT 100` and follow the
> returned cursor instead.

### Keys by prefix, with counts

```bash
docker exec forgeops-redis-1 redis-cli --scan --pattern 'ai:cache:l2:*' | wc -l
docker exec forgeops-redis-1 redis-cli --scan --pattern 'forgeops:agentcmd:*' | wc -l
docker exec forgeops-redis-1 redis-cli --scan --pattern 'forgeops:nonce:*' | wc -l
docker exec forgeops-redis-1 redis-cli --scan --pattern 'forgeops:envseq:*' | wc -l

# Every namespace in use, without knowing them in advance
docker exec forgeops-redis-1 redis-cli --scan | awk -F: '{print $1":"$2}' | sort | uniq -c
```

### Total keys and memory

```bash
docker exec forgeops-redis-1 redis-cli DBSIZE
docker exec forgeops-redis-1 redis-cli INFO keyspace
docker exec forgeops-redis-1 redis-cli INFO memory | grep -E 'used_memory_human|maxmemory_human'
```

### Inspect one key

```bash
# The first key of each kind, so these work without knowing today's ids
ENVSEQ=$(docker exec forgeops-redis-1 redis-cli --scan --pattern 'forgeops:envseq:*' | head -1)
NONCE=$(docker exec forgeops-redis-1 redis-cli --scan --pattern 'forgeops:nonce:*' | head -1)

# What type is it?
docker exec forgeops-redis-1 redis-cli TYPE "$ENVSEQ"

# Read it (strings)
docker exec forgeops-redis-1 redis-cli GET "$ENVSEQ"

# How long until it expires?
docker exec forgeops-redis-1 redis-cli TTL "$NONCE"
```

Substitute a literal key if you prefer — the ids are per-device and per-command, so any
example written down here would be expired by the time it is read:

```bash
docker exec forgeops-redis-1 redis-cli --scan --pattern 'forgeops:envseq:*' | head -1
```

### Vector index used by the semantic cache

Redis Stack exposes RediSearch. On this stack the L2 cache is keyed directly rather than
served by an index, so the list is empty and that is expected — the command is here for the
case where an index is added:

```bash
docker exec forgeops-redis-1 redis-cli FT._LIST
```

---

## 4. Find the live ports before connecting from the host

The launcher moves services off a port that is already taken, so the numbers are not
guaranteed. Read them from `.env` at the repository root:

```bash
grep -E '^(POSTGRES_PORT|REDIS_PORT|BACKEND_PORT|FRONTEND_PORT|AUTHENTIK_PORT)=' .env
```

On this machine they are:

| Service     | Host port |
| :---------- | :-------- |
| PostgreSQL  | 15432     |
| Redis       | 16379     |
| Backend API | 18000     |
| Frontend    | 13000     |
| Authentik   | 19000     |

To connect from the host rather than `docker exec`, use the host port:

```bash
psql -h 127.0.0.1 -p 15432 -U forgeops -d forgeops
redis-cli -h 127.0.0.1 -p 16379
```

Both are bound to `127.0.0.1` only, so neither is reachable from another machine. That is
deliberate — see the compose file's port bindings — and it is also why `docker exec` is the
simpler route when demonstrating on a lab machine.

The password is `POSTGRES_PASSWORD` in `.env`. Where `PGPASSWORD` is set, `psql` in the
container does not need it, because the container authenticates locally as the superuser.

---

## 5. Container names, if `docker exec` says "No such container"

```bash
docker compose ps --format '{{.Service}}  {{.Name}}  {{.State}}'
```

The names are stable for a given checkout directory, but confirm rather than assume:

```bash
docker ps --format '{{.Names}}\t{{.Status}}' | grep -E 'postgres|redis'
```

---

## 6. A one-command summary to show at a glance

```bash
echo "=== PostgreSQL ==="
docker exec forgeops-postgres-1 psql -U forgeops -d forgeops -c \
  "SELECT count(*) AS tables FROM pg_tables WHERE schemaname='public';"
docker exec forgeops-postgres-1 psql -U forgeops -d forgeops -c \
  "SELECT relname AS table, n_live_tup AS rows FROM pg_stat_user_tables
    WHERE n_live_tup > 0 ORDER BY n_live_tup DESC LIMIT 12;"
echo
echo "=== Redis ==="
docker exec forgeops-redis-1 redis-cli DBSIZE
docker exec forgeops-redis-1 redis-cli --scan --pattern 'ai:cache:*' | wc -l
docker exec forgeops-redis-1 redis-cli --scan --pattern 'forgeops:*' | wc -l
```

---

## Notes

- **Read-only.** Every command here is a query. None of them modifies, drops or truncates
  anything, and none of them needs the stack to be stopped.
- **`docker exec` needs the container running.** If a service is down, `docker compose up -d`
  first, or read the host-port table above and connect from the host instead.
- The Postgres password in `.env` starts as `change-me-locally` only for a hand-written file;
  the setup script replaces it with a generated value on first run.
