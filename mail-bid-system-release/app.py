import datetime as dt
import email.policy
import email.utils
import json
import sqlite3
import smtplib
import socket
import sys
import threading
import time
import uuid
from dataclasses import dataclass
from email.message import EmailMessage
from pathlib import Path
from typing import Dict, List, Optional
from zoneinfo import ZoneInfo

from flask import Flask, jsonify, render_template, request

if getattr(sys, "frozen", False) and hasattr(sys, "_MEIPASS"):
    BASE_DIR = Path(sys._MEIPASS)
else:
    BASE_DIR = Path(__file__).resolve().parent
MAILBOX_DIR = BASE_DIR / "mailbox"
OUTBOX_DIR = MAILBOX_DIR / "Outbox"
UPLOAD_DIR = BASE_DIR / "uploads"
DATA_DIR = BASE_DIR / "data"
DB_FILE = DATA_DIR / "delivery.db"

TZ_SHANGHAI = ZoneInfo("Asia/Shanghai")

for folder in (MAILBOX_DIR, OUTBOX_DIR, UPLOAD_DIR, DATA_DIR):
    folder.mkdir(parents=True, exist_ok=True)


def now_shanghai() -> dt.datetime:
    return dt.datetime.now(TZ_SHANGHAI)


def now_iso() -> str:
    return now_shanghai().isoformat(timespec="milliseconds")


def parse_bool(value: Optional[str], default: bool = False) -> bool:
    if value is None:
        return default
    return value.lower() in {"1", "true", "yes", "on"}


def parse_schedule_time(value: str) -> dt.datetime:
    naive = dt.datetime.strptime(value, "%Y-%m-%d %H:%M:%S")
    return naive.replace(tzinfo=TZ_SHANGHAI)


class NTPClient:
    @staticmethod
    def query(server: str, timeout: float = 5.0) -> dt.datetime:
        msg = b"\x1b" + 47 * b"\0"
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.settimeout(timeout)
            sock.sendto(msg, (server, 123))
            data, _ = sock.recvfrom(48)
        if len(data) < 48:
            raise RuntimeError("invalid NTP response")
        ntp_seconds = int.from_bytes(data[40:44], "big")
        ntp_fraction = int.from_bytes(data[44:48], "big")
        ts = ntp_seconds - 2208988800 + (ntp_fraction / 2**32)
        return dt.datetime.fromtimestamp(ts, tz=TZ_SHANGHAI)


@dataclass
class SMTPClientConfig:
    host: str
    port: int
    username: str
    password: str
    use_tls: bool
    use_ssl: bool


class SQLiteStore:
    def __init__(self, db_path: Path):
        self.db_path = db_path
        self._lock = threading.Lock()
        self._init_db()

    def _connect(self):
        conn = sqlite3.connect(self.db_path, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_db(self):
        with self._connect() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS deliveries (
                    delivery_id TEXT PRIMARY KEY,
                    task_id TEXT UNIQUE NOT NULL,
                    status TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    sender TEXT NOT NULL,
                    recipient TEXT NOT NULL,
                    subject TEXT,
                    body TEXT,
                    html_body TEXT,
                    host TEXT NOT NULL,
                    port INTEGER NOT NULL,
                    username TEXT,
                    password TEXT,
                    use_tls INTEGER NOT NULL,
                    use_ssl INTEGER NOT NULL,
                    use_ntp INTEGER NOT NULL,
                    ntp_server TEXT,
                    save_outbox INTEGER NOT NULL,
                    attempt_count INTEGER NOT NULL,
                    first_send_at TEXT NOT NULL,
                    second_send_at TEXT,
                    third_send_at TEXT,
                    attachment_manifest TEXT NOT NULL,
                    total_success INTEGER NOT NULL DEFAULT 0,
                    total_failed INTEGER NOT NULL DEFAULT 0
                );

                CREATE TABLE IF NOT EXISTS delivery_attempts (
                    attempt_id TEXT PRIMARY KEY,
                    delivery_id TEXT NOT NULL,
                    attempt_index INTEGER NOT NULL,
                    scheduled_for TEXT NOT NULL,
                    status TEXT NOT NULL,
                    started_at TEXT,
                    finished_at TEXT,
                    duration_ms REAL,
                    error TEXT,
                    client_ip TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    UNIQUE(delivery_id, attempt_index),
                    FOREIGN KEY (delivery_id) REFERENCES deliveries(delivery_id)
                );

                CREATE TABLE IF NOT EXISTS delivery_events (
                    event_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    delivery_id TEXT NOT NULL,
                    attempt_id TEXT,
                    attempt_index INTEGER,
                    ts TEXT NOT NULL,
                    event TEXT NOT NULL,
                    payload TEXT,
                    FOREIGN KEY (delivery_id) REFERENCES deliveries(delivery_id)
                );

                CREATE TABLE IF NOT EXISTS task_logs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    task_id TEXT NOT NULL,
                    ts TEXT NOT NULL,
                    message TEXT NOT NULL
                );

                CREATE INDEX IF NOT EXISTS idx_delivery_events_delivery_id ON delivery_events(delivery_id);
                CREATE INDEX IF NOT EXISTS idx_task_logs_task_id ON task_logs(task_id);
                """
            )
            # Lightweight migration for existing single-file DB.
            self._ensure_column(conn, "deliveries", "html_body", "TEXT")
            conn.commit()

    def _ensure_column(self, conn: sqlite3.Connection, table: str, column: str, sql_type: str):
        cols = conn.execute(f"PRAGMA table_info({table})").fetchall()
        names = {c[1] for c in cols}
        if column not in names:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {sql_type}")

    def create_delivery(self, payload: dict):
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                INSERT INTO deliveries (
                    delivery_id, task_id, status, created_at, updated_at,
                    sender, recipient, subject, body, html_body,
                    host, port, username, password,
                    use_tls, use_ssl, use_ntp, ntp_server, save_outbox,
                    attempt_count, first_send_at, second_send_at, third_send_at,
                    attachment_manifest
                ) VALUES (
                    :delivery_id, :task_id, :status, :created_at, :updated_at,
                    :sender, :recipient, :subject, :body, :html_body,
                    :host, :port, :username, :password,
                    :use_tls, :use_ssl, :use_ntp, :ntp_server, :save_outbox,
                    :attempt_count, :first_send_at, :second_send_at, :third_send_at,
                    :attachment_manifest
                )
                """,
                payload,
            )

            for attempt in payload["attempts"]:
                conn.execute(
                    """
                    INSERT INTO delivery_attempts (
                        attempt_id, delivery_id, attempt_index, scheduled_for,
                        status, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, 'scheduled', ?, ?)
                    """,
                    (
                        attempt["attempt_id"],
                        payload["delivery_id"],
                        attempt["attempt_index"],
                        attempt["scheduled_for"],
                        payload["created_at"],
                        payload["created_at"],
                    ),
                )
            conn.commit()

    def append_event(
        self,
        delivery_id: str,
        event: str,
        attempt_id: Optional[str] = None,
        attempt_index: Optional[int] = None,
        payload: Optional[dict] = None,
    ):
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                INSERT INTO delivery_events (delivery_id, attempt_id, attempt_index, ts, event, payload)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    delivery_id,
                    attempt_id,
                    attempt_index,
                    now_iso(),
                    event,
                    json.dumps(payload or {}, ensure_ascii=False),
                ),
            )
            conn.commit()

    def append_task_log(self, task_id: str, message: str):
        with self._lock, self._connect() as conn:
            conn.execute(
                "INSERT INTO task_logs (task_id, ts, message) VALUES (?, ?, ?)",
                (task_id, now_iso(), message),
            )
            conn.commit()

    def set_delivery_status(self, delivery_id: str, status: str, total_success: int, total_failed: int):
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                UPDATE deliveries
                SET status=?, total_success=?, total_failed=?, updated_at=?
                WHERE delivery_id=?
                """,
                (status, total_success, total_failed, now_iso(), delivery_id),
            )
            conn.commit()

    def update_attempt_running(self, attempt_id: str, client_ip: str):
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                UPDATE delivery_attempts
                SET status='running', started_at=?, updated_at=?, error=NULL, client_ip=?
                WHERE attempt_id=?
                """,
                (now_iso(), now_iso(), client_ip, attempt_id),
            )
            conn.commit()

    def update_attempt_finished(self, attempt_id: str, success: bool, duration_ms: float, error: Optional[str]):
        new_status = "succeeded" if success else "failed"
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                UPDATE delivery_attempts
                SET status=?, finished_at=?, duration_ms=?, error=?, updated_at=?
                WHERE attempt_id=?
                """,
                (new_status, now_iso(), duration_ms, error, now_iso(), attempt_id),
            )
            conn.commit()

    def get_delivery_by_task_id(self, task_id: str) -> Optional[sqlite3.Row]:
        with self._connect() as conn:
            return conn.execute("SELECT * FROM deliveries WHERE task_id=?", (task_id,)).fetchone()

    def get_delivery(self, delivery_id: str) -> Optional[sqlite3.Row]:
        with self._connect() as conn:
            return conn.execute("SELECT * FROM deliveries WHERE delivery_id=?", (delivery_id,)).fetchone()

    def get_attempts(self, delivery_id: str) -> List[sqlite3.Row]:
        with self._connect() as conn:
            return conn.execute(
                "SELECT * FROM delivery_attempts WHERE delivery_id=? ORDER BY attempt_index ASC", (delivery_id,)
            ).fetchall()

    def get_task_logs(self, task_id: str) -> List[str]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT ts, message FROM task_logs WHERE task_id=? ORDER BY id ASC", (task_id,)
            ).fetchall()
        return [f"[{row['ts']}] {row['message']}" for row in rows]

    def get_events(self, delivery_id: str) -> List[dict]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT event_id, attempt_id, attempt_index, ts, event, payload
                FROM delivery_events
                WHERE delivery_id=?
                ORDER BY event_id ASC
                """,
                (delivery_id,),
            ).fetchall()
        out = []
        for row in rows:
            payload = {}
            if row["payload"]:
                try:
                    payload = json.loads(row["payload"])
                except json.JSONDecodeError:
                    payload = {"raw": row["payload"]}
            out.append(
                {
                    "event_id": row["event_id"],
                    "attempt_id": row["attempt_id"],
                    "attempt_index": row["attempt_index"],
                    "ts": row["ts"],
                    "event": row["event"],
                    "payload": payload,
                }
            )
        return out

    def get_delivery_list(self, limit: int = 50) -> List[dict]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT delivery_id, task_id, status, created_at, updated_at,
                       sender, recipient, subject, host, port,
                       attempt_count, first_send_at, second_send_at, third_send_at,
                       total_success, total_failed
                FROM deliveries
                ORDER BY created_at DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()

        out = []
        for row in rows:
            out.append(
                {
                    "delivery_id": row["delivery_id"],
                    "task_id": row["task_id"],
                    "status": row["status"],
                    "created_at": row["created_at"],
                    "updated_at": row["updated_at"],
                    "sender": row["sender"],
                    "recipient": row["recipient"],
                    "subject": row["subject"],
                    "host": row["host"],
                    "port": row["port"],
                    "attempt_count": row["attempt_count"],
                    "schedule": [row["first_send_at"], row["second_send_at"], row["third_send_at"]],
                    "total_success": row["total_success"],
                    "total_failed": row["total_failed"],
                }
            )
        return out

    def get_pending_deliveries(self) -> List[str]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT DISTINCT d.delivery_id
                FROM deliveries d
                JOIN delivery_attempts a ON a.delivery_id = d.delivery_id
                WHERE d.status IN ('queued','running','partial_success')
                  AND a.status IN ('scheduled','running')
                ORDER BY d.created_at ASC
                """
            ).fetchall()
        return [r["delivery_id"] for r in rows]


class MailService:
    class _TraceMixin:
        def __init__(self, *args, **kwargs):
            self.transcript: List[str] = []
            super().__init__(*args, **kwargs)

        def _print_debug(self, *args):
            line = " ".join(str(a) for a in args)
            self.transcript.append(line)

    class TraceSMTP(_TraceMixin, smtplib.SMTP):
        pass

    class TraceSMTP_SSL(_TraceMixin, smtplib.SMTP_SSL):
        pass

    @staticmethod
    def resolve_client_ip(host: str, port: int) -> str:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.connect((host, port))
            return sock.getsockname()[0]

    @staticmethod
    def guess_mime(file_name: str):
        name = file_name.lower()
        if name.endswith(".txt"):
            return "text", "plain"
        if name.endswith(".html") or name.endswith(".htm"):
            return "text", "html"
        if name.endswith(".pdf"):
            return "application", "pdf"
        if name.endswith(".json"):
            return "application", "json"
        if name.endswith(".png"):
            return "image", "png"
        if name.endswith(".jpg") or name.endswith(".jpeg"):
            return "image", "jpeg"
        return "application", "octet-stream"

    @staticmethod
    def build_message(
        sender: str,
        recipient: str,
        subject: str,
        body: str,
        html_body: str,
        attachments: List[Path],
    ) -> EmailMessage:
        msg = EmailMessage()
        msg["From"] = sender
        msg["To"] = recipient
        msg["Subject"] = subject
        msg["Date"] = email.utils.formatdate(localtime=True)
        msg.set_content(body)
        if html_body and html_body.strip():
            msg.add_alternative(html_body, subtype="html")
        for path in attachments:
            data = path.read_bytes()
            mt, st = MailService.guess_mime(path.name)
            msg.add_attachment(data, maintype=mt, subtype=st, filename=path.name)
        return msg

    @staticmethod
    def open_client(config: SMTPClientConfig):
        if config.use_ssl:
            client = MailService.TraceSMTP_SSL(config.host, config.port, timeout=25)
        else:
            client = MailService.TraceSMTP(config.host, config.port, timeout=25)
        # Enable before SMTP commands so EHLO/STARTTLS/AUTH are captured too.
        client.set_debuglevel(1)
        if not config.use_ssl:
            if config.use_tls:
                client.starttls()
        if config.username:
            client.login(config.username, config.password)
        return client

    @staticmethod
    def save_outbox(message: EmailMessage) -> Path:
        stamp = dt.datetime.now().strftime("%Y%m%d_%H%M%S_%f")
        path = OUTBOX_DIR / f"mail_{stamp}.eml"
        path.write_bytes(message.as_bytes(policy=email.policy.SMTP))
        return path


class DeliveryExecutor:
    def __init__(self, store: SQLiteStore):
        self.store = store
        self._threads: Dict[str, threading.Thread] = {}
        self._lock = threading.Lock()

    def launch(self, delivery_id: str):
        with self._lock:
            t = self._threads.get(delivery_id)
            if t and t.is_alive():
                return
            nt = threading.Thread(target=self._run_delivery, args=(delivery_id,), daemon=True)
            self._threads[delivery_id] = nt
            nt.start()

    def recover_pending(self):
        for delivery_id in self.store.get_pending_deliveries():
            self.launch(delivery_id)

    def _run_delivery(self, delivery_id: str):
        delivery = self.store.get_delivery(delivery_id)
        if not delivery:
            return

        task_id = delivery["task_id"]
        self.store.append_task_log(task_id, "任务开始执行")
        self.store.append_event(delivery_id, "delivery_runner_started")

        config = SMTPClientConfig(
            host=delivery["host"],
            port=delivery["port"],
            username=delivery["username"] or "",
            password=delivery["password"] or "",
            use_tls=bool(delivery["use_tls"]),
            use_ssl=bool(delivery["use_ssl"]),
        )

        # DNS resolution support for SMTP hostnames.
        try:
            infos = socket.getaddrinfo(config.host, config.port, proto=socket.IPPROTO_TCP)
            resolved_ips = sorted({info[4][0] for info in infos if info and info[4]})
            self.store.append_task_log(task_id, f"DNS解析: {config.host} -> {', '.join(resolved_ips)}")
            self.store.append_event(
                delivery_id,
                "server_dns_resolved",
                payload={"host": config.host, "port": config.port, "resolved_ips": resolved_ips},
            )
        except Exception as exc:
            self.store.append_task_log(task_id, f"DNS解析失败: {exc}")
            self.store.append_event(
                delivery_id,
                "server_dns_resolve_failed",
                payload={"host": config.host, "port": config.port, "error": str(exc)},
            )

        attachment_paths = []
        try:
            manifest = json.loads(delivery["attachment_manifest"])
            attachment_paths = [Path(x) for x in manifest if Path(x).exists()]
        except json.JSONDecodeError:
            attachment_paths = []

        # 标记为 running
        self.store.set_delivery_status(delivery_id, "running", delivery["total_success"], delivery["total_failed"])

        offset_seconds = 0.0
        if bool(delivery["use_ntp"]):
            ntp_server = delivery["ntp_server"] or "pool.ntp.org"
            try:
                ntp_now = NTPClient.query(ntp_server)
                local_now = now_shanghai()
                offset_seconds = (ntp_now - local_now).total_seconds()
                self.store.append_task_log(task_id, f"NTP 同步成功，offset={offset_seconds:.3f}s")
                self.store.append_event(
                    delivery_id,
                    "ntp_synced",
                    payload={
                        "ntp_server": ntp_server,
                        "ntp_now": ntp_now.isoformat(timespec="milliseconds"),
                        "local_now": local_now.isoformat(timespec="milliseconds"),
                        "offset_seconds": round(offset_seconds, 6),
                    },
                )
            except Exception as exc:
                self.store.append_task_log(task_id, f"NTP 同步失败: {exc}")
                self.store.append_event(delivery_id, "ntp_sync_failed", payload={"error": str(exc)})

        attempts = self.store.get_attempts(delivery_id)
        success_count = 0
        failed_count = 0

        for attempt in attempts:
            if attempt["status"] == "succeeded":
                success_count += 1
                continue
            if attempt["status"] == "failed":
                failed_count += 1
                continue

            attempt_id = attempt["attempt_id"]
            attempt_index = attempt["attempt_index"]
            scheduled_for = dt.datetime.fromisoformat(attempt["scheduled_for"])

            self.store.append_event(
                delivery_id,
                "attempt_schedule_registered",
                attempt_id=attempt_id,
                attempt_index=attempt_index,
                payload={"scheduled_for": attempt["scheduled_for"]},
            )

            while True:
                adjusted_now = now_shanghai() + dt.timedelta(seconds=offset_seconds)
                remaining = (scheduled_for - adjusted_now).total_seconds()
                if remaining <= 0:
                    break
                self.store.append_task_log(task_id, f"Attempt #{attempt_index} 等待 {remaining:.1f}s")
                time.sleep(min(remaining, 1.0))

            try:
                client_ip = MailService.resolve_client_ip(config.host, config.port)
            except Exception:
                client_ip = "unknown"

            self.store.append_event(
                delivery_id,
                "client_ip_resolved",
                attempt_id=attempt_id,
                attempt_index=attempt_index,
                payload={"client_ip": client_ip},
            )
            self.store.update_attempt_running(attempt_id, client_ip)
            self.store.append_event(
                delivery_id,
                "attempt_started",
                attempt_id=attempt_id,
                attempt_index=attempt_index,
                payload={"client_ip": client_ip},
            )

            subject = delivery["subject"] or ""
            body = delivery["body"] or ""
            html_body = delivery["html_body"] or ""
            msg = MailService.build_message(
                delivery["sender"], delivery["recipient"], subject, body, html_body, attachment_paths
            )

            send_start = time.perf_counter()
            smtp_transcript: List[str] = []
            try:
                with MailService.open_client(config) as smtp:
                    smtp.send_message(msg)
                    smtp_transcript = list(getattr(smtp, "transcript", []))

                duration_ms = round((time.perf_counter() - send_start) * 1000, 3)
                self.store.update_attempt_finished(attempt_id, True, duration_ms, None)
                self.store.append_task_log(task_id, f"Attempt #{attempt_index} 发送成功 ({duration_ms} ms)")
                self.store.append_event(
                    delivery_id,
                    "attempt_send_succeeded",
                    attempt_id=attempt_id,
                    attempt_index=attempt_index,
                    payload={"duration_ms": duration_ms},
                )
                if smtp_transcript:
                    self.store.append_event(
                        delivery_id,
                        "smtp_transcript",
                        attempt_id=attempt_id,
                        attempt_index=attempt_index,
                        payload={"lines": smtp_transcript},
                    )
                    self.store.append_task_log(task_id, f"Attempt #{attempt_index} SMTP通话日志开始")
                    for line in smtp_transcript:
                        self.store.append_task_log(task_id, f"SMTP[{attempt_index}] {line}")
                    self.store.append_task_log(task_id, f"Attempt #{attempt_index} SMTP通话日志结束")
                success_count += 1

                if bool(delivery["save_outbox"]):
                    out = MailService.save_outbox(msg)
                    self.store.append_event(
                        delivery_id,
                        "outbox_saved",
                        attempt_id=attempt_id,
                        attempt_index=attempt_index,
                        payload={"filename": out.name},
                    )
            except Exception as exc:
                duration_ms = round((time.perf_counter() - send_start) * 1000, 3)
                if 'smtp' in locals():
                    smtp_transcript = list(getattr(smtp, "transcript", []))
                self.store.update_attempt_finished(attempt_id, False, duration_ms, str(exc))
                self.store.append_task_log(task_id, f"Attempt #{attempt_index} 发送失败: {exc}")
                self.store.append_event(
                    delivery_id,
                    "attempt_send_failed",
                    attempt_id=attempt_id,
                    attempt_index=attempt_index,
                    payload={"duration_ms": duration_ms, "error": str(exc)},
                )
                if smtp_transcript:
                    self.store.append_event(
                        delivery_id,
                        "smtp_transcript",
                        attempt_id=attempt_id,
                        attempt_index=attempt_index,
                        payload={"lines": smtp_transcript},
                    )
                    self.store.append_task_log(task_id, f"Attempt #{attempt_index} SMTP通话日志开始")
                    for line in smtp_transcript:
                        self.store.append_task_log(task_id, f"SMTP[{attempt_index}] {line}")
                    self.store.append_task_log(task_id, f"Attempt #{attempt_index} SMTP通话日志结束")
                failed_count += 1

        if failed_count == 0:
            final_status = "success"
        elif success_count == 0:
            final_status = "failed"
        else:
            final_status = "partial_success"

        self.store.set_delivery_status(delivery_id, final_status, success_count, failed_count)
        self.store.append_task_log(task_id, f"任务完成: {final_status}")
        self.store.append_event(
            delivery_id,
            "delivery_completed",
            payload={
                "status": final_status,
                "total_success": success_count,
                "total_failed": failed_count,
            },
        )


app = Flask(__name__)
store = SQLiteStore(DB_FILE)
executor = DeliveryExecutor(store)


def save_uploaded_files(delivery_id: str, items) -> List[Path]:
    base = UPLOAD_DIR / delivery_id
    base.mkdir(parents=True, exist_ok=True)
    out = []
    for item in items:
        if not item.filename:
            continue
        target = base / Path(item.filename).name
        item.save(target)
        out.append(target)
    return out


def build_attempt_schedule(attempt_count: int, first: str, second: str, third: str) -> List[str]:
    times = [first]
    if attempt_count >= 2:
        times.append(second)
    if attempt_count == 3:
        times.append(third)

    parsed = [parse_schedule_time(t) for t in times]
    for i in range(1, len(parsed)):
        if parsed[i] <= parsed[i - 1]:
            raise ValueError("时间必须严格递增")

    return [p.isoformat(timespec="seconds") for p in parsed]


@app.get("/")
def index():
    return render_template("index.html")


@app.post("/api/send")
def api_send():
    form = request.form

    sender = (form.get("sender") or "").strip()
    recipient = (form.get("recipient") or "").strip()
    subject = (form.get("subject") or "").strip()
    body = form.get("body") or ""
    html_body = form.get("html_body") or ""
    host = (form.get("host") or "127.0.0.1").strip()

    try:
        port = int(form.get("port") or 25)
        attempt_count = int(form.get("attempt_count") or 1)
    except ValueError:
        return jsonify({"error": "port 或 attempt_count 非法"}), 400

    if attempt_count not in (1, 2, 3):
        return jsonify({"error": "attempt_count 只允许 1~3"}), 400

    first_send_at = (form.get("first_send_at") or "").strip()
    second_send_at = (form.get("second_send_at") or "").strip()
    third_send_at = (form.get("third_send_at") or "").strip()

    if not sender or not recipient:
        return jsonify({"error": "sender 和 recipient 必填"}), 400

    if not first_send_at:
        return jsonify({"error": "first_send_at 必填"}), 400
    if attempt_count >= 2 and not second_send_at:
        return jsonify({"error": "second_send_at 必填"}), 400
    if attempt_count == 3 and not third_send_at:
        return jsonify({"error": "third_send_at 必填"}), 400

    try:
        schedule = build_attempt_schedule(attempt_count, first_send_at, second_send_at, third_send_at)
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400

    delivery_id = str(uuid.uuid4())
    task_id = str(uuid.uuid4())
    attachments = save_uploaded_files(delivery_id, request.files.getlist("attachments"))

    payload = {
        "delivery_id": delivery_id,
        "task_id": task_id,
        "status": "queued",
        "created_at": now_iso(),
        "updated_at": now_iso(),
        "sender": sender,
        "recipient": recipient,
        "subject": subject,
        "body": body,
        "html_body": html_body,
        "host": host,
        "port": port,
        "username": (form.get("username") or "").strip(),
        "password": form.get("password") or "",
        "use_tls": 1 if parse_bool(form.get("use_tls"), False) else 0,
        "use_ssl": 1 if parse_bool(form.get("use_ssl"), False) else 0,
        "use_ntp": 1 if parse_bool(form.get("use_ntp"), False) else 0,
        "ntp_server": (form.get("ntp_server") or "pool.ntp.org").strip(),
        "save_outbox": 1 if parse_bool(form.get("save_outbox"), True) else 0,
        "attempt_count": attempt_count,
        "first_send_at": schedule[0],
        "second_send_at": schedule[1] if attempt_count >= 2 else None,
        "third_send_at": schedule[2] if attempt_count == 3 else None,
        "attachment_manifest": json.dumps([str(p) for p in attachments], ensure_ascii=False),
        "attempts": [
            {
                "attempt_id": str(uuid.uuid4()),
                "attempt_index": i + 1,
                "scheduled_for": schedule[i],
            }
            for i in range(attempt_count)
        ],
    }

    store.create_delivery(payload)
    store.append_task_log(task_id, "任务已创建")
    store.append_event(
        delivery_id,
        "delivery_queued",
        payload={
            "task_id": task_id,
            "attempt_count": attempt_count,
            "schedule": schedule,
            "host": host,
            "port": port,
            "sender": sender,
            "recipient": recipient,
            "subject": subject,
            "timezone": "Asia/Shanghai",
            "attachments": [p.name for p in attachments],
        },
    )

    executor.launch(delivery_id)
    return jsonify({"task_id": task_id, "delivery_id": delivery_id, "timezone": "Asia/Shanghai"})


@app.get("/api/tasks/<task_id>")
def api_task(task_id: str):
    delivery = store.get_delivery_by_task_id(task_id)
    if not delivery:
        return jsonify({"error": "task not found"}), 404

    attempts = store.get_attempts(delivery["delivery_id"])
    attempt_items = []
    for a in attempts:
        attempt_items.append(
            {
                "attempt_id": a["attempt_id"],
                "attempt_index": a["attempt_index"],
                "scheduled_for": a["scheduled_for"],
                "status": a["status"],
                "started_at": a["started_at"],
                "finished_at": a["finished_at"],
                "duration_ms": a["duration_ms"],
                "error": a["error"],
                "client_ip": a["client_ip"],
            }
        )

    return jsonify(
        {
            "id": task_id,
            "delivery_id": delivery["delivery_id"],
            "status": delivery["status"],
            "created_at": delivery["created_at"],
            "updated_at": delivery["updated_at"],
            "logs": store.get_task_logs(task_id),
            "attempts": attempt_items,
            "total_success": delivery["total_success"],
            "total_failed": delivery["total_failed"],
            "timezone": "Asia/Shanghai",
        }
    )


@app.get("/api/deliveries")
def api_deliveries():
    limit = int(request.args.get("limit", 50))
    items = store.get_delivery_list(limit)
    for item in items:
        item["attempts"] = [
            {
                "attempt_index": a["attempt_index"],
                "status": a["status"],
                "scheduled_for": a["scheduled_for"],
                "started_at": a["started_at"],
                "finished_at": a["finished_at"],
                "duration_ms": a["duration_ms"],
                "error": a["error"],
            }
            for a in store.get_attempts(item["delivery_id"])
        ]
    return jsonify({"items": items, "timezone": "Asia/Shanghai"})


@app.get("/api/deliveries/<delivery_id>")
def api_delivery_events(delivery_id: str):
    delivery = store.get_delivery(delivery_id)
    if not delivery:
        return jsonify({"error": "delivery not found"}), 404

    attempts = store.get_attempts(delivery_id)
    events = store.get_events(delivery_id)
    return jsonify(
        {
            "delivery_id": delivery_id,
            "status": delivery["status"],
            "attempts": [
                {
                    "attempt_id": a["attempt_id"],
                    "attempt_index": a["attempt_index"],
                    "scheduled_for": a["scheduled_for"],
                    "status": a["status"],
                    "started_at": a["started_at"],
                    "finished_at": a["finished_at"],
                    "duration_ms": a["duration_ms"],
                    "error": a["error"],
                    "client_ip": a["client_ip"],
                }
                for a in attempts
            ],
            "events": events,
            "timezone": "Asia/Shanghai",
        }
    )


@app.get("/healthz")
def healthz():
    return jsonify({"status": "ok", "time": now_iso(), "timezone": "Asia/Shanghai"})


# 启动时恢复未完成任务
executor.recover_pending()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8080, debug=True)
