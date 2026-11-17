"""MySQL (MariaDB) and MongoDB in a coding job: detection, env rewriting, and real servers where installed."""

from __future__ import annotations

import json
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from ts_harness.implementation import db_servers
from ts_harness.implementation import test_services as ts


def _write(path: Path, text: str = "") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


class DetectionTests(unittest.TestCase):
    def test_node_python_prisma_and_compose(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            _write(repo / "package.json", json.dumps({"dependencies": {"mysql2": "3"}}))
            self.assertEqual(ts.detect_services(repo), (["mysql"], []))
            _write(repo / "prisma" / "schema.prisma", 'datasource db {\n  provider = "mongodb"\n}\n')
            self.assertEqual(ts.detect_services(repo)[0], ["mongodb", "mysql"])
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            _write(repo / "requirements.txt", "Django==5.2\nmysqlclient==2.2\npymongo==4.10\n")
            _write(repo / "docker-compose.yml", "services:\n  search:\n    image: elasticsearch:8.15.0\n")
            self.assertEqual(ts.detect_services(repo), (["mongodb", "mysql"], ["elasticsearch"]))


class EnvTests(unittest.TestCase):
    TEMPLATE = {
        "DATABASE_URL": "mysql://shop:s3cret@mysql:3306/shop",
        "MYSQL_ROOT_PASSWORD": "root",
        "MONGODB_URI": "mongodb://admin:pw@mongo:27017/events?authSource=admin&retryWrites=true",
        "DB_HOST": "mysql",
        "DB_PORT": "3306",
        "MONGO_HOST": "mongo",
    }

    def test_mysql_credentials_from_url_and_root_password(self):
        creds = ts.db_credentials(self.TEMPLATE, "mysql")
        self.assertEqual((creds.user, creds.password, creds.name), ("shop", "s3cret", "shop"))
        root = ts.db_credentials({"MYSQL_ROOT_PASSWORD": "rootpw", "MYSQL_DATABASE": "app"}, "mysql")
        self.assertEqual((root.user, root.password, root.name), ("root", "rootpw", "app"))

    def test_urls_hosts_and_ports_point_at_the_job(self):
        services = {"mysql": {"port": 3307}, "mongodb": {"port": 27018, "database": "events"}}
        changes = ts.rewrite_env(self.TEMPLATE, services, {"mysql", "mongo"})
        self.assertEqual(changes["DATABASE_URL"], "mysql://shop:s3cret@127.0.0.1:3307/shop")
        self.assertEqual(changes["MONGODB_URI"], "mongodb://127.0.0.1:27018/events?retryWrites=true")
        self.assertEqual((changes["DB_HOST"], changes["DB_PORT"], changes["MONGO_HOST"]), ("127.0.0.1", "3307", "127.0.0.1"))
        self.assertEqual(ts.mongo_database(self.TEMPLATE), "events")

    def test_atlas_and_remote_urls_are_left_alone(self):
        remote = {"MONGODB_URI": "mongodb://u:p@db.example.com:27017/app", "X": "mongodb+srv://c.example.net/app"}
        self.assertEqual(ts.rewrite_env(remote, {"mongodb": {"port": 27018, "database": "app"}}, set()), {})

    def test_service_env_for_mysql_and_mongo(self):
        mysql = ts.DbCredentials(user="shop", password="p@ss", name="shop")
        env = ts._service_env(
            {"mysql": {"port": 3307}, "mongodb": {"port": 27018, "database": "events"}}, ts.DbCredentials(), mysql
        )
        self.assertEqual(env["DATABASE_URL"], "mysql://shop:p%40ss@127.0.0.1:3307/shop")
        self.assertEqual(env["MONGODB_URI"], "mongodb://127.0.0.1:27018/events")


class BsonTests(unittest.TestCase):
    def test_round_trip(self):
        doc = {"replSetInitiate": {"_id": "rs0", "members": [{"_id": 0, "host": "127.0.0.1:27018"}]}, "ok": 1.0, "big": 2**40}
        self.assertEqual(db_servers.bson_decode(db_servers.bson_encode(doc)), {**doc, "replSetInitiate": {"_id": "rs0", "members": [{"_id": 0, "host": "127.0.0.1:27018"}]}})


@unittest.skipUnless(shutil.which("mariadbd") and shutil.which("mariadb-install-db"), "MariaDB is not installed")
class RealMysqlTests(unittest.TestCase):
    def test_start_create_user_and_reset(self):
        with tempfile.TemporaryDirectory() as tmp:
            Path(tmp).chmod(0o755)  # the mysql user must reach its data directory
            creds = ts.DbCredentials(user="shop", password="s3cret", name="shop")
            info, message = db_servers.start_mysql(Path(tmp), creds, port=ts.free_port(3306))
            try:
                self.assertIsNotNone(info, message)
                query = subprocess.run(
                    ["mariadb", "-h127.0.0.1", f"-P{info['port']}", "-ushop", "-ps3cret", "shop", "-e", "SELECT 1"],
                    capture_output=True, text=True, check=False,
                )
                self.assertEqual(query.returncode, 0, query.stderr)
                self.assertEqual(db_servers.reset_mysql(info), "")
            finally:
                db_servers._stop_pid(Path(tmp) / "mysqlrun" / "mysql.pid")


@unittest.skipUnless(shutil.which("mongod"), "MongoDB is not installed")
class RealMongoTests(unittest.TestCase):
    def test_replica_set_primary_and_reset(self):
        with tempfile.TemporaryDirectory() as tmp:
            info, message = db_servers.start_mongodb(Path(tmp), "events", port=ts.free_port(27017))
            try:
                self.assertIsNotNone(info, message)
                self.assertTrue(db_servers.mongo_command(info["port"], {"hello": 1})["isWritablePrimary"])
                self.assertEqual(db_servers.reset_mongodb(info), "")
            finally:
                db_servers._stop_pid(Path(tmp) / "mongod.pid")


if __name__ == "__main__":
    unittest.main()
