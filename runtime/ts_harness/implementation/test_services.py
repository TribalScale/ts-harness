"""Databases and integration tests inside a coding job.

A backend repo's database tests and end-to-end suite need services the unit run
does not: Postgres, Redis, sometimes the app itself. The coding worker image
ships Postgres and Redis; this module starts the ones a repo needs on loopback,
creates the role and database the repo's env template names, writes a ``.env``
from that template with service hosts pointed at the local copies, and runs the
repo's integration suite with setup steps and background processes around it.

A repo can say what it needs in ``.ts-harness.yml``::

    services: [postgres, redis]          # detected from dependencies when omitted
    env_file: env-example-relational     # template for .env; detected when omitted
    env: {MAIL_HOST: 127.0.0.1}          # extra values for .env and the test commands
    integration:
      setup: [npm run build, npm run migration:run]
      background:
        - run: npm run start:prod
          wait_for: 127.0.0.1:3000       # host:port or an http(s) URL
      command: npm run test:e2e -- --runInBand
      timeout_seconds: 1200
      blocking: true                     # default true when declared here

Without an ``integration`` block, a ``test:integration``/``test:int``/``test:db``
script runs when a service is up, and its failures are advisory.
"""

from __future__ import annotations

import atexit
import glob
import hashlib
import json
import os
import pwd
import re
import shlex
import shutil
import signal
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import quote, unquote, urlsplit, urlunsplit

from ts_harness.implementation import db_servers

CONFIG_NAMES = (".ts-harness.yml", ".ts-harness.yaml")
SUPPORTED_SERVICES = ("postgres", "mysql", "mongodb", "redis")
SERVICES_ENV = "TS_HARNESS_SERVICES"
DEFAULT_INTEGRATION_TIMEOUT = 1200
DEFAULT_READY_TIMEOUT = 180
DEFAULT_SETUP_TIMEOUT = 900
_OUTPUT_LIMIT = 200_000

_NODE_PG = {"pg", "pg-promise", "postgres", "pg-native", "@mikro-orm/postgresql", "@databases/pg", "slonik"}
_NODE_REDIS = {"ioredis", "redis", "bullmq", "bull", "@keyv/redis", "cache-manager-redis-store", "cache-manager-ioredis"}
_NODE_MYSQL = {"mysql", "mysql2", "mariadb", "@mikro-orm/mysql", "@mikro-orm/mariadb"}
_NODE_MONGO = {"mongodb", "mongoose", "@mikro-orm/mongodb", "@typegoose/typegoose"}
_PY_MYSQL = re.compile(r"^\s*['\"]?(pymysql|mysqlclient|aiomysql|mysql-connector-python|asyncmy)\b", re.IGNORECASE | re.MULTILINE)
_PY_MONGO = re.compile(r"^\s*['\"]?(pymongo|motor|mongoengine|beanie)\b", re.IGNORECASE | re.MULTILINE)
_PY_PG = re.compile(r"^\s*['\"]?(psycopg2?(-binary)?|psycopg\[?|asyncpg)\b", re.IGNORECASE | re.MULTILINE)
_PY_REDIS = re.compile(r"^\s*['\"]?redis\b", re.IGNORECASE | re.MULTILINE)
_INTEGRATION_SCRIPTS = ("test:integration", "test:int", "test:db", "integration")

_DB_KEYS = {
    "url": ("DATABASE_URL", "POSTGRES_URL", "POSTGRESQL_URL", "PG_URL", "DB_URL", "TYPEORM_URL"),
    "user": ("DATABASE_USERNAME", "DATABASE_USER", "DB_USERNAME", "DB_USER", "POSTGRES_USER", "PGUSER", "TYPEORM_USERNAME"),
    "password": ("DATABASE_PASSWORD", "DB_PASSWORD", "DB_PASS", "POSTGRES_PASSWORD", "PGPASSWORD", "TYPEORM_PASSWORD"),
    "name": ("DATABASE_NAME", "DB_NAME", "DB_DATABASE", "POSTGRES_DB", "PGDATABASE", "TYPEORM_DATABASE"),
    "host": ("DATABASE_HOST", "DB_HOST", "POSTGRES_HOST", "PGHOST", "TYPEORM_HOST"),
    "port": ("DATABASE_PORT", "DB_PORT", "POSTGRES_PORT", "PGPORT", "TYPEORM_PORT"),
}
_REDIS_KEYS = {"url": ("REDIS_URL", "REDIS_URI"), "host": ("REDIS_HOST",), "port": ("REDIS_PORT",)}
_MYSQL_KEYS = {
    "url": ("MYSQL_URL",),
    "user": ("MYSQL_USER",),
    "password": ("MYSQL_PASSWORD",),
    "name": ("MYSQL_DATABASE", "MYSQL_DB"),
    "host": ("MYSQL_HOST",),
    "port": ("MYSQL_PORT",),
}
_MONGO_KEYS = {
    "url": ("MONGODB_URI", "MONGODB_URL", "MONGO_URI", "MONGO_URL", "MONGO_CONNECTION_STRING"),
    "name": ("MONGODB_DB", "MONGODB_DATABASE", "MONGO_DB", "MONGO_DATABASE", "MONGO_DB_NAME"),
    "host": ("MONGO_HOST", "MONGODB_HOST"),
    "port": ("MONGO_PORT", "MONGODB_PORT"),
}
_SQL_SCHEMES = {"postgres": ("postgres://", "postgresql://"), "mysql": ("mysql://", "mysql2://", "mariadb://")}
_LOCAL_HOSTS = {"localhost", "127.0.0.1", "0.0.0.0", "host.docker.internal"}


@dataclass
class DbCredentials:
    user: str = "postgres"
    password: str = "postgres"
    name: str = "app_test"


@dataclass
class RunningServices:
    """What a coding job started; ``env`` goes to OpenCode and every test command."""

    services: dict[str, dict[str, Any]] = field(default_factory=dict)
    env: dict[str, str] = field(default_factory=dict)
    unavailable: list[str] = field(default_factory=list)
    env_file: str = ""
    log: list[str] = field(default_factory=list)

    def note(self) -> str:
        """One paragraph for the coding prompt."""
        if not self.services and not self.unavailable:
            return ""
        parts = []
        if self.services:
            names = ", ".join(f"{name} on 127.0.0.1:{info['port']}" for name, info in self.services.items())
            parts.append(f"This job runs {names} for tests; the connection settings are in the environment")
            if self.env_file:
                parts[-1] += f" and in {self.env_file} (written from the repo's env template)"
            parts[-1] += ". Database and integration tests can run here: run them before finishing."
        if self.unavailable:
            parts.append(
                "Not available here: " + ", ".join(self.unavailable) + ". Tests that need it run only in CI; "
                "write them in the repo's existing style."
            )
        return " ".join(parts)


# --- repo configuration ---------------------------------------------------------------------------


def load_repo_config(repo_dir: Path) -> dict[str, Any]:
    import yaml

    for name in CONFIG_NAMES:
        path = Path(repo_dir) / name
        if path.is_file():
            try:
                data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
            except Exception:  # noqa: BLE001 - a broken file is the same as none, and is reported
                return {"_error": f"{name} is not valid YAML"}
            return data if isinstance(data, dict) else {"_error": f"{name} must be a mapping"}
    return {}


def _read(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def _package_deps(repo_dir: Path) -> set[str]:
    try:
        data = json.loads(_read(Path(repo_dir) / "package.json") or "{}")
    except json.JSONDecodeError:
        return set()
    deps: set[str] = set()
    for key in ("dependencies", "devDependencies", "optionalDependencies", "peerDependencies"):
        if isinstance(data.get(key), dict):
            deps.update(data[key])
    return deps


def compose_services(repo_dir: Path) -> dict[str, str]:
    """``{service name: image}`` from the repo's docker compose files."""
    import yaml

    found: dict[str, str] = {}
    for pattern in ("docker-compose*.y*ml", "compose*.y*ml"):
        for path in sorted(Path(repo_dir).glob(pattern)):
            try:
                data = yaml.safe_load(_read(path)) or {}
            except Exception:  # noqa: BLE001 - compose files with custom tags are skipped
                continue
            for name, spec in (data.get("services") or {}).items() if isinstance(data, dict) else []:
                image = str((spec or {}).get("image") or "") if isinstance(spec, dict) else ""
                found.setdefault(str(name), image)
    return found


def _image_service(image: str) -> str:
    base = image.split("@", 1)[0].rsplit("/", 1)[-1].split(":", 1)[0].lower()
    if base in {"postgres", "postgis", "timescaledb", "timescaledb-ha"}:
        return "postgres"
    if base in {"redis", "valkey", "redis-stack", "redis-stack-server"}:
        return "redis"
    if base in {"mysql", "mariadb", "percona"}:
        return "mysql"
    if base in {"mongo", "mongodb", "mongodb-community-server"}:
        return "mongodb"
    for other in ("elasticsearch", "opensearch", "kafka", "rabbitmq", "localstack", "minio", "cassandra", "nats"):
        if other in base:
            return other
    return ""


def detect_services(repo_dir: Path, config: dict[str, Any] | None = None) -> tuple[list[str], list[str]]:
    """(services to start, services the repo uses that the worker cannot run)."""
    repo_dir = Path(repo_dir)
    config = config if config is not None else load_repo_config(repo_dir)
    declared = config.get("services")
    wanted: set[str] = set()
    other: set[str] = set()
    if isinstance(declared, list):
        for name in declared:
            name = str(name).strip().lower()
            (wanted if name in SUPPORTED_SERVICES else other).add(name)
        return sorted(wanted), sorted(other)
    deps = _package_deps(repo_dir)
    if deps & _NODE_PG or re.search(r'provider\s*=\s*"postgres(ql)?"', _read(repo_dir / "prisma" / "schema.prisma")):
        wanted.add("postgres")
    if deps & _NODE_REDIS:
        wanted.add("redis")
    prisma = _read(repo_dir / "prisma" / "schema.prisma")
    if deps & _NODE_MYSQL or re.search(r'provider\s*=\s*"mysql"', prisma):
        wanted.add("mysql")
    if deps & _NODE_MONGO or re.search(r'provider\s*=\s*"mongodb"', prisma):
        wanted.add("mongodb")
    python_text = "\n".join(
        _read(repo_dir / name) for name in ("requirements.txt", "requirements-dev.txt", "requirements-test.txt", "pyproject.toml")
    )
    if _PY_PG.search(python_text):
        wanted.add("postgres")
    if _PY_REDIS.search(python_text):
        wanted.add("redis")
    if _PY_MYSQL.search(python_text):
        wanted.add("mysql")
    if _PY_MONGO.search(python_text):
        wanted.add("mongodb")
    for image in compose_services(repo_dir).values():
        service = _image_service(image)
        if service in SUPPORTED_SERVICES:
            wanted.add(service)
        elif service:
            other.add(service)
    return sorted(wanted), sorted(other - wanted)


# --- env template -----------------------------------------------------------------------------------


_ENV_LINE = re.compile(r"^(\s*(?:export\s+)?)([A-Za-z_][A-Za-z0-9_]*)(\s*=\s*)(.*)$")


def parse_env(text: str) -> dict[str, str]:
    values: dict[str, str] = {}
    for line in text.splitlines():
        match = _ENV_LINE.match(line)
        if match and not line.lstrip().startswith("#"):
            values[match.group(2)] = _unquote(match.group(4))
    return values


def _unquote(raw: str) -> str:
    value = raw.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
        return value[1:-1]
    return re.split(r"\s+#", value, maxsplit=1)[0].strip()


def find_env_template(repo_dir: Path, config: dict[str, Any] | None = None, services: list[str] | None = None) -> Path | None:
    repo_dir = Path(repo_dir)
    named = str((config or {}).get("env_file") or "").strip()
    if named:
        path = repo_dir / named
        return path if path.is_file() else None
    for name in (".env.test", ".env.testing", ".env.example", ".env.sample", ".env.dist", "example.env", ".env.template"):
        if (repo_dir / name).is_file():
            return repo_dir / name
    candidates = sorted(Path(p) for p in glob.glob(str(repo_dir / "env-example*")) + glob.glob(str(repo_dir / ".env.*.example")))
    if not candidates:
        return None
    for path in candidates:
        text = _read(path).lower()
        if any(service in text for service in services or []):
            return path
    return candidates[0]


def _first(values: dict[str, str], keys: tuple[str, ...]) -> str:
    return next((values[key] for key in keys if values.get(key)), "")


def db_credentials(template: dict[str, str], service: str = "postgres") -> DbCredentials:
    """Role and database the repo's env template names for ``service`` (postgres or mysql)."""
    creds = DbCredentials() if service == "postgres" else DbCredentials(user="root", password="root")
    if service == "mysql":
        url = _first(template, _MYSQL_KEYS["url"]) or _first(template, _DB_KEYS["url"])
        if url.startswith(_SQL_SCHEMES["mysql"]):
            parts = urlsplit(url)
            creds.user = unquote(parts.username or "") or creds.user
            creds.password = unquote(parts.password or "") or creds.password
            creds.name = parts.path.lstrip("/").split("?", 1)[0] or creds.name
        creds.user = _first(template, _MYSQL_KEYS["user"]) or _first(template, _DB_KEYS["user"]) or creds.user
        creds.password = (
            _first(template, _MYSQL_KEYS["password"]) or _first(template, _DB_KEYS["password"])
            or (template.get("MYSQL_ROOT_PASSWORD", "") if creds.user == "root" else "") or creds.password
        )
        creds.name = _first(template, _MYSQL_KEYS["name"]) or _first(template, _DB_KEYS["name"]) or creds.name
        return creds
    url = _first(template, _DB_KEYS["url"])
    if url.startswith(("postgres://", "postgresql://")):
        parts = urlsplit(url)
        creds.user = unquote(parts.username or "") or creds.user
        creds.password = unquote(parts.password or "") or creds.password
        creds.name = parts.path.lstrip("/").split("?", 1)[0] or creds.name
    creds.user = _first(template, _DB_KEYS["user"]) or creds.user
    creds.password = _first(template, _DB_KEYS["password"]) or creds.password
    creds.name = _first(template, _DB_KEYS["name"]) or creds.name
    return creds


def mongo_database(template: dict[str, str]) -> str:
    for key in (*_MONGO_KEYS["url"], *_DB_KEYS["url"]):
        value = template.get(key, "")
        if value.startswith("mongodb://"):
            name = urlsplit(value).path.lstrip("/").split("?", 1)[0]
            if name:
                return name
    return _first(template, _MONGO_KEYS["name"]) or "app_test"


def _rewrite_mongo_url(url: str, port: int, local_names: set[str]) -> str:
    """The job's MongoDB runs without auth: keep the database and options, drop credentials."""
    parts = urlsplit(url)
    hosts = [h.rsplit(":", 1)[0].strip("[]").lower() for h in parts.netloc.rsplit("@", 1)[-1].split(",")]
    if any(h and h not in local_names for h in hosts):
        return url
    query = "&".join(q for q in parts.query.split("&") if q and not q.lower().startswith(("authsource", "authmechanism")))
    return urlunsplit(("mongodb", f"127.0.0.1:{port}", parts.path, query, parts.fragment))


def _rewrite_url(url: str, port: int, local_names: set[str]) -> str:
    parts = urlsplit(url)
    host = parts.hostname or ""
    if host and host not in local_names:
        return url
    auth = parts.netloc.rsplit("@", 1)[0] + "@" if "@" in parts.netloc else ""
    return urlunsplit((parts.scheme, f"{auth}127.0.0.1:{port}", parts.path, parts.query, parts.fragment))


def rewrite_env(
    template: dict[str, str],
    services: dict[str, dict[str, Any]],
    compose_names: set[str],
) -> dict[str, str]:
    """The template's values that change so they reach the services running in this job."""
    local_names = _LOCAL_HOSTS | {name.lower() for name in compose_names}
    changes: dict[str, str] = {}
    pg, mysql, mongo, redis = (services.get(n) for n in ("postgres", "mysql", "mongodb", "redis"))
    sql = pg or mysql  # generic DATABASE_* keys go to the repo's SQL database

    def keyed(key: str) -> dict[str, Any] | None:
        if key.startswith(("POSTGRES_", "PG")):
            return pg
        if key.startswith("MYSQL_"):
            return mysql
        if key.startswith(("MONGO_", "MONGODB_")):
            return mongo
        if key.startswith("REDIS_"):
            return redis
        return sql

    for key, value in template.items():
        lowered = value.strip().lower()
        if pg and value.startswith(_SQL_SCHEMES["postgres"]):
            changes[key] = _rewrite_url(value, pg["port"], local_names)
        elif mysql and value.startswith(_SQL_SCHEMES["mysql"]):
            changes[key] = _rewrite_url(value, mysql["port"], local_names)
        elif mongo and value.startswith("mongodb://"):
            changes[key] = _rewrite_mongo_url(value, mongo["port"], local_names)
        elif redis and value.startswith(("redis://", "rediss://")):
            changes[key] = _rewrite_url(value, redis["port"], local_names)
        elif key in (*_DB_KEYS["host"], *_MYSQL_KEYS["host"], *_MONGO_KEYS["host"], *_REDIS_KEYS["host"]) and keyed(key):
            changes[key] = "127.0.0.1"
        elif key in (*_DB_KEYS["port"], *_MYSQL_KEYS["port"], *_MONGO_KEYS["port"], *_REDIS_KEYS["port"]) and keyed(key):
            changes[key] = str(keyed(key)["port"])
        elif key.endswith("HOST") and lowered in {name.lower() for name in compose_names}:
            changes[key] = "127.0.0.1"
    return {key: value for key, value in changes.items() if template.get(key) != value}


def render_env_file(text: str, values: dict[str, str]) -> str:
    """``text`` with each ``KEY=`` line set to ``values[KEY]``; keys it lacks are appended."""
    seen: set[str] = set()
    lines = []
    for line in text.splitlines():
        match = _ENV_LINE.match(line)
        if match and not line.lstrip().startswith("#") and match.group(2) in values:
            key = match.group(2)
            seen.add(key)
            line = f"{match.group(1)}{key}{match.group(3)}{_quote(values[key])}"
        lines.append(line)
    extra = [f"{key}={_quote(value)}" for key, value in values.items() if key not in seen]
    if extra:
        lines += ["", "# ts-harness: services in this coding job", *extra]
    return "\n".join(lines) + "\n"


def _quote(value: str) -> str:
    return value if re.fullmatch(r"[A-Za-z0-9_./:@%+,=-]*", value) else json.dumps(value)


# --- processes --------------------------------------------------------------------------------------


def free_port(preferred: int) -> int:
    for port in (preferred, 0):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            try:
                sock.bind(("127.0.0.1", port))
            except OSError:
                continue
            return int(sock.getsockname()[1])
    raise OSError("no free port")


def _pg_bin(name: str) -> str | None:
    found = sorted(glob.glob(f"/usr/lib/postgresql/*/bin/{name}"), key=lambda p: [int(x) for x in re.findall(r"\d+", p)])
    return found[-1] if found else shutil.which(name)


def _as_postgres(command: list[str]) -> list[str]:
    """initdb and postgres refuse to run as root; coding workers run as root."""
    if os.geteuid() != 0:
        return command
    return ["runuser", "-u", "postgres", "--", *command]


def _chown_postgres(path: Path) -> None:
    if os.geteuid() != 0:
        return
    entry = pwd.getpwnam("postgres")
    for root, dirs, files in os.walk(path):
        os.chown(root, entry.pw_uid, entry.pw_gid)
        for name in [*dirs, *files]:
            os.chown(os.path.join(root, name), entry.pw_uid, entry.pw_gid)


def _sql_ident(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def _sql_literal(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def _run(runner, command: list[str], *, env: dict[str, str] | None = None, timeout: int = 120) -> subprocess.CompletedProcess:
    try:
        return runner(command, capture_output=True, text=True, check=False, env=env, timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        return subprocess.CompletedProcess(command, 124, stdout=str(exc.stdout or ""), stderr=f"timed out after {timeout}s")
    except OSError as exc:
        return subprocess.CompletedProcess(command, 127, stdout="", stderr=str(exc))


def _tail(result: subprocess.CompletedProcess, limit: int = 1500) -> str:
    return "\n".join(part for part in (result.stdout, result.stderr) if part).strip()[-limit:]


def _psql(runner, port: int, sql: str, *, database: str = "postgres") -> subprocess.CompletedProcess:
    psql = _pg_bin("psql") or "psql"
    return _run(
        runner,
        [psql, "-h", "127.0.0.1", "-p", str(port), "-U", "postgres", "-d", database, "-v", "ON_ERROR_STOP=1", "-qAt", "-c", sql],
        env={**os.environ, "PGPASSWORD": "", "PGCONNECT_TIMEOUT": "10"},
    )


def start_postgres(root: Path, creds: DbCredentials, *, runner=subprocess.run) -> tuple[dict[str, Any] | None, str]:
    initdb, pg_ctl = _pg_bin("initdb"), _pg_bin("pg_ctl")
    if not initdb or not pg_ctl:
        return None, "postgres is not installed in this worker"
    data = root / "pgdata"
    sockets = root / "pgsock"
    state = root / "postgres.json"
    if state.is_file():
        info = json.loads(_read(state) or "{}")
        if info.get("port") and _psql(runner, int(info["port"]), "select 1").returncode == 0:
            return info, f"postgres already running on 127.0.0.1:{info['port']}"
    root.mkdir(parents=True, exist_ok=True)
    if not (data / "PG_VERSION").is_file():
        data.mkdir(parents=True, exist_ok=True)
        _chown_postgres(data)
        made = _run(runner, _as_postgres([initdb, "-D", str(data), "-U", "postgres", "--auth=trust", "-E", "UTF8", "--no-locale"]), timeout=180)
        if made.returncode != 0:
            return None, f"postgres initdb failed: {_tail(made)}"
    sockets.mkdir(parents=True, exist_ok=True)
    # Only postgres's own directories: MySQL or MongoDB may share ``root``.
    _chown_postgres(data)
    _chown_postgres(sockets)
    port = free_port(5432)
    options = (
        f"-c listen_addresses=127.0.0.1 -p {port} -c unix_socket_directories={sockets} "
        "-c fsync=off -c synchronous_commit=off -c full_page_writes=off -c max_connections=200"
    )
    started = _run(runner, _as_postgres([pg_ctl, "-D", str(data), "-l", str(sockets / "postgres.log"), "-w", "-t", "60", "-o", options, "start"]), timeout=120)
    if started.returncode != 0:
        return None, f"postgres failed to start: {_tail(started)} {_read(sockets / 'postgres.log')[-800:]}"
    info = {"port": port, "data": str(data), "user": creds.user, "password": creds.password, "database": creds.name}
    role = _sql_ident(creds.user)
    sql = (
        f"ALTER ROLE postgres PASSWORD {_sql_literal(creds.password)}"
        if creds.user == "postgres"
        else f"DO $$BEGIN IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = {_sql_literal(creds.user)}) "
        f"THEN CREATE ROLE {role} LOGIN SUPERUSER; END IF; END$$; ALTER ROLE {role} PASSWORD {_sql_literal(creds.password)}"
    )
    made = _psql(runner, port, sql)
    if made.returncode != 0:
        return None, f"postgres role setup failed: {_tail(made)}"
    reset = reset_postgres(info, runner=runner)
    if reset:
        return None, reset
    state.write_text(json.dumps(info), encoding="utf-8")
    atexit.register(stop_postgres, info)
    return info, f"postgres ready on 127.0.0.1:{port} (database {creds.name}, user {creds.user})"


def reset_postgres(info: dict[str, Any], *, runner=subprocess.run) -> str:
    """Drop and recreate the test database, so each integration run starts empty."""
    name, owner = _sql_ident(str(info["database"])), _sql_ident(str(info["user"]))
    for sql in (f"DROP DATABASE IF EXISTS {name} WITH (FORCE)", f"CREATE DATABASE {name} OWNER {owner}"):
        result = _psql(runner, int(info["port"]), sql)
        if result.returncode != 0:
            return f"postgres database reset failed: {_tail(result)}"
    return ""


def stop_postgres(info: dict[str, Any]) -> None:
    pg_ctl = _pg_bin("pg_ctl")
    if pg_ctl and info.get("data"):
        _run(subprocess.run, _as_postgres([pg_ctl, "-D", str(info["data"]), "-m", "fast", "-w", "stop"]), timeout=60)


def start_redis(root: Path, *, runner=subprocess.run) -> tuple[dict[str, Any] | None, str]:
    server = shutil.which("redis-server") or shutil.which("valkey-server")
    if not server:
        return None, "redis is not installed in this worker"
    state = root / "redis.json"
    if state.is_file():
        info = json.loads(_read(state) or "{}")
        if info.get("port") and _redis_ping(int(info["port"])):
            return info, f"redis already running on 127.0.0.1:{info['port']}"
    root.mkdir(parents=True, exist_ok=True)
    port = free_port(6379)
    started = _run(
        runner,
        [server, "--bind", "127.0.0.1", "--port", str(port), "--save", "", "--appendonly", "no", "--daemonize", "yes",
         "--pidfile", str(root / "redis.pid"), "--logfile", str(root / "redis.log"), "--dir", str(root)],
    )
    if started.returncode != 0:
        return None, f"redis failed to start: {_tail(started)}"
    deadline = time.monotonic() + 15
    while not _redis_ping(port):
        if time.monotonic() > deadline:
            return None, "redis did not answer within 15s"
        time.sleep(0.2)
    info = {"port": port, "pidfile": str(root / "redis.pid")}
    state.write_text(json.dumps(info), encoding="utf-8")
    atexit.register(stop_redis, info)
    return info, f"redis ready on 127.0.0.1:{port}"


def _redis_ping(port: int) -> bool:
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=1) as sock:
            sock.sendall(b"PING\r\n")
            return sock.recv(16).startswith(b"+PONG")
    except OSError:
        return False


def flush_redis(info: dict[str, Any]) -> None:
    try:
        with socket.create_connection(("127.0.0.1", int(info["port"])), timeout=2) as sock:
            sock.sendall(b"FLUSHALL\r\n")
            sock.recv(16)
    except OSError:
        pass


def stop_redis(info: dict[str, Any]) -> None:
    try:
        pid = int(_read(Path(info["pidfile"])).strip() or 0)
        if pid:
            os.kill(pid, signal.SIGTERM)
    except (OSError, ValueError, KeyError):
        pass


# --- setup for a coding job -------------------------------------------------------------------------


def services_root(repo_dir: Path) -> Path:
    digest = hashlib.sha256(str(Path(repo_dir).resolve()).encode()).hexdigest()[:12]
    return Path(os.environ.get("TS_HARNESS_SERVICES_DIR") or "/tmp/ts-harness-services") / digest


def _service_env(
    services: dict[str, dict[str, Any]], creds: DbCredentials, mysql_creds: DbCredentials | None = None
) -> dict[str, str]:
    env: dict[str, str] = {}
    mysql = services.get("mysql")
    if mysql and mysql_creds is not None:
        user, password, name = (quote(v, safe="") for v in (mysql_creds.user, mysql_creds.password, mysql_creds.name))
        url = f"mysql://{user}:{password}@127.0.0.1:{mysql['port']}/{name}"
        env.update({"MYSQL_URL": url, "MYSQL_HOST": "127.0.0.1", "MYSQL_PORT": str(mysql["port"]),
                    "MYSQL_USER": mysql_creds.user, "MYSQL_PASSWORD": mysql_creds.password, "MYSQL_DATABASE": mysql_creds.name})
        if not services.get("postgres"):
            env["DATABASE_URL"] = url
    mongo = services.get("mongodb")
    if mongo:
        url = f"mongodb://127.0.0.1:{mongo['port']}/{mongo['database']}"
        env.update({"MONGODB_URI": url, "MONGODB_URL": url, "MONGO_URI": url, "MONGO_URL": url})
        if not services.get("postgres") and not services.get("mysql"):
            env["DATABASE_URL"] = url
    pg = services.get("postgres")
    if pg:
        url = f"postgresql://{quote(creds.user, safe='')}:{quote(creds.password, safe='')}@127.0.0.1:{pg['port']}/{quote(creds.name, safe='')}"
        env.update(
            {
                "DATABASE_URL": url,
                "PGHOST": "127.0.0.1",
                "PGPORT": str(pg["port"]),
                "PGUSER": creds.user,
                "PGPASSWORD": creds.password,
                "PGDATABASE": creds.name,
            }
        )
    redis = services.get("redis")
    if redis:
        env.update({"REDIS_URL": f"redis://127.0.0.1:{redis['port']}", "REDIS_HOST": "127.0.0.1", "REDIS_PORT": str(redis["port"])})
    return env


def _git_ignored(repo_dir: Path, rel: str, runner) -> bool:
    result = _run(runner, ["git", "-C", str(repo_dir), "check-ignore", "-q", rel])
    return result.returncode == 0


def _exclude_locally(repo_dir: Path, rel: str) -> None:
    exclude = Path(repo_dir) / ".git" / "info" / "exclude"
    if not exclude.parent.is_dir():
        return
    lines = _read(exclude).splitlines()
    if f"/{rel}" not in lines:
        text = _read(exclude)
        exclude.write_text(text + ("" if not text or text.endswith("\n") else "\n") + f"/{rel}\n", encoding="utf-8")


def start_test_services(repo_dir: Path, *, runner=subprocess.run) -> RunningServices:
    """Start what the repo needs and point its env at it. Never raises: the log says what failed."""
    repo_dir = Path(repo_dir)
    running = RunningServices()
    config = load_repo_config(repo_dir)
    if config.get("_error"):
        running.log.append(str(config["_error"]))
    wanted, unavailable = detect_services(repo_dir, config)
    running.unavailable = list(unavailable)
    overrides = {str(k): str(v) for k, v in (config.get("env") or {}).items()} if isinstance(config.get("env"), dict) else {}
    if not wanted:
        # A frontend needs no database but may still need its declared env file (API URL,
        # mocking switch) for its e2e suite.
        if config.get("env_file"):
            template_path = find_env_template(repo_dir, config, [])
            if template_path is not None:
                _write_env_file(repo_dir, config, template_path, _read(template_path), overrides, runner, running)
            running.env = dict(overrides)
        return running
    template_path = find_env_template(repo_dir, config, wanted)
    template_text = _read(template_path) if template_path else ""
    template = parse_env(template_text)
    creds = db_credentials(template)
    mysql_creds = db_credentials(template, "mysql")
    root = services_root(repo_dir)
    for name in wanted:
        if name == "postgres":
            info, message = start_postgres(root, creds, runner=runner)
        elif name == "mysql":
            info, message = db_servers.start_mysql(root, mysql_creds, port=free_port(3306), runner=runner)
        elif name == "mongodb":
            info, message = db_servers.start_mongodb(root, mongo_database(template), port=free_port(27017))
        else:
            info, message = start_redis(root, runner=runner)
        running.log.append(message)
        if info is None:
            running.unavailable.append(name)
        else:
            running.services[name] = info
    if not running.services:
        return running
    changes = {**rewrite_env(template, running.services, set(compose_services(repo_dir))), **overrides}
    service_env = _service_env(running.services, creds, mysql_creds)
    if template_path is not None:
        _write_env_file(repo_dir, config, template_path, template_text, changes, runner, running)
    running.env = {
        **service_env,
        **{key: value for key, value in {**template, **changes}.items() if value and _connection_key(key)},
        **overrides,
        SERVICES_ENV: json.dumps(running.services),
    }
    return running


def _write_env_file(
    repo_dir: Path, config: dict[str, Any], template_path: Path, template_text: str,
    changes: dict[str, str], runner, running: RunningServices,
) -> None:
    """``.env`` from the template, only when absent, and never committed."""
    target = str(config.get("env_target") or ".env")
    if (repo_dir / target).exists():
        return
    (repo_dir / target).write_text(render_env_file(template_text, changes), encoding="utf-8")
    if not _git_ignored(repo_dir, target, runner):
        _exclude_locally(repo_dir, target)
    running.env_file = target
    running.log.append(f"wrote {target} from {template_path.name}")


def _connection_key(key: str) -> bool:
    return any(
        key in keys for keys in (*_DB_KEYS.values(), *_REDIS_KEYS.values(), *_MYSQL_KEYS.values(), *_MONGO_KEYS.values())
    )


# --- integration suite ------------------------------------------------------------------------------


@dataclass
class IntegrationPlan:
    command: str
    setup: list[str] = field(default_factory=list)
    background: list[dict[str, Any]] = field(default_factory=list)
    timeout_seconds: int = DEFAULT_INTEGRATION_TIMEOUT
    blocking: bool = True
    source: str = "config"
    # Screenshots for review and QA: Playwright specs the change touched, rerun with screenshots on.
    evidence_specs: list[str] = field(default_factory=list)
    evidence_dir: str = ""


def integration_plan(
    repo_dir: Path, *, services_running: bool, changed_files: list[str] | None = None
) -> IntegrationPlan | None:
    """The configured integration suite, or a ``test:integration``-style script when a service is up."""
    config = load_repo_config(repo_dir)
    block = config.get("integration")
    if isinstance(block, dict) and str(block.get("command") or "").strip():
        background = []
        for item in block.get("background") or []:
            if isinstance(item, str):
                item = {"run": item}
            if isinstance(item, dict) and str(item.get("run") or "").strip():
                background.append(item)
        return IntegrationPlan(
            command=str(block["command"]).strip(),
            setup=[str(step) for step in block.get("setup") or [] if str(step).strip()],
            background=background,
            timeout_seconds=int(block.get("timeout_seconds") or DEFAULT_INTEGRATION_TIMEOUT),
            blocking=bool(block.get("blocking", True)),
        )
    from ts_harness.implementation.test_env import node_run_command, package_scripts, uses_playwright

    config_file = playwright_config(repo_dir, changed_files)
    if config_file is not None and (uses_playwright(repo_dir) or uses_playwright(config_file.parent)):
        # Playwright starts the app itself through the config's webServer; with
        # nothing declared, its result is evidence, not a gate.
        from ts_harness.implementation.workspaces import find_bin

        binary = find_bin(repo_dir, config_file.parent, "playwright") or Path(repo_dir) / "node_modules" / ".bin" / "playwright"
        command = shlex.join([str(binary), "test", "--reporter=line"])
        if config_file.parent != Path(repo_dir):
            command = f"cd {shlex.quote(str(config_file.parent))} && {command}"
        return IntegrationPlan(command=command, blocking=False, source="playwright")
    if not services_running:
        return None

    scripts = package_scripts(repo_dir)
    script = next((name for name in _INTEGRATION_SCRIPTS if name in scripts), None)
    if script is None:
        return None
    return IntegrationPlan(command=shlex.join(node_run_command(repo_dir, script)), blocking=False, source=f"script:{script}")


def _parse_target(target: str) -> tuple[str, str, int]:
    if target.startswith(("http://", "https://")):
        parts = urlsplit(target)
        return "http", target, parts.port or (443 if parts.scheme == "https" else 80)
    host, _, port = target.rpartition(":")
    return "tcp", host or "127.0.0.1", int(port)


def _ready(target: str) -> bool:
    kind, host, port = _parse_target(target)
    if kind == "http":
        try:
            with urllib.request.urlopen(host, timeout=3):  # noqa: S310 - loopback URL from the repo's config
                return True
        except urllib.error.HTTPError:
            return True
        except (OSError, ValueError):
            return False
    try:
        with socket.create_connection((host, port), timeout=1):
            return True
    except OSError:
        return False


def _group_alive(pgid: int) -> bool:
    """Whether any live (non-zombie) process is left in group ``pgid``."""
    proc_root = Path("/proc")
    if not proc_root.is_dir():
        try:
            os.killpg(pgid, 0)
        except OSError:
            return False
        return True
    for stat in proc_root.glob("[0-9]*/stat"):
        try:
            fields = stat.read_text().rsplit(")", 1)[1].split()
        except (OSError, IndexError):
            continue
        # After the command name: state, ppid, pgrp.
        if len(fields) > 2 and fields[2] == str(pgid) and fields[0] != "Z":
            return True
    return False


def _stop_group(proc: subprocess.Popen, *, grace_seconds: float = 15) -> None:
    """Stop the whole process group, not only its shell: the app it started holds the port.

    Returns once no live process is left, so the next run cannot find an old server
    already answering on the port it waits for.
    """
    pgid = proc.pid
    for sig, wait in ((signal.SIGTERM, grace_seconds), (signal.SIGKILL, 5.0)):
        try:
            os.killpg(pgid, sig)
        except OSError:
            pass
        deadline = time.monotonic() + wait
        while time.monotonic() < deadline:
            proc.poll()
            if not _group_alive(pgid):
                proc.poll()
                return
            time.sleep(0.1)
    proc.poll()


def _shell(command: str, *, cwd: Path, env: dict[str, str], timeout: int) -> tuple[int, str]:
    try:
        done = subprocess.run(
            command, shell=True, cwd=str(cwd), env=env, capture_output=True, text=True, timeout=timeout,
            start_new_session=True,
        )
        return done.returncode, "\n".join(part for part in (done.stdout, done.stderr) if part)
    except subprocess.TimeoutExpired as exc:
        out = "\n".join(str(part) for part in (exc.stdout, exc.stderr) if part)
        return 124, f"{out}\ntimed out after {timeout}s"


def reset_services(env: dict[str, str], *, runner=subprocess.run) -> str:
    try:
        services = json.loads(env.get(SERVICES_ENV) or "{}")
    except json.JSONDecodeError:
        return ""
    if services.get("redis"):
        flush_redis(services["redis"])
    problems = []
    if services.get("postgres"):
        problems.append(reset_postgres(services["postgres"], runner=runner))
    if services.get("mysql"):
        problems.append(db_servers.reset_mysql(services["mysql"], runner=runner))
    if services.get("mongodb"):
        problems.append(db_servers.reset_mongodb(services["mongodb"]))
    return "; ".join(p for p in problems if p)


def run_integration(
    repo_dir: Path, plan: IntegrationPlan, env: dict[str, str], *, log_dir: Path | None = None
) -> subprocess.CompletedProcess:
    """Reset the services, run setup, start background processes, run the suite, stop them."""
    repo_dir = Path(repo_dir)
    log_dir = Path(log_dir or services_root(repo_dir))
    log_dir.mkdir(parents=True, exist_ok=True)
    output: list[str] = []
    args = ["sh", "-c", plan.command]

    def finished(code: int) -> subprocess.CompletedProcess:
        return subprocess.CompletedProcess(args, code, stdout="\n".join(output)[-_OUTPUT_LIMIT:], stderr="")

    problem = reset_services(env)
    if problem:
        output.append(problem)
        return finished(1)
    for step in plan.setup:
        code, text = _shell(step, cwd=repo_dir, env=env, timeout=DEFAULT_SETUP_TIMEOUT)
        output.append(f"$ {step}\n{text[-4000:]}")
        if code != 0:
            output.append(f"setup step failed (exit {code}): {step}")
            return finished(code)
    procs: list[tuple[subprocess.Popen, Path, dict[str, Any]]] = []
    try:
        for index, item in enumerate(plan.background):
            log_path = log_dir / f"background-{index}.log"
            target = str(item.get("wait_for") or "").strip()
            if target and _ready(target):
                # Something else already answers there; waiting would test against it, not this run.
                output.append(f"background `{item['run']}`: {target} is already in use before it started")
                return finished(1)
            handle = open(log_path, "w", encoding="utf-8")  # noqa: SIM115 - closed with the process
            proc = subprocess.Popen(
                str(item["run"]), shell=True, cwd=str(repo_dir), env=env, stdout=handle, stderr=subprocess.STDOUT,
                start_new_session=True,
            )
            handle.close()
            procs.append((proc, log_path, item))
            target = str(item.get("wait_for") or "").strip()
            if not target:
                continue
            deadline = time.monotonic() + int(item.get("ready_timeout_seconds") or DEFAULT_READY_TIMEOUT)
            while not _ready(target):
                if proc.poll() is not None or time.monotonic() > deadline:
                    why = f"exited with {proc.returncode}" if proc.poll() is not None else "timed out"
                    output.append(f"background `{item['run']}` not ready at {target} ({why}):\n{_read(log_path)[-4000:]}")
                    return finished(1)
                time.sleep(1)
            output.append(f"background `{item['run']}` ready at {target}")
        code, text = _shell(plan.command, cwd=repo_dir, env=env, timeout=plan.timeout_seconds)
        output.append(f"$ {plan.command}\n{text}")
        if code != 0:
            for proc, log_path, item in procs:
                output.append(f"log of `{item['run']}` (end):\n{_read(log_path)[-2000:]}")
        elif plan.evidence_specs and plan.evidence_dir:
            output.append(capture_screenshots(repo_dir, plan.evidence_specs, Path(plan.evidence_dir), env))
        return finished(code)
    finally:
        for proc, _log_path, _item in procs:
            _stop_group(proc)


def write_agent_command(bin_dir: Path) -> Path:
    """``ts-harness-integration`` on the job's PATH, so OpenCode runs the same suite the host verifies."""
    bin_dir.mkdir(parents=True, exist_ok=True)
    path = bin_dir / "ts-harness-integration"
    path.write_text(
        "#!/bin/sh\n"
        "# Runs the repo's integration suite with this job's databases (reset first).\n"
        "# With test files as arguments, runs only those: ts-harness-integration e2e/a.spec.ts\n"
        f'exec {shlex.quote(sys.executable)} -m ts_harness.implementation.test_services integration "$PWD" "$@"\n',
        encoding="utf-8",
    )
    path.chmod(0o755)
    return path


def scoped_plan(repo_dir: Path, plan: IntegrationPlan, tests: list[str]) -> IntegrationPlan:
    """``plan`` running only ``tests``, with the same setup and background processes.

    The coding agent runs the tests it added or changed; the host runs the whole suite after
    it finishes. With a Playwright config the files go to ``playwright test``; otherwise they
    are appended to the suite command (after ``--`` for ``npm run``).
    """
    from dataclasses import replace

    from ts_harness.implementation.workspaces import find_bin

    repo_dir = Path(repo_dir)
    base = playwright_config(repo_dir, tests)
    binary = find_bin(repo_dir, base.parent, "playwright") if base is not None else None
    if base is not None and binary is not None:
        relative = [os.path.relpath(repo_dir / test, base.parent) for test in tests]
        command = f"cd {shlex.quote(str(base.parent))} && " + shlex.join(
            [str(binary), "test", "--config", str(base), *relative]
        )
    else:
        separator = " --" if plan.command.lstrip().startswith("npm run") else ""
        command = plan.command + separator + " " + shlex.join(tests)
    return replace(plan, command=command, evidence_specs=[], evidence_dir="")


def main(argv: list[str]) -> int:
    if len(argv) < 2 or argv[0] != "integration":
        print("usage: python -m ts_harness.implementation.test_services integration <repo_dir> [test files]", file=sys.stderr)
        return 2
    repo_dir = Path(argv[1])
    tests = [arg for arg in argv[2:] if arg.strip()]
    plan = integration_plan(repo_dir, services_running=bool(os.environ.get(SERVICES_ENV)))
    if plan is None:
        print("No integration suite: add an `integration` block to .ts-harness.yml or a test:integration script.")
        return 0
    if tests:
        plan = scoped_plan(repo_dir, plan, tests)
        print(f"Running only: {' '.join(tests)} (the host runs the whole suite after you finish)")
    result = run_integration(repo_dir, plan, dict(os.environ))
    print(result.stdout)
    print(f"integration suite exit {result.returncode}")
    return result.returncode



# --- processes the coding agent left behind ---------------------------------------------------------


def _proc_table() -> dict[int, tuple[int, str, str]]:
    """``{pid: (ppid, state, cmdline)}`` from /proc."""
    table: dict[int, tuple[int, str, str]] = {}
    for stat in Path("/proc").glob("[0-9]*/stat"):
        try:
            fields = stat.read_text().rsplit(")", 1)[1].split()
            cmd = (stat.parent / "cmdline").read_bytes().replace(b"\0", b" ").decode(errors="replace").strip()
            table[int(stat.parent.name)] = (int(fields[1]), fields[0], cmd)
        except (OSError, ValueError, IndexError):
            continue
    return table


def _ancestors(pid: int, table: dict[int, tuple[int, str, str]]) -> list[int]:
    chain = []
    while pid in table and pid not in chain:
        chain.append(pid)
        pid = table[pid][0]
    return chain


def in_coding_worker(table: dict[int, tuple[int, str, str]] | None = None) -> bool:
    """True inside a one-job worker container: the coding worker script is this process or an ancestor."""
    table = table if table is not None else _proc_table()
    return any("coding-worker" in table[pid][2] for pid in _ancestors(os.getpid(), table))


def _service_pids(repo_dir: Path) -> set[int]:
    root = services_root(repo_dir)
    pids: set[int] = set()
    for path in (root / "pgdata" / "postmaster.pid", root / "redis.pid", root / "mysqlrun" / "mysql.pid", root / "mongod.pid"):
        try:
            pids.add(int(_read(path).splitlines()[0].strip()))
        except (ValueError, IndexError):
            continue
    return pids


def stop_stray_processes(repo_dir: Path, *, table: dict[int, tuple[int, str, str]] | None = None, kill=os.kill) -> list[str]:
    """Stop servers the coding agent started and left running (``yarn dev &``, a mock API).

    They hold the ports the repo's e2e suite starts its own servers on, so host
    verification would fail on "port already in use" or test a stale server. Only in a
    worker container: everything re-parented to PID 1 is a leftover, except this job's
    databases.
    """
    if table is None:
        table = _proc_table()
        if not in_coding_worker(table):
            return []
    # The worker script and this process stay; with ``docker run --init`` the worker is
    # itself a child of PID 1, like the leftovers.
    protected = _service_pids(repo_dir) | set(_ancestors(os.getpid(), table))
    children: dict[int, list[int]] = {}
    for pid, (ppid, _state, _cmd) in table.items():
        children.setdefault(ppid, []).append(pid)
    stray_roots = [pid for pid, (ppid, state, _cmd) in table.items() if ppid == 1 and pid not in protected and state != "Z"]
    stopped: list[str] = []
    for root in stray_roots:
        tree, frontier = [], [root]
        while frontier:
            pid = frontier.pop()
            tree.append(pid)
            frontier.extend(children.get(pid, []))
        for pid in tree:
            try:
                kill(pid, signal.SIGKILL)
            except OSError:
                continue
        stopped.append(f"{root}: {table[root][2][:120]}")
    return stopped


# --- screenshots ------------------------------------------------------------------------------------

_SPEC_SUFFIXES = (".spec.ts", ".spec.tsx", ".spec.js", ".spec.mjs", ".test.ts", ".test.js", ".e2e.ts", ".e2e-spec.ts")
MAX_SCREENSHOTS = 6
EVIDENCE_CONFIG = "playwright.ts-harness-evidence.config.ts"


def playwright_config(repo_dir: Path, changed_files: list[str] | None = None) -> Path | None:
    """The repo's Playwright config, or in a monorepo the one of a package the change touched."""
    found = sorted(Path(repo_dir).glob("playwright.config.*"))
    if found:
        return found[0]
    from ts_harness.implementation.workspaces import changed_packages

    for package in changed_packages(repo_dir, changed_files or []):
        found = sorted(package.glob("playwright.config.*"))
        if found:
            return found[0]
    return None


def changed_playwright_specs(repo_dir: Path, changed_files: list[str]) -> list[str]:
    """Playwright specs the change added or edited (paths relative to the repo)."""
    repo_dir = Path(repo_dir)
    specs = []
    for path in changed_files:
        parts = {part.lower() for part in Path(path).parts}
        if path.endswith(_SPEC_SUFFIXES) and parts & {"e2e", "playwright", "tests-e2e", "e2e-tests"} and (repo_dir / path).is_file():
            specs.append(path)
    return sorted(specs)


def _evidence_config_text(base: Path, out_dir: Path) -> str:
    """The repo's own config (web server, setup project, storage state) with screenshots on."""
    return (
        "// Written by TS Harness for one run; not part of the repository.\n"
        f"import base from './{base.stem}';\n"
        "const shot = { screenshot: 'on' as const, video: 'off' as const, trace: 'on' as const };\n"
        "export default {\n"
        "  ...base,\n"
        f"  outputDir: {json.dumps(str(out_dir))},\n"
        "  reporter: 'line',\n"
        "  retries: 0,\n"
        "  use: { ...(base.use ?? {}), ...shot },\n"
        "  projects: (base.projects ?? []).map((project: any) => ({ ...project, use: { ...(project.use ?? {}), ...shot } })),\n"
        "};\n"
    )


def capture_screenshots(repo_dir: Path, specs: list[str], out_dir: Path, env: dict[str, str], *, timeout: int = 600) -> str:
    """Rerun ``specs`` with screenshots on; images land in ``out_dir``. Never fails verification."""
    from ts_harness.implementation.workspaces import find_bin

    repo_dir = Path(repo_dir)
    base = playwright_config(repo_dir, specs)
    binary = find_bin(repo_dir, base.parent, "playwright") if base is not None else None
    if base is None or binary is None:
        return "screenshots: no Playwright config or binary"
    specs = [os.path.relpath(repo_dir / spec, base.parent) for spec in specs]
    shutil.rmtree(out_dir, ignore_errors=True)
    out_dir.mkdir(parents=True, exist_ok=True)
    wrapper = base.parent / EVIDENCE_CONFIG
    wrapper.write_text(_evidence_config_text(base, out_dir), encoding="utf-8")
    try:
        command = shlex.join([str(binary), "test", "--config", str(wrapper), *specs])
        code, text = _shell(command, cwd=base.parent, env=env, timeout=timeout)
    finally:
        wrapper.unlink(missing_ok=True)
    for trace in sorted(out_dir.rglob("trace.zip")):
        if "setup" not in trace.parent.name.lower():
            assertion_frames(trace, trace.parent)
    shots = screenshot_files(out_dir)
    return f"screenshots: {len(shots)} captured from {', '.join(specs)} (exit {code})" + ("" if code == 0 else f"\n{text[-1500:]}")


FRAMES_PER_TEST = 4


def assertion_frames(trace_zip: Path, out_dir: Path, *, limit: int = FRAMES_PER_TEST) -> list[Path]:
    """The page as each ``expect`` in the test passed, from the trace's screencast.

    A screenshot at the end of a test shows its last state (often after cleanup); the
    frames at assertions show what the test checked. Up to ``limit`` distinct frames,
    spread over the test's assertions, as ``step-N.jpeg``.
    """
    import zipfile

    try:
        archive = zipfile.ZipFile(trace_zip)
    except (OSError, zipfile.BadZipFile):
        return []
    with archive:
        names = set(archive.namelist())
        expects: dict[str, float] = {}
        ends: dict[str, float] = {}
        starts: list[float] = []
        frames: list[tuple[float, str]] = []
        for name in sorted(n for n in names if n.endswith(".trace") and n != "test.trace"):
            for line in archive.read(name).decode(errors="replace").splitlines():
                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    continue
                kind = event.get("type")
                if kind == "before":
                    starts.append(float(event.get("startTime") or 0))
                if kind == "before" and event.get("method") == "expect":
                    expects[str(event.get("callId"))] = float(event.get("startTime") or 0)
                elif kind == "after" and event.get("callId"):
                    ends[str(event["callId"])] = float(event.get("endTime") or 0)
                elif kind == "screencast-frame" and event.get("sha1"):
                    frames.append((float(event.get("timestamp") or 0), str(event["sha1"])))
        frames.sort()
        starts.sort()
        picked: list[str] = []
        for call_id, _start in sorted(expects.items(), key=lambda item: item[1]):
            end = ends.get(call_id)
            if end is None:
                continue
            # Frames are painted a little after the DOM the assertion saw: take the latest frame
            # before the next action starts (within a second), else the last one before it passed.
            next_start = next((t for t in starts if t > end), end + 1000)
            settled = [sha for stamp, sha in frames if end <= stamp < min(next_start, end + 1000)]
            shown = settled or [sha for stamp, sha in frames if stamp <= end]
            if shown and (not picked or picked[-1] != shown[-1]):
                picked.append(shown[-1])
        if len(picked) > limit:
            step = (len(picked) - 1) / (limit - 1)
            picked = [picked[round(i * step)] for i in range(limit)]
        written = []
        for index, sha in enumerate(picked, start=1):
            member = f"resources/{sha}"
            if member in names:
                path = out_dir / f"step-{index}.jpeg"
                path.write_bytes(archive.read(member))
                written.append(path)
        return written


def screenshot_files(out_dir: Path) -> list[Path]:
    """At most ``MAX_SCREENSHOTS`` images: the change's own tests first, assertion frames before end shots."""
    if not Path(out_dir).is_dir():
        return []
    files = [*Path(out_dir).rglob("step-*.jpeg"), *Path(out_dir).rglob("*.png")]
    with_frames = {p.parent for p in files if p.suffix == ".jpeg"}
    # A test with assertion frames does not also need its end-of-test screenshot.
    files = [p for p in files if p.suffix == ".jpeg" or p.parent not in with_frames]
    files.sort(key=lambda p: ("setup" in p.parent.name.lower(), p.suffix != ".jpeg", str(p.parent), p.name))
    return files[:MAX_SCREENSHOTS]


def screenshot_name(path: Path) -> str:
    """``<test folder>[-step-N].<ext>``: Playwright names the folder after the spec and test title."""
    stem = re.sub(r"[^A-Za-z0-9._-]+", "-", path.parent.name).strip("-")[:90] or "screenshot"
    suffix = "" if path.stem.endswith("-1") and not path.stem.startswith("step-") else f"-{path.stem}"
    if path.stem.startswith("test-finished"):
        suffix = ""
    return f"{stem}{suffix}{path.suffix}"


# Last, so every helper above (and below main) is defined when this runs as a module.
if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
