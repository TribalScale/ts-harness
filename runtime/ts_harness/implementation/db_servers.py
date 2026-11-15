"""MySQL (MariaDB) and MongoDB for a coding job's tests, on loopback.

Postgres and Redis live in ``test_services``; these two follow the same shape:
``start_*`` returns ``(info, message)`` with ``info`` ``None`` on failure, ``reset_*``
empties the test database before an integration run.

MongoDB runs as a one-node replica set (``rs0``): Prisma and multi-document
transactions need one, and drivers connect to it like a standalone server. The
image ships no MongoDB shell, so the few admin commands go over the wire protocol
(OP_MSG) with a minimal BSON codec.
"""

from __future__ import annotations

import atexit
import os
import pwd
import shutil
import signal
import socket
import struct
import subprocess
import time
from pathlib import Path
from typing import Any

READY_SECONDS = 60


def _log_tail(path: Path, limit: int = 800) -> str:
    try:
        return path.read_text(errors="replace")[-limit:]
    except OSError:
        return ""


def _port_open(port: int) -> bool:
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=1):
            return True
    except OSError:
        return False


def _chown(path: Path, user: str) -> None:
    if os.geteuid() != 0:
        return
    entry = pwd.getpwnam(user)
    for root, dirs, files in os.walk(path):
        os.chown(root, entry.pw_uid, entry.pw_gid)
        for name in [*dirs, *files]:
            os.chown(os.path.join(root, name), entry.pw_uid, entry.pw_gid)


def _spawn(command: list[str], log_path: Path) -> subprocess.Popen:
    handle = open(log_path, "a", encoding="utf-8")  # noqa: SIM115 - owned by the child
    proc = subprocess.Popen(command, stdout=handle, stderr=subprocess.STDOUT, start_new_session=True)
    handle.close()
    return proc


def _stop_pid(pidfile: Path) -> None:
    try:
        os.kill(int(pidfile.read_text().split()[0]), signal.SIGTERM)
    except (OSError, ValueError, IndexError):
        pass


def _q(value: str) -> str:
    return "'" + value.replace("\\", "\\\\").replace("'", "''") + "'"


def _ident(value: str) -> str:
    return "`" + value.replace("`", "``") + "`"


# --- MySQL (MariaDB) ---------------------------------------------------------------------------------


def _mysql_bin(*names: str) -> str | None:
    return next((found for name in names if (found := shutil.which(name))), None)


def _mysql_sql(info: dict[str, Any], sql: str, *, runner=subprocess.run) -> subprocess.CompletedProcess:
    client = _mysql_bin("mariadb", "mysql") or "mariadb"
    return runner(
        [client, f"--socket={info['socket']}", "-uroot", "--batch", "-e", sql],
        capture_output=True, text=True, check=False, timeout=60,
    )


def start_mysql(root: Path, creds: Any, *, port: int, runner=subprocess.run) -> tuple[dict[str, Any] | None, str]:
    server = _mysql_bin("mariadbd", "mysqld")
    install = _mysql_bin("mariadb-install-db", "mysql_install_db")
    if not server or not install:
        return None, "mysql (MariaDB) is not installed in this worker"
    data, sock, log = root / "mysql", root / "mysql.sock", root / "mysql.log"
    info = {"port": port, "socket": str(sock), "pidfile": str(root / "mysql.pid"), "user": creds.user,
            "password": creds.password, "database": creds.name}
    root.mkdir(parents=True, exist_ok=True)
    if not (data / "mysql").is_dir():
        data.mkdir(parents=True, exist_ok=True)
        made = runner(
            [install, "--user=mysql", f"--datadir={data}", "--auth-root-authentication-method=normal",
             "--skip-test-db", "--skip-name-resolve"],
            capture_output=True, text=True, check=False, timeout=180,
        )
        if made.returncode != 0:
            return None, f"mysql install-db failed: {(made.stderr or made.stdout or '')[-800:]}"
    _chown(root, "mysql")
    proc = _spawn(
        [server, "--user=mysql", f"--datadir={data}", f"--socket={sock}", f"--port={port}", "--bind-address=127.0.0.1",
         "--skip-name-resolve", "--skip-log-bin", "--innodb-flush-log-at-trx-commit=0", f"--pid-file={info['pidfile']}",
         "--max-connections=300"],
        log,
    )
    deadline = time.monotonic() + READY_SECONDS
    while not (_port_open(port) and sock.exists()):
        if proc.poll() is not None or time.monotonic() > deadline:
            return None, f"mysql did not start: {_log_tail(log)}"
        time.sleep(0.3)
    user = _q(creds.user)
    made = _mysql_sql(
        info,
        f"CREATE USER IF NOT EXISTS {user}@'%' IDENTIFIED BY {_q(creds.password)}; "
        f"ALTER USER {user}@'%' IDENTIFIED BY {_q(creds.password)}; "
        f"GRANT ALL PRIVILEGES ON *.* TO {user}@'%' WITH GRANT OPTION; FLUSH PRIVILEGES;",
        runner=runner,
    )
    if made.returncode != 0:
        return None, f"mysql user setup failed: {(made.stderr or made.stdout or '')[-800:]}"
    problem = reset_mysql(info, runner=runner)
    if problem:
        return None, problem
    atexit.register(_stop_pid, Path(info["pidfile"]))
    return info, f"mysql (MariaDB) ready on 127.0.0.1:{port} (database {creds.name}, user {creds.user})"


def reset_mysql(info: dict[str, Any], *, runner=subprocess.run) -> str:
    name = _ident(str(info["database"]))
    result = _mysql_sql(
        info, f"DROP DATABASE IF EXISTS {name}; CREATE DATABASE {name} CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;",
        runner=runner,
    )
    return "" if result.returncode == 0 else f"mysql database reset failed: {(result.stderr or '')[-800:]}"


# --- MongoDB -----------------------------------------------------------------------------------------


def _bson_element(key: str, value: Any) -> bytes:
    name = key.encode() + b"\x00"
    if isinstance(value, bool):
        return b"\x08" + name + (b"\x01" if value else b"\x00")
    if isinstance(value, int):
        return (b"\x10" + name + struct.pack("<i", value)) if -(2**31) <= value < 2**31 else (b"\x12" + name + struct.pack("<q", value))
    if isinstance(value, float):
        return b"\x01" + name + struct.pack("<d", value)
    if isinstance(value, str):
        data = value.encode() + b"\x00"
        return b"\x02" + name + struct.pack("<i", len(data)) + data
    if isinstance(value, dict):
        return b"\x03" + name + bson_encode(value)
    if isinstance(value, (list, tuple)):
        return b"\x04" + name + bson_encode({str(i): item for i, item in enumerate(value)})
    if value is None:
        return b"\x0a" + name
    raise TypeError(f"unsupported BSON value: {type(value).__name__}")


def bson_encode(doc: dict[str, Any]) -> bytes:
    body = b"".join(_bson_element(key, value) for key, value in doc.items()) + b"\x00"
    return struct.pack("<i", len(body) + 4) + body


_FIXED_SIZES = {0x01: 8, 0x06: 0, 0x07: 12, 0x08: 1, 0x09: 8, 0x0A: 0, 0x10: 4, 0x11: 8, 0x12: 8, 0x13: 16, 0x7F: 0, 0xFF: 0}


def bson_decode(data: bytes, offset: int = 0) -> dict[str, Any]:
    """Enough of BSON to read command replies; values it does not need come back as ``None``."""
    end = offset + struct.unpack_from("<i", data, offset)[0] - 1
    pos, doc = offset + 4, {}
    while pos < end:
        kind = data[pos]
        name_end = data.index(b"\x00", pos + 1)
        key = data[pos + 1 : name_end].decode()
        pos = name_end + 1
        if kind in (0x02, 0x0D, 0x0E):  # string, JavaScript, symbol
            size = struct.unpack_from("<i", data, pos)[0]
            doc[key] = data[pos + 4 : pos + 3 + size].decode(errors="replace")
            pos += 4 + size
        elif kind in (0x03, 0x04):  # document, array
            size = struct.unpack_from("<i", data, pos)[0]
            sub = bson_decode(data, pos)
            doc[key] = list(sub.values()) if kind == 0x04 else sub
            pos += size
        elif kind == 0x05:  # binary: length, subtype, bytes
            doc[key] = None
            pos += 5 + struct.unpack_from("<i", data, pos)[0]
        elif kind == 0x0B:  # regex: two cstrings
            doc[key] = None
            pos = data.index(b"\x00", data.index(b"\x00", pos) + 1) + 1
        elif kind in _FIXED_SIZES:
            if kind == 0x01:
                doc[key] = struct.unpack_from("<d", data, pos)[0]
            elif kind == 0x08:
                doc[key] = data[pos] == 1
            elif kind == 0x10:
                doc[key] = struct.unpack_from("<i", data, pos)[0]
            elif kind == 0x12:
                doc[key] = struct.unpack_from("<q", data, pos)[0]
            else:
                doc[key] = None
            pos += _FIXED_SIZES[kind]
        else:
            raise ValueError(f"unsupported BSON type 0x{kind:02x}")
    return doc


def mongo_command(port: int, command: dict[str, Any], *, database: str = "admin", timeout: float = 10) -> dict[str, Any]:
    """Run one command with OP_MSG and return the reply document."""
    body = struct.pack("<I", 0) + b"\x00" + bson_encode({**command, "$db": database})
    header = struct.pack("<iiii", 16 + len(body), 1, 0, 2013)
    with socket.create_connection(("127.0.0.1", port), timeout=timeout) as sock:
        sock.sendall(header + body)
        raw = b""
        while len(raw) < 4:
            raw += sock.recv(4 - len(raw))
        total = struct.unpack("<i", raw)[0]
        while len(raw) < total:
            chunk = sock.recv(total - len(raw))
            if not chunk:
                break
            raw += chunk
    return bson_decode(raw, 16 + 4 + 1)


def start_mongodb(root: Path, database: str, *, port: int) -> tuple[dict[str, Any] | None, str]:
    server = shutil.which("mongod")
    if not server:
        return None, "mongodb is not installed in this worker"
    data, log = root / "mongodb", root / "mongodb.log"
    data.mkdir(parents=True, exist_ok=True)
    info = {"port": port, "pidfile": str(root / "mongod.pid"), "database": database}
    proc = _spawn(
        [server, "--dbpath", str(data), "--port", str(port), "--bind_ip", "127.0.0.1", "--replSet", "rs0",
         "--wiredTigerCacheSizeGB", "0.25", "--pidfilepath", info["pidfile"], "--quiet"],
        log,
    )
    deadline = time.monotonic() + READY_SECONDS
    while not _port_open(port):
        if proc.poll() is not None or time.monotonic() > deadline:
            return None, f"mongodb did not start: {_log_tail(log)}"
        time.sleep(0.3)
    try:
        status = mongo_command(port, {"replSetGetStatus": 1})
        if not status.get("ok"):
            reply = mongo_command(
                port, {"replSetInitiate": {"_id": "rs0", "members": [{"_id": 0, "host": f"127.0.0.1:{port}"}]}}
            )
            if not reply.get("ok"):
                return None, f"mongodb replica set init failed: {reply.get('errmsg')}"
        while not mongo_command(port, {"hello": 1}).get("isWritablePrimary"):
            if time.monotonic() > deadline:
                return None, "mongodb did not become primary"
            time.sleep(0.3)
    except (OSError, ValueError) as exc:
        return None, f"mongodb setup failed: {exc}"
    problem = reset_mongodb(info)
    if problem:
        return None, problem
    atexit.register(_stop_pid, Path(info["pidfile"]))
    return info, f"mongodb ready on 127.0.0.1:{port} (replica set rs0, database {database})"


def reset_mongodb(info: dict[str, Any]) -> str:
    try:
        reply = mongo_command(int(info["port"]), {"dropDatabase": 1}, database=str(info["database"]))
    except (OSError, ValueError) as exc:
        return f"mongodb database reset failed: {exc}"
    return "" if reply.get("ok") else f"mongodb database reset failed: {reply.get('errmsg')}"
