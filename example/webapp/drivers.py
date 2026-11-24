"""Infrastructure capabilities. These drivers store records, objects and delivery outcomes."""
import base64
import fcntl
import hashlib
import json
import logging
import os
import re
import secrets
import shutil
import smtplib
import ssl
import stat
import subprocess
import tempfile
import time
from contextlib import contextmanager
from email.message import EmailMessage
from pathlib import Path
from urllib.parse import urlencode, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

import jwt
import psycopg
from psycopg import sql
from psycopg.conninfo import conninfo_to_dict
from psycopg.rows import dict_row

from values import Problem, canonical, digest


LOG = logging.getLogger("application")
KEY = re.compile(r"[a-zA-Z0-9_-]{1,128}\Z")
BACKUP_LOCK = 4268310419

RESTORE_SCHEMA = "sc_storage_restore"
RESTORE_MARKER = ".restore.json"
DATABASE_CONTENTS = """
    SELECT EXISTS (
        SELECT FROM pg_namespace
        WHERE nspname NOT IN ('public', 'information_schema', 'sc_storage_restore')
          AND left(nspname, 3) <> 'pg_'
        UNION ALL
        SELECT FROM pg_depend
        WHERE refclassid = 'pg_namespace'::regclass
          AND refobjid = to_regnamespace('public')
        UNION ALL
        SELECT FROM pg_extension WHERE extname <> 'plpgsql'
        UNION ALL
        SELECT FROM pg_largeobject_metadata
    )
"""


def file_digest(path, copy_to=None):
    hashed = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            hashed.update(block)
            if copy_to is not None:
                copy_to.write(block)
    return hashed.hexdigest()


def sync_directory(path):
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def destination_conflict():
    return Problem(409, "restore-destination", "Restore requires an empty or matching restore destination.")


def invalid_backup():
    return Problem(422, "invalid-backup", "The backup checksums do not match.")


def incomplete_restore():
    return Problem(503, "restore-incomplete", "Restore is incomplete. Rerun it with the same backup.")


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *_args, **_kwargs):
        return None


def b64(data):
    return base64.urlsafe_b64encode(data).decode().rstrip("=")


class Identity:
    def __init__(self, settings):
        self.settings = settings
        self.metadata = None

    def endpoint(self, url):
        parsed = urlsplit(url)
        allowed = parsed.scheme == "https" or (
            self.settings.get("SC_INSECURE_LOCAL") == "1"
            and parsed.scheme == "http" and parsed.hostname in ("localhost", "127.0.0.1", "::1")
        )
        if not allowed or not parsed.netloc or parsed.username is not None or parsed.fragment:
            raise ValueError("untrusted identity endpoint")
        return url

    def request(self, url, data=None):
        payload = urlencode(data).encode() if data else None
        request = Request(self.endpoint(url), data=payload, headers={"Accept": "application/json"})
        with build_opener(NoRedirect()).open(request, timeout=5) as response:
            if response.status != 200:
                raise ValueError("unexpected identity response")
            return json.loads(response.read(65537))

    def discover(self):
        if self.metadata is None:
            issuer = self.settings["SC_OIDC_ISSUER"].rstrip("/")
            data = self.request(issuer + "/.well-known/openid-configuration")
            if data["issuer"] != issuer:
                raise ValueError("identity issuer mismatch")
            for key in ("authorization_endpoint", "token_endpoint", "jwks_uri"):
                self.endpoint(data[key])
            self.metadata = data
        return self.metadata

    def authorize(self, callback, state, verifier, nonce):
        try:
            metadata = self.discover()
            arguments = {
                "client_id": self.settings["SC_OIDC_CLIENT"], "response_type": "code",
                "scope": "openid email", "redirect_uri": callback, "state": state, "nonce": nonce,
                "code_challenge": b64(hashlib.sha256(verifier.encode()).digest()),
                "code_challenge_method": "S256",
            }
            url = metadata["authorization_endpoint"]
            return url + ("&" if "?" in url else "?") + urlencode(arguments)
        except (KeyError, OSError, ValueError) as exc:
            raise Problem(503, "identity-unavailable", "The identity provider is unavailable.") from exc

    def exchange(self, callback, code, verifier, nonce):
        try:
            metadata = self.discover()
            client = self.settings["SC_OIDC_CLIENT"]
            args = {
                "grant_type": "authorization_code", "client_id": client, "code": code,
                "redirect_uri": callback, "code_verifier": verifier,
            }
            if self.settings.get("SC_OIDC_SECRET"):
                args["client_secret"] = self.settings["SC_OIDC_SECRET"]
            token = self.request(metadata["token_endpoint"], args)["id_token"]
            header = jwt.get_unverified_header(token)
            matches = [
                jwk for jwk in self.request(metadata["jwks_uri"])["keys"]
                if jwk.get("kid") == header.get("kid") and jwk.get("use", "sig") == "sig"
                and jwk.get("alg", "RS256") == "RS256" and jwk.get("kty") == "RSA"
            ]
            if header.get("alg") != "RS256" or len(matches) != 1:
                raise ValueError("unexpected signature key")
            key = jwt.PyJWK.from_dict(matches[0], algorithm="RS256").key
            claims = jwt.decode(token, key=key, algorithms=["RS256"], audience=client,
                                issuer=metadata["issuer"], options={"require": ["exp", "iat", "sub", "nonce"]})
            if not secrets.compare_digest(claims["nonce"], nonce) or claims.get("azp", client) != client:
                raise ValueError("invalid identity binding")
            if isinstance(claims["aud"], list) and len(claims["aud"]) > 1 and claims.get("azp") != client:
                raise ValueError("unbound audience")
            return {"issuer": claims["iss"], "subject": claims["sub"], "email": claims.get("email"),
                    "verified": claims.get("email_verified") is True}
        except (KeyError, OSError, TypeError, ValueError, jwt.PyJWTError) as exc:
            raise Problem(401, "invalid-identity", "The identity provider did not verify the sign-in.") from exc


class BlobStore:
    def __init__(self, root):
        self.root = Path(root)

    def path(self, key):
        if not isinstance(key, str) or not KEY.fullmatch(key):
            raise ValueError("invalid storage key")
        return self.root / key

    def put(self, key, data):
        self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
        final = self.path(key)
        with tempfile.NamedTemporaryFile(dir=self.root, prefix=".blob-", delete=False) as file:
            stage = Path(file.name)
            try:
                file.write(data)
                file.flush()
                os.fsync(file.fileno())
                try:
                    os.link(stage, final)
                except FileExistsError:
                    if final.read_bytes() != data:
                        raise Problem(409, "object-conflict", "The stored object has different content.")
                self.sync()
            finally:
                stage.unlink(missing_ok=True)
        return {"length": len(data), "digest": digest(data)}

    def get(self, key):
        try:
            return self.path(key).read_bytes()
        except FileNotFoundError as exc:
            raise Problem(404, "object-missing", "The stored file is missing.") from exc

    def check(self, key):
        try:
            data = self.path(key).read_bytes()
        except FileNotFoundError:
            return None
        return {"length": len(data), "digest": digest(data)}

    def aged(self):
        cutoff = time.time() - 86400
        if not self.root.is_dir():
            return []
        old = []
        for path in self.root.iterdir():
            if KEY.fullmatch(path.name):
                try:
                    if path.stat().st_mtime < cutoff:
                        old.append(path.name)
                except FileNotFoundError:
                    pass
        # Rotate the sampled window when the store contains older referenced versions.
        old.sort()
        if len(old) <= 20:
            return old
        index = (int(time.time()) // 5 * 20) % len(old)
        return (old[index:] + old[:index])[:20]

    def delete(self, key):
        self.path(key).unlink(missing_ok=True)
        self.sync()

    def sync(self):
        if self.root.is_dir():
            sync_directory(self.root)


class RestoreObjects:
    """Own a durable staging directory without writing into a live object store."""
    def __init__(self, root):
        location = root.absolute()
        if not location.name:
            raise destination_conflict()
        self.root = location.parent.resolve() / location.name
        self.identity = digest(str(self.root))
        name = ".sc-restore-" + digest(self.root.name)
        self.stage = self.root.parent / name
        self.lock_path = self.root.parent / (name + ".lock")

    @contextmanager
    def lock(self):
        directory = self.root.parent
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        fd = os.open(self.lock_path, os.O_CREAT | os.O_NOFOLLOW | os.O_RDWR | os.O_NONBLOCK, 0o600)
        try:
            if not stat.S_ISREG(os.fstat(fd).st_mode):
                raise destination_conflict()
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise Problem(409, "restore-busy", "Another restore owns the object destination.") from exc
            yield
        finally:
            os.close(fd)

    @staticmethod
    def entries(path):
        if path.is_symlink() or (path.exists() and not path.is_dir()):
            raise destination_conflict()
        return {entry.name: entry for entry in path.iterdir()} if path.exists() else {}

    def require_empty(self):
        if self.entries(self.root) or self.stage.exists() or self.stage.is_symlink():
            raise destination_conflict()

    @staticmethod
    def require_owner(entries, ownership):
        marker = entries.get(RESTORE_MARKER)
        if marker is None or marker.is_symlink() or not marker.is_file():
            raise destination_conflict()
        try:
            if marker.stat().st_size > 1024 or json.loads(marker.read_text()) != ownership:
                raise destination_conflict()
        except (ValueError, OSError) as exc:
            raise destination_conflict() from exc

    def new_stage(self, ownership):
        scratch = Path(tempfile.mkdtemp(dir=self.stage.parent, prefix=self.stage.name + "-"))
        try:
            with (scratch / RESTORE_MARKER).open("x") as marker:
                marker.write(canonical(ownership))
                marker.flush()
                os.fsync(marker.fileno())
            sync_directory(scratch)
            os.rename(scratch, self.stage)
            sync_directory(self.stage.parent)
        finally:
            if scratch.exists():
                shutil.rmtree(scratch)

    @staticmethod
    def verify(entries, expected, complete):
        names = entries.keys() - {RESTORE_MARKER}
        if names - expected.keys() or (complete and names != expected.keys()):
            raise destination_conflict()
        for name in names:
            item = entries[name]
            if item.is_symlink() or not item.is_file() or file_digest(item) != expected[name]:
                raise destination_conflict()

    def publish(self, objects, expected, ownership):
        visible = self.entries(self.root)
        if visible:
            self.require_owner(visible, ownership)
            self.verify(visible, expected, complete=True)
            sync_directory(self.root.parent)
            return

        if not self.stage.exists() and not self.stage.is_symlink():
            self.new_stage(ownership)
        staged = self.entries(self.stage)
        self.require_owner(staged, ownership)
        for name, item in list(staged.items()):
            # BlobStore creates this temporary link inside the owned staging directory.
            if name.startswith(".blob-") and item.is_file() and not item.is_symlink():
                item.unlink()
                del staged[name]
        self.verify(staged, expected, complete=False)
        store = BlobStore(self.stage)
        for name, source in objects.items():
            if name not in staged:
                data = source.read_bytes()
                if digest(data) != expected[name]:
                    raise invalid_backup()
                store.put(name, data)
        sync_directory(self.stage)
        os.replace(self.stage, self.root)
        sync_directory(self.root.parent)


def send_mail(settings, envelope):
    message = EmailMessage()
    message["From"] = settings["SC_SMTP_FROM"]
    message["To"] = envelope["to"]
    message["Message-ID"] = f'<{envelope["id"]}@{settings.get("SC_SMTP_ID_HOST", "approval.local")}>'
    message["Subject"] = envelope["subject"]
    message.set_content(envelope["body"])
    data_started = False
    try:
        with smtplib.SMTP(settings["SC_SMTP_HOST"], int(settings.get("SC_SMTP_PORT", "587")),
                          timeout=5) as client:
            client.ehlo()
            if settings.get("SC_SMTP_PLAINTEXT_LOCAL") != "1":
                client.starttls(context=ssl.create_default_context())
                client.ehlo()
            if settings.get("SC_SMTP_USER"):
                client.login(settings["SC_SMTP_USER"], settings["SC_SMTP_PASSWORD"])
            data_started = True
            refused = client.send_message(message)
            return "failed" if refused else "accepted"
    except smtplib.SMTPResponseException:
        return "failed"
    except (OSError, smtplib.SMTPException):
        return "unknown" if data_started else "failed"


class Context:
    def __init__(self, settings, root, policy=None):
        self.settings = settings
        self.root = root
        self.policy = policy
        self.connection = None
        self.transaction = None
        self.blob = BlobStore(settings.get("SC_BLOBS", "./blobs"))
        self.identity = Identity(settings)
        self.operations = {
            "sql": self.sql, "begin": self.begin, "commit": self.commit,
            "token": lambda: secrets.token_urlsafe(32),
            "now": lambda: int(time.time()), "setting": self.setting,
            "authorize-id": self.identity.authorize, "exchange-id": self.identity.exchange,
            "blob-put": self.blob.put, "blob-check": self.blob.check,
            "blob-get": self.blob.get, "blob-delete": self.blob.delete, "blob-aged": self.blob.aged,
            "compile-rule": self.compile_rule, "mail": self.mail,
            "backup-files": self.backup, "restore-files": self.restore,
        }

    def effect(self, name, *args):
        return self.operations[name](*args)

    def setting(self, name):
        if name not in {"SC_ORIGIN", "SC_INSECURE_LOCAL", "SC_SMTP_FROM"}:
            raise ValueError("undeclared setting")
        return self.settings.get(name)

    def connect(self):
        if self.connection is None:
            self.connection = psycopg.connect(self.settings["SC_DATABASE"], autocommit=True,
                                              connect_timeout=5, row_factory=dict_row)
            self.connection.execute("SET statement_timeout='8s'; SET lock_timeout='5s'")
        return self.connection

    def begin(self):
        if self.transaction is not None:
            raise RuntimeError("nested transaction")
        self.transaction = self.connect().transaction()
        self.transaction.__enter__()
        # Use a new PostgreSQL statement snapshot after acquiring the shared lock.
        statements = self.connect().execute(f"""
            SELECT pg_advisory_xact_lock_shared({BACKUP_LOCK});
            SELECT EXISTS (
                SELECT FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
                WHERE n.nspname = 'sc_storage_restore' AND c.relname = 'pending'
            ) AS restoring
        """)
        statements.nextset()
        if statements.fetchone()["restoring"]:
            raise incomplete_restore()

    def commit(self):
        if self.transaction is None:
            raise RuntimeError("no open transaction")
        transaction, self.transaction = self.transaction, None
        transaction.__exit__(None, None, None)

    def __enter__(self):
        return self

    def __exit__(self, exc_type, value, traceback):
        try:
            if self.transaction is not None:
                if exc_type is None:
                    self.commit()
                else:
                    self.transaction.__exit__(exc_type, value, traceback)
                    self.transaction = None
        finally:
            if self.connection is not None:
                self.connection.close()

    def sql(self, statement, args):
        if self.transaction is None:
            raise RuntimeError("SQL requires an explicit transaction")
        cursor = self.connect().execute(statement, args if args else None)
        return cursor.fetchall() if cursor.description else []

    def compile_rule(self, source):
        if self.policy:
            command = self.policy
        else:
            command = [str(self.root.parent / "policy")]
        try:
            process = subprocess.run(command, input=canonical(source), text=True,
                                     capture_output=True, timeout=10, env={
                                         "PATH": os.environ["PATH"], "PYTHONDONTWRITEBYTECODE": "1"
                                     })
            if process.returncode == 2:
                raise Problem(422, "invalid-workflow", "The workflow definition is invalid.")
            if process.returncode or len(process.stdout) > 8192:
                raise ValueError("tenant compiler failed")
            return json.loads(process.stdout)
        except (OSError, ValueError, subprocess.TimeoutExpired) as exc:
            raise Problem(503, "compiler-unavailable", "The workflow compiler is unavailable.") from exc

    def mail(self, envelope):
        host = self.settings.get("SC_SMTP_HOST")
        if not host or not self.settings.get("SC_SMTP_FROM"):
            return "failed"
        if self.settings.get("SC_SMTP_PLAINTEXT_LOCAL") == "1" and host not in {"localhost", "127.0.0.1", "::1"}:
            return "failed"
        return send_mail(self.settings, envelope)

    def database_environment(self):
        options = conninfo_to_dict(self.settings["SC_DATABASE"])
        env = {key: value for key, value in os.environ.items() if not key.startswith("PG")}
        for key, val in options.items():
            if key not in {"host", "hostaddr", "port", "dbname", "user", "password", "sslmode", "sslrootcert"}:
                raise ValueError("unsupported database option for backup")
            env[{"dbname": "PGDATABASE"}.get(key, "PG" + key.upper())] = val
        return env

    @contextmanager
    def exclusive_snapshot(self):
        connection = self.connect()
        if self.transaction:
            raise RuntimeError("snapshot must run outside a request transaction")
        connection.execute("SELECT pg_advisory_lock(%s)", [BACKUP_LOCK])
        try:
            yield connection
        finally:
            connection.execute("SELECT pg_advisory_unlock(%s)", [BACKUP_LOCK])

    def backup(self, path):
        target = Path(path)
        if target.exists():
            raise Problem(409, "backup-exists", "The backup path already exists.")
        with self.exclusive_snapshot() as connection, tempfile.TemporaryDirectory(dir=target.parent) as stage:
            receipt = self.restore_receipt(connection)
            if receipt and not receipt["completed"]:
                raise incomplete_restore()
            staged = Path(stage) / "snapshot"
            staged.mkdir()
            subprocess.run(["pg_dump", "--format=custom", f"--exclude-schema={RESTORE_SCHEMA}",
                            "--file", str(staged / "postgres.dump")],
                           env=self.database_environment(), check=True, timeout=120)
            objects = staged / "objects"
            objects.mkdir()
            for source in self.blob.root.iterdir() if self.blob.root.exists() else []:
                if KEY.fullmatch(source.name) and source.is_file():
                    shutil.copyfile(source, objects / source.name)
            files = [staged / "postgres.dump", *sorted(objects.iterdir())]
            (staged / "checksums.json").write_text(canonical({
                str(item.relative_to(staged)): file_digest(item) for item in files
            }))
            staged.rename(target)
        return {"status": "backed-up", "path": str(target)}

    @staticmethod
    def read_backup(source):
        try:
            checksums = json.loads((source / "checksums.json").read_text())
            objects = {item.name: item for item in sorted((source / "objects").iterdir())}
            files = [source / "postgres.dump", *objects.values()]
            if (
                any(not KEY.fullmatch(name) for name in objects)
                or any(item.is_symlink() or not item.is_file() for item in files)
                or checksums != {str(item.relative_to(source)): file_digest(item) for item in files}
            ):
                raise invalid_backup()
        except (ValueError, OSError) as exc:
            raise invalid_backup() from exc
        return checksums, objects

    @staticmethod
    def restore_receipt(connection):
        row = connection.execute("""
            SELECT to_regclass('sc_storage_restore.pending') AS pending,
                   to_regclass('sc_storage_restore.receipt') AS receipt
        """).fetchone()
        if row["pending"] and row["receipt"]:
            raise destination_conflict()
        relation = "pending" if row["pending"] else "receipt"
        if row[relation] is None:
            return None
        values = connection.execute(sql.SQL(
            "SELECT operation, snapshot, object_store FROM sc_storage_restore.{} WHERE singleton"
        ).format(sql.Identifier(relation))).fetchone()
        if values is None:
            raise destination_conflict()
        return {**values, "completed": relation == "receipt"}

    @staticmethod
    def reserve_restore(connection, snapshot, object_store):
        if connection.execute(
            "SELECT to_regnamespace('sc_storage_restore') AS namespace"
        ).fetchone()["namespace"] is not None:
            raise destination_conflict()
        receipt = {"operation": secrets.token_hex(16), "snapshot": snapshot,
                   "object_store": object_store, "completed": False}
        with connection.transaction():
            connection.execute("""
                CREATE SCHEMA sc_storage_restore;
                CREATE TABLE sc_storage_restore.pending (
                    singleton boolean PRIMARY KEY DEFAULT true CHECK(singleton),
                    operation text NOT NULL,
                    snapshot text NOT NULL,
                    object_store text NOT NULL
                )
            """)
            connection.execute(
                "INSERT INTO sc_storage_restore.pending (operation, snapshot, object_store) VALUES (%s, %s, %s)",
                [receipt["operation"], snapshot, object_store],
            )
        return receipt

    def install_dump(self, archive, expected, receipt):
        env = {**self.database_environment(), "PGCONNECT_TIMEOUT": "5"}
        match = sql.SQL("singleton AND operation={} AND snapshot={} AND object_store={}").format(
            sql.Literal(receipt["operation"]), sql.Literal(receipt["snapshot"]), sql.Literal(receipt["object_store"]),
        )
        # Escape the complete PL/pgSQL body as a SQL literal, including journal values.
        guard = sql.SQL("DO {}").format(sql.Literal(sql.SQL("""
            BEGIN
                IF ({contents}) OR NOT EXISTS (SELECT FROM sc_storage_restore.pending WHERE {match}) THEN
                    RAISE EXCEPTION 'restore destination changed';
                END IF;
            END
        """).format(contents=sql.SQL(DATABASE_CONTENTS), match=match).as_string())).as_string()
        with tempfile.TemporaryFile() as dump, tempfile.TemporaryFile() as statements:
            if file_digest(archive, dump) != expected:
                raise invalid_backup()
            dump.seek(0)
            subprocess.run(["pg_restore", "--exit-on-error", "--file=-"], stdin=dump, stdout=statements,
                           env=env, check=True, timeout=120)
            statements.seek(0)
            subprocess.run([
                "psql", "--no-psqlrc", "--single-transaction", "--set=ON_ERROR_STOP=1",
                "-c", f"SET LOCAL lock_timeout='5s'; SELECT pg_advisory_xact_lock({BACKUP_LOCK});",
                "-c", guard, "-f", "-", "-c", "ALTER TABLE sc_storage_restore.pending RENAME TO receipt",
            ], stdin=statements, stdout=subprocess.DEVNULL, env=env, check=True, timeout=120)

    def restore(self, path):
        source = Path(path)
        checksums, objects = self.read_backup(source)
        snapshot = digest(checksums)
        destination = RestoreObjects(self.blob.root)
        if source.resolve() in (destination.root.resolve(), *destination.root.resolve().parents):
            raise destination_conflict()
        with destination.lock():
            with self.exclusive_snapshot() as connection:
                receipt = self.restore_receipt(connection)
                if receipt and (receipt["snapshot"] != snapshot or receipt["object_store"] != destination.identity):
                    raise destination_conflict()
                if receipt and receipt["completed"]:
                    destination.require_owner(destination.entries(destination.root),
                                              {"operation": receipt["operation"], "snapshot": snapshot})
                    return {"status": "restored", "path": str(source)}
                if connection.execute(DATABASE_CONTENTS + " AS populated").fetchone()["populated"]:
                    raise destination_conflict()
                if receipt is None:
                    destination.require_empty()
                    receipt = self.reserve_restore(connection, snapshot, destination.identity)
                ownership = {"operation": receipt["operation"], "snapshot": snapshot}
                expected = {name: checksums["objects/" + name] for name in objects}
                try:
                    destination.publish(objects, expected, ownership)
                except OSError as exc:
                    raise incomplete_restore() from exc
            # The installer acquires the database lock in its own transaction, which can
            # finish safely even if this parent exits. The object lock covers the handoff.
            try:
                self.install_dump(source / "postgres.dump", checksums["postgres.dump"], receipt)
            except (OSError, subprocess.SubprocessError) as exc:
                with self.exclusive_snapshot() as connection:
                    observed = self.restore_receipt(connection)
                    if observed != {**receipt, "completed": True}:
                        raise incomplete_restore() from exc
        return {"status": "restored", "path": str(source)}
