"""
Sensor onboarding utilities shared by collector and web API.
"""

import hashlib
import hmac
import re
import secrets
import sqlite3
import threading
from datetime import datetime, timedelta
from pathlib import Path

from .database import upsert_sensor

_CONFIG_LOCK = threading.Lock()


def now_iso() -> str:
    return datetime.now().strftime("%Y-%m-%dT%H:%M:%S")


def _hash_passcode(passcode: str) -> str:
    return hashlib.sha256(passcode.encode("utf-8")).hexdigest()


def create_temp_passcode(conn: sqlite3.Connection, ttl_minutes: int = 15) -> tuple[str, str]:
    code = f"{secrets.randbelow(1_000_000):06d}"
    created_at = now_iso()
    expires_at = (datetime.now() + timedelta(minutes=ttl_minutes)).strftime("%Y-%m-%dT%H:%M:%S")
    conn.execute(
        """
        INSERT INTO onboarding_temp_codes (code_hash, created_at, expires_at)
        VALUES (?, ?, ?)
        """,
        (_hash_passcode(code), created_at, expires_at),
    )
    conn.commit()
    return code, expires_at


def is_valid_temp_passcode(conn: sqlite3.Connection, passcode: str) -> bool:
    row = conn.execute(
        """
        SELECT code_hash
        FROM onboarding_temp_codes
        WHERE revoked_at IS NULL
          AND expires_at >= ?
        """,
        (now_iso(),),
    ).fetchall()
    if not row:
        return False
    code_hash = _hash_passcode(passcode)
    return any(hmac.compare_digest(entry["code_hash"], code_hash) for entry in row)


def queue_start_pairing(conn: sqlite3.Connection) -> int:
    cur = conn.execute(
        """
        INSERT INTO onboarding_commands (command, payload, status, created_at)
        VALUES (?, ?, 'pending', ?)
        """,
        ("start_pairing", "{}", now_iso()),
    )
    conn.execute(
        """
        UPDATE onboarding_state
        SET pairing_active = 0,
            pairing_started_at = NULL,
            pairing_ends_at = NULL,
            candidate_ieee = NULL,
            candidate_model = NULL,
            candidate_joined_at = NULL,
            first_reading_at = NULL,
            metadata_saved = 0,
            last_error = NULL,
            updated_at = ?
        WHERE id = 1
        """,
        (now_iso(),),
    )
    conn.commit()
    return cur.lastrowid


def fetch_pending_commands(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute(
        """
        SELECT id, command, payload
        FROM onboarding_commands
        WHERE status = 'pending'
        ORDER BY id ASC
        """
    ).fetchall()


def mark_command_done(conn: sqlite3.Connection, command_id: int) -> None:
    conn.execute(
        """
        UPDATE onboarding_commands
        SET status = 'done',
            processed_at = ?,
            error = NULL
        WHERE id = ?
        """,
        (now_iso(), command_id),
    )
    conn.commit()


def mark_command_failed(conn: sqlite3.Connection, command_id: int, error: str) -> None:
    conn.execute(
        """
        UPDATE onboarding_commands
        SET status = 'failed',
            processed_at = ?,
            error = ?
        WHERE id = ?
        """,
        (now_iso(), error[:500], command_id),
    )
    conn.execute(
        """
        UPDATE onboarding_state
        SET last_error = ?, updated_at = ?
        WHERE id = 1
        """,
        (error[:500], now_iso()),
    )
    conn.commit()


def set_pairing_state(conn: sqlite3.Connection, active: bool, duration_seconds: int = 0) -> None:
    started_at = now_iso() if active else None
    ends_at = None
    if active:
        ends_at = (datetime.now() + timedelta(seconds=duration_seconds)).strftime("%Y-%m-%dT%H:%M:%S")
    conn.execute(
        """
        UPDATE onboarding_state
        SET pairing_active = ?,
            pairing_started_at = ?,
            pairing_ends_at = ?,
            updated_at = ?
        WHERE id = 1
        """,
        (1 if active else 0, started_at, ends_at, now_iso()),
    )
    conn.commit()


def maybe_close_expired_pairing(conn: sqlite3.Connection) -> None:
    row = conn.execute(
        """
        SELECT pairing_active, pairing_ends_at
        FROM onboarding_state
        WHERE id = 1
        """
    ).fetchone()
    if not row or row["pairing_active"] != 1 or not row["pairing_ends_at"]:
        return
    if row["pairing_ends_at"] <= now_iso():
        conn.execute(
            """
            UPDATE onboarding_state
            SET pairing_active = 0,
                updated_at = ?
            WHERE id = 1
            """,
            (now_iso(),),
        )
        conn.commit()


def record_device_joined(conn: sqlite3.Connection, ieee: str, model: str | None) -> None:
    conn.execute(
        """
        UPDATE onboarding_state
        SET candidate_ieee = ?,
            candidate_model = ?,
            candidate_joined_at = ?,
            first_reading_at = NULL,
            metadata_saved = 0,
            updated_at = ?
        WHERE id = 1
        """,
        (ieee, model, now_iso(), now_iso()),
    )
    conn.commit()


def record_first_reading(conn: sqlite3.Connection, ieee: str) -> None:
    row = conn.execute(
        "SELECT candidate_ieee, first_reading_at FROM onboarding_state WHERE id = 1"
    ).fetchone()
    if not row or row["candidate_ieee"] != ieee or row["first_reading_at"] is not None:
        return
    conn.execute(
        """
        UPDATE onboarding_state
        SET first_reading_at = ?,
            updated_at = ?
        WHERE id = 1
        """,
        (now_iso(), now_iso()),
    )
    conn.commit()


def set_tcp_check_state(
    conn: sqlite3.Connection,
    *,
    precheck_ok: bool | None = None,
    postcheck_ok: bool | None = None,
) -> None:
    fields = []
    params: list[object] = []
    if precheck_ok is not None:
        fields.append("tcp_precheck_ok = ?")
        params.append(1 if precheck_ok else 0)
    if postcheck_ok is not None:
        fields.append("tcp_postcheck_ok = ?")
        params.append(1 if postcheck_ok else 0)
    if not fields:
        return
    fields.append("updated_at = ?")
    params.append(now_iso())
    params.append(1)
    conn.execute(
        f"UPDATE onboarding_state SET {', '.join(fields)} WHERE id = ?",
        params,
    )
    conn.commit()


def get_onboarding_state(conn: sqlite3.Connection) -> dict:
    row = conn.execute("SELECT * FROM onboarding_state WHERE id = 1").fetchone()
    if not row:
        return {}
    return dict(row)


def save_sensor_metadata(
    conn: sqlite3.Connection,
    *,
    ieee_address: str,
    friendly_name: str,
    zone: str,
) -> None:
    upsert_sensor(
        conn,
        ieee_address=ieee_address,
        friendly_name=friendly_name,
        zone=zone,
    )
    conn.execute(
        "UPDATE readings SET zone = ? WHERE ieee_address = ?",
        (zone, ieee_address),
    )
    conn.execute(
        """
        UPDATE onboarding_state
        SET metadata_saved = 1,
            updated_at = ?
        WHERE id = 1
        """,
        (now_iso(),),
    )
    conn.commit()
    _update_config_mapping("SENSOR_NAMES", ieee_address, friendly_name)
    _update_config_mapping("ZONES", ieee_address, zone)


def _update_config_mapping(mapping_name: str, key: str, value: str) -> None:
    config_path = Path(__file__).with_name("config.py")
    with _CONFIG_LOCK:
        text = config_path.read_text(encoding="utf-8")
        updated = _upsert_dict_item(text, mapping_name, key, value)
        config_path.write_text(updated, encoding="utf-8")


def _upsert_dict_item(text: str, mapping_name: str, key: str, value: str) -> str:
    mapping_start = re.search(
        rf"{re.escape(mapping_name)}\s*:\s*dict\[[^\]]+\]\s*=\s*\{{",
        text,
    )
    if not mapping_start:
        raise ValueError(f"Mapping {mapping_name} not found in config.py")

    brace_open_idx = text.find("{", mapping_start.start())
    if brace_open_idx < 0:
        raise ValueError(f"Could not find opening brace for {mapping_name}")

    brace_depth = 0
    brace_close_idx = -1
    for idx in range(brace_open_idx, len(text)):
        ch = text[idx]
        if ch == "{":
            brace_depth += 1
        elif ch == "}":
            brace_depth -= 1
            if brace_depth == 0:
                brace_close_idx = idx
                break
    if brace_close_idx < 0:
        raise ValueError(f"Could not find closing brace for {mapping_name}")

    dict_body = text[brace_open_idx + 1:brace_close_idx]
    line_pattern = re.compile(rf'^(\s*)"{re.escape(key)}"\s*:\s*"[^"]*"(,?.*)$', re.MULTILINE)

    if line_pattern.search(dict_body):
        new_body = line_pattern.sub(rf'\1"{key}": "{value}"\2', dict_body)
    else:
        insertion = f'    "{key}": "{value}",\n'
        body_strip = dict_body.rstrip()
        if not body_strip:
            new_body = "\n" + insertion
        else:
            if not body_strip.endswith("\n"):
                body_strip += "\n"
            new_body = body_strip + insertion

    return text[:brace_open_idx + 1] + new_body + text[brace_close_idx:]
