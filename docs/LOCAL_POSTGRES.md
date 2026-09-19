# Running PostgreSQL locally on Windows (no Docker, no admin rights)

The documented path for this project is Docker Compose (Phase 15). This note
covers the fallback used on the original development machine, where that was not
available.

## Why a portable cluster

Three constraints ruled out the usual options:

| Option | Blocker |
|---|---|
| Docker Desktop | Needs WSL2, which needs hardware virtualization. `HypervisorPresent` was `False` — a BIOS change and a reboot. |
| EDB Windows installer (winget / choco) | `get.enterprisedb.com` returns **403 Forbidden** to non-browser clients on this network. Both package managers download from that host, so both fail identically. |
| Chocolatey | Also requires an elevated shell. |

The binaries were instead taken from **Maven Central**, where the
`io.zonky.test.postgres` artifacts publish official PostgreSQL builds for use by
JVM test frameworks. No admin rights, no service registration, no reboot.

## Setup

```bash
PGROOT=/c/Users/LENOVO/pgsql17
VER=17.11.0
URL=https://repo1.maven.org/maven2/io/zonky/test/postgres/embedded-postgres-binaries-windows-amd64/$VER/embedded-postgres-binaries-windows-amd64-$VER.jar

mkdir -p "$PGROOT"
curl -sL -o pg.jar "$URL"
unzip -oq pg.jar postgres-windows-x86_64.txz
tar -xJf postgres-windows-x86_64.txz -C "$PGROOT"
```

Initialise the cluster (the password file is deleted immediately afterwards):

```bash
"$PGROOT/bin/initdb.exe" -D "$PGROOT/data" -U postgres \
  --auth-local=scram-sha-256 --auth-host=scram-sha-256 \
  --pwfile=/path/to/pw.txt --encoding=UTF8 --locale=C
```

## Start / stop

```bash
PGROOT=/c/Users/LENOVO/pgsql17

# start
"$PGROOT/bin/pg_ctl.exe" -D "$PGROOT/data" -l "$PGROOT/server.log" -o "-p 5432" start

# status
"$PGROOT/bin/pg_ctl.exe" -D "$PGROOT/data" status

# stop
"$PGROOT/bin/pg_ctl.exe" -D "$PGROOT/data" -m fast stop
```

## Two gotchas worth knowing

**Do not start the server from a shell you will later kill.** Starting it with
`pg_ctl -w` inside a process that is subsequently terminated leaves the
postmaster alive but breaks the session context its backends inherit. Every
later connection then dies with:

```
server process was terminated by exception 0xC0000142   (STATUS_DLL_INIT_FAILED)
```

The postmaster looks healthy and the port is listening, so this presents as
"connects, then immediately drops". Start without `-w` and let `pg_ctl` detach
properly.

**`psql.exe` is not included.** The Zonky build ships only the server binaries —
`postgres`, `initdb`, `pg_ctl`, plus `libpq`. This does not affect the
application: `psycopg[binary]` bundles its own libpq. All DDL and seeding in
Phase 2 is therefore driven from Python rather than from `psql` scripts, which
is a better outcome anyway — it is testable and cross-platform.

## Verified state

```
PostgreSQL 17.11 on x86_64-windows, compiled by msvc-19.44
encoding : UTF8
listening: 127.0.0.1:5432 and [::1]:5432
psycopg 3.3.5 connects and executes queries successfully
```
