#!/usr/bin/python3
"""Authenticated idempotent label printing bridge.

The service accepts one PDF per idempotency key over TLS with a bearer
token, normalizes the document to a single shipping label, and hands it to
a CUPS queue. Receipts live in a local SQLite database: replaying a key
never prints twice, and an interrupted submission is reported as unknown
instead of being retried.
"""
import hashlib
import hmac
import io
import json
import os
from dataclasses import dataclass
from pathlib import Path
import re
import resource
import sqlite3
import ssl
import subprocess
import sys
import tempfile
from http.server import BaseHTTPRequestHandler, HTTPServer

from pypdf import PdfReader, PdfWriter, Transformation

MAX_BYTES = 4 * 1024 * 1024
KEY = re.compile(r"[0-9a-f]{8}-(?:[0-9a-f]{4}-){3}[0-9a-f]{12}\Z")


@dataclass(frozen=True)
class Config:
    """Runtime configuration read from the environment.

    Every value has a safe default; set the LABEL_* variables to adapt the
    bridge to a specific installation.
    """

    listen: str = "127.0.0.1"
    port: int = 8631
    queue: str = "PC43t"
    media: str = "Label100x150"
    job_prefix: str = "labelprinter-"
    token_file: Path = Path("/etc/labelprinter/token")
    cert_file: Path = Path("/etc/labelprinter/server.crt")
    key_file: Path = Path("/etc/labelprinter/server.key")
    state_dir: Path = Path("/var/lib/labelprinter")

    @classmethod
    def from_environment(cls):
        try:
            port = int(os.environ.get("LABEL_PORT", "8631"))
        except ValueError as error:
            raise ValueError("LABEL_PORT must be a number") from error
        if not 0 < port < 65536:
            raise ValueError("LABEL_PORT must be between 1 and 65535")
        return cls(
            listen=os.environ.get("LABEL_LISTEN", cls.listen),
            port=port,
            queue=os.environ.get("LABEL_QUEUE", cls.queue),
            media=os.environ.get("LABEL_MEDIA", cls.media),
            job_prefix=os.environ.get("LABEL_JOB_PREFIX", cls.job_prefix),
            token_file=Path(os.environ.get("LABEL_TOKEN_FILE", cls.token_file)),
            cert_file=Path(os.environ.get("LABEL_CERT_FILE", cls.cert_file)),
            key_file=Path(os.environ.get("LABEL_KEY_FILE", cls.key_file)),
            state_dir=Path(os.environ.get("LABEL_STATE_DIR", cls.state_dir)),
        )


def normalize_pdf(data):
    """Validate and normalize a label PDF in a disposable worker.

    Compressed PDF streams can expand far beyond the input limit, so the
    parsing runs in a child process with memory, CPU and output limits
    before any CUPS submission is recorded.
    """
    if not data.startswith(b"%PDF-") or len(data) > MAX_BYTES:
        raise ValueError("Only a PDF shipping label is supported")
    try:
        with tempfile.TemporaryFile() as output:
            result = subprocess.run(
                [sys.executable, str(Path(__file__).resolve()), "--normalize-pdf"],
                input=data, stdout=output, stderr=subprocess.DEVNULL, timeout=8,
            )
            if result.returncode != 0:
                raise ValueError("PDF normalization failed")
            output.seek(0)
            document = output.read(MAX_BYTES + 1)
            if not document.startswith(b"%PDF-") or len(document) > MAX_BYTES:
                raise ValueError("Normalized label exceeds the output limit")
            return document
    except (OSError, subprocess.TimeoutExpired, ValueError) as error:
        raise ValueError("Invalid or unsupported single-label PDF") from error


def _normalize_pdf(data):
    try:
        reader = PdfReader(io.BytesIO(data), strict=True)
        if reader.is_encrypted or len(reader.pages) != 1:
            raise ValueError("Exactly one unencrypted label is required")
        page = reader.pages[0]
        page.transfer_rotation_to_content()
        width, height = float(page.mediabox.width), float(page.mediabox.height)
        if not (30 <= min(width, height) * 25.4 / 72 and max(width, height) * 25.4 / 72 <= 160):
            raise ValueError("Choose a single label PDF, not an A4 sheet")
        if width > height:
            page.rotate(90)
            page.transfer_rotation_to_content()
            width, height = float(page.mediabox.width), float(page.mediabox.height)
        target_w, target_h = 100 * 72 / 25.4, 150 * 72 / 25.4
        scale = min(1, target_w / width, target_h / height)
        transform = Transformation().translate(-float(page.mediabox.left), -float(page.mediabox.bottom)).scale(scale).translate((target_w - width * scale) / 2, (target_h - height * scale) / 2)
        writer = PdfWriter()
        target = writer.add_blank_page(width=target_w, height=target_h)
        target.merge_transformed_page(page, transform)
        output = io.BytesIO()
        writer.write(output)
        return output.getvalue()
    except Exception as error:
        raise ValueError("Invalid or unsupported single-label PDF") from error


def _limit_worker_resources():
    limits = ((resource.RLIMIT_AS, 128 * 1024 * 1024),
              (resource.RLIMIT_CPU, 6),
              (resource.RLIMIT_FSIZE, MAX_BYTES))
    for kind, limit in limits:
        try:
            _, hard = resource.getrlimit(kind)
            if hard != resource.RLIM_INFINITY:
                limit = min(limit, hard)
            resource.setrlimit(kind, (limit, limit))
        except (ValueError, OSError):
            pass


def _normalization_worker():
    _limit_worker_resources()
    data = sys.stdin.buffer.read(MAX_BYTES + 1)
    if not data.startswith(b"%PDF-") or len(data) > MAX_BYTES:
        return 1
    try:
        document = _normalize_pdf(data)
        if len(document) > MAX_BYTES:
            return 1
        sys.stdout.buffer.write(document)
        sys.stdout.buffer.flush()
        return 0
    except Exception:
        return 1


class Spooler:
    """Durable idempotency receipts around a CUPS submit call.

    ``submit`` receives the request key and a file path and returns a CUPS
    job ID. States are ``submitting``, ``submitted`` and ``unknown``. A
    process crash across the CUPS boundary cannot safely be retried, so
    interrupted submissions become ``unknown`` on restart and are never
    submitted again for the same key.
    """

    def __init__(self, directory, submit):
        self.directory = directory
        self.submit = submit
        self.db = sqlite3.connect(Path(directory) / "receipts.sqlite3")
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=FULL")
        self.db.execute("CREATE TABLE IF NOT EXISTS receipts (id TEXT PRIMARY KEY, digest TEXT NOT NULL, state TEXT NOT NULL, job_id INTEGER NOT NULL DEFAULT 0)")
        self.db.execute("UPDATE receipts SET state='unknown' WHERE state='submitting'")
        self.db.commit()

    def print(self, key, data):
        """Print one document for an idempotency key and return its state."""
        if not KEY.fullmatch(key):
            raise ValueError("Invalid print request ID")
        digest = hashlib.sha256(data).hexdigest()
        row = self.db.execute("SELECT digest,state,job_id FROM receipts WHERE id=?", (key,)).fetchone()
        if row:
            if not hmac.compare_digest(row[0], digest):
                raise ValueError("Print request ID already belongs to another document")
            return {"state": row[1], "job_id": row[2]}
        document = normalize_pdf(data)
        self.db.execute("INSERT INTO receipts (id,digest,state) VALUES (?,?,'submitting')", (key, digest))
        self.db.commit()
        state, job_id = "unknown", 0
        try:
            with tempfile.NamedTemporaryFile(dir=self.directory, suffix=".pdf") as file:
                file.write(document)
                file.flush()
                job_id = self.submit(key, file.name)
                state = "submitted"
        except Exception:
            pass
        self.db.execute("UPDATE receipts SET state=?,job_id=? WHERE id=?", (state, job_id, key))
        self.db.commit()
        return {"state": state, "job_id": job_id}


def cups_submit(key, path, config):
    """Submit one file to the CUPS queue and return the job ID."""
    result = subprocess.run(["lp", "-d", config.queue, "-n", "1", "-t", config.job_prefix + key,
                             "-o", "media=" + config.media, "-o", "job-sheets=none", path],
                            check=True, capture_output=True, text=True, timeout=30,
                            env={**os.environ, "LC_ALL": "C"})
    match = re.search(r"request id is " + re.escape(config.queue) + r"-(\d+)", result.stdout)
    if not match:
        raise ValueError("Missing CUPS receipt")
    return int(match[1])


class Handler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        pass

    def setup(self):
        super().setup()
        self.connection.settimeout(35)

    def reply(self, status, body):
        encoded = json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(encoded)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(encoded)

    def do_POST(self):
        if not hmac.compare_digest(self.headers.get("Authorization", ""), "Bearer " + self.server.token):
            return self.reply(403, {"error": "unauthorized"})
        key = self.path.removeprefix("/jobs/")
        if not self.path.startswith("/jobs/") or not KEY.fullmatch(key):
            return self.reply(404, {"error": "not found"})
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if not 0 < length <= MAX_BYTES or self.headers.get("Content-Type") != "application/pdf":
                return self.reply(422, {"error": "single label PDF required"})
            data = self.rfile.read(length)
            if len(data) != length:
                return self.reply(400, {"error": "incomplete document"})
            result = self.server.spooler.print(key, data)
        except ValueError:
            return self.reply(422, {"error": "unsupported label or conflicting request"})
        self.reply(200, result)


class TLSServer(HTTPServer):
    handshake_timeout = 5

    def get_request(self):
        connection, address = super().get_request()
        connection.settimeout(self.handshake_timeout)
        try:
            return self.tls.wrap_socket(connection, server_side=True), address
        except Exception:
            connection.close()
            raise


def main():
    if sys.argv[1:] == ["--normalize-pdf"]:
        return _normalization_worker()
    try:
        config = Config.from_environment()
    except ValueError as error:
        print(error, file=sys.stderr)
        return 2
    try:
        token = config.token_file.read_text().strip()
    except OSError as error:
        print(f"cannot read the print token: {error}", file=sys.stderr)
        return 2
    if len(token) < 32:
        print("the print token must be at least 32 characters", file=sys.stderr)
        return 2
    os.umask(0o077)
    config.state_dir.mkdir(parents=True, exist_ok=True)
    server = TLSServer((config.listen, config.port), Handler)
    server.token = token
    server.spooler = Spooler(config.state_dir, lambda key, path: cups_submit(key, path, config))
    tls = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    tls.minimum_version = ssl.TLSVersion.TLSv1_2
    tls.load_cert_chain(config.cert_file, config.key_file)
    server.tls = tls
    server.serve_forever()
    return 0


if __name__ == "__main__":
    sys.exit(main())
