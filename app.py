import os
import csv
import sqlite3
from io import StringIO
from datetime import datetime
from zoneinfo import ZoneInfo

import requests
from flask import Flask, request, render_template, redirect, Response

app = Flask(__name__)

BOT_TOKEN = os.environ.get("BOT_TOKEN")
BOT_SECRET = os.environ.get("BOT_SECRET", "secret123")
ADMIN_PIN = os.environ.get("ADMIN_PIN", "1234")

TIMEZONE = ZoneInfo("Asia/Kolkata")
DB_NAME = "attendance.db"

MONTHLY_LEAVE_LIMIT = 4

# For testing, attendance is open all day.
ATTENDANCE_START_HOUR = 8
ATTENDANCE_START_MINUTE = 45

ATTENDANCE_END_HOUR = 9
ATTENDANCE_END_MINUTE = 0


def get_db():
    conn = sqlite3.connect(DB_NAME)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    conn = get_db()
    cur = conn.cursor()

    cur.execute("""
        CREATE TABLE IF NOT EXISTS employees (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            employee_code TEXT UNIQUE NOT NULL,
            telegram_user_id TEXT UNIQUE,
            is_admin INTEGER DEFAULT 0
        )
    """)

    cur.execute("""
        CREATE TABLE IF NOT EXISTS attendance (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            employee_id INTEGER NOT NULL,
            attendance_date TEXT NOT NULL,
            status TEXT NOT NULL,
            marked_at TEXT NOT NULL,
            source TEXT DEFAULT 'Telegram',
            UNIQUE(employee_id, attendance_date),
            FOREIGN KEY(employee_id) REFERENCES employees(id)
        )
    """)

    employees = [
        ("Fathima", "EMP001", 0),
        ("Akhil", "EMP002", 0),
        ("Sara", "EMP003", 0),
        ("Rahul", "EMP008", 0),
        ("Meera", "EMP009", 0),
        ("David", "EMP010", 0),
        ("Admin", "ADMIN01", 1),
    ]

    for name, code, is_admin in employees:
        cur.execute("""
            INSERT OR IGNORE INTO employees (name, employee_code, is_admin)
            VALUES (?, ?, ?)
        """, (name, code, is_admin))

    conn.commit()
    conn.close()


def send_message(chat_id, text, reply_markup=None):
    if not BOT_TOKEN:
        print("BOT_TOKEN is missing")
        return

    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"

    payload = {
        "chat_id": chat_id,
        "text": text,
        "parse_mode": "HTML"
    }

    if reply_markup:
        payload["reply_markup"] = reply_markup

    requests.post(url, json=payload, timeout=10)


def main_menu():
    return {
        "inline_keyboard": [
            [
                {"text": "Mark Present", "callback_data": "MARK_PRESENT"},
                {"text": "Mark Leave", "callback_data": "MARK_LEAVE"}
            ],
            [
                {"text": "My Leave Balance", "callback_data": "BALANCE"},
                {"text": "My Monthly Summary", "callback_data": "SUMMARY"}
            ]
        ]
    }


def today_str():
    return datetime.now(TIMEZONE).date().isoformat()


def now_str():
    return datetime.now(TIMEZONE).strftime("%Y-%m-%d %H:%M:%S")


def is_weekend():
    return False


def is_inside_attendance_window():
    return True


def get_employee_by_telegram_id(telegram_user_id):
    conn = get_db()
    emp = conn.execute("""
        SELECT * FROM employees WHERE telegram_user_id = ?
    """, (str(telegram_user_id),)).fetchone()
    conn.close()
    return emp


def get_employee_by_code(employee_code):
    conn = get_db()
    emp = conn.execute("""
        SELECT * FROM employees WHERE employee_code = ?
    """, (employee_code.upper(),)).fetchone()
    conn.close()
    return emp


def register_employee(employee_code, telegram_user_id):
    emp = get_employee_by_code(employee_code)

    if not emp:
        return False, "Invalid employee code."

    if emp["telegram_user_id"] and emp["telegram_user_id"] != str(telegram_user_id):
        return False, "This employee code is already linked to another Telegram account."

    conn = get_db()
    conn.execute("""
        UPDATE employees
        SET telegram_user_id = ?
        WHERE employee_code = ?
    """, (str(telegram_user_id), employee_code.upper()))
    conn.commit()
    conn.close()

    return True, f"Registration successful. Welcome, {emp['name']}."


def mark_attendance(employee_id, status):
    if is_weekend():
        return False, "Attendance is not required on weekends."

    if not is_inside_attendance_window():
        return False, "Attendance window is closed. You can mark attendance only between 8:45 AM and 9:00 AM."

    conn = get_db()

    existing = conn.execute("""
        SELECT * FROM attendance
        WHERE employee_id = ? AND attendance_date = ?
    """, (employee_id, today_str())).fetchone()

    if existing:
        conn.close()
        return False, f"You already marked <b>{existing['status']}</b> today at {existing['marked_at']}."

    conn.execute("""
        INSERT INTO attendance (employee_id, attendance_date, status, marked_at)
        VALUES (?, ?, ?, ?)
    """, (employee_id, today_str(), status, now_str()))

    conn.commit()
    conn.close()

    return True, f"Attendance marked as <b>{status}</b> at {now_str()}."


def get_leave_balance(employee_id):
    now = datetime.now(TIMEZONE)
    year = str(now.year)
    month = f"{now.month:02d}"

    conn = get_db()
    leaves_used = conn.execute("""
        SELECT COUNT(*) AS count
        FROM attendance
        WHERE employee_id = ?
        AND status = 'Leave'
        AND strftime('%Y', attendance_date) = ?
        AND strftime('%m', attendance_date) = ?
    """, (employee_id, year, month)).fetchone()["count"]

    conn.close()

    remaining = max(MONTHLY_LEAVE_LIMIT - leaves_used, 0)
    return leaves_used, remaining


def get_employee_summary(employee_id):
    now = datetime.now(TIMEZONE)
    year = str(now.year)
    month = f"{now.month:02d}"

    conn = get_db()
    rows = conn.execute("""
        SELECT status, COUNT(*) AS count
        FROM attendance
        WHERE employee_id = ?
        AND strftime('%Y', attendance_date) = ?
        AND strftime('%m', attendance_date) = ?
        GROUP BY status
    """, (employee_id, year, month)).fetchall()

    conn.close()

    summary = {"Present": 0, "Leave": 0}

    for row in rows:
        summary[row["status"]] = row["count"]

    leaves_used, leaves_remaining = get_leave_balance(employee_id)

    return summary["Present"], leaves_used, leaves_remaining


@app.route(f"/telegram/{BOT_SECRET}", methods=["POST"])
def telegram_webhook():
    update = request.get_json()

    if "message" in update:
        message = update["message"]
        chat_id = message["chat"]["id"]
        telegram_user_id = message["from"]["id"]
        text = message.get("text", "").strip()

        if text == "/start":
            send_message(
                chat_id,
                "Welcome to the Attendance & Leave Management Bot.\n\n"
                "To register, send:\n"
                "<b>/register EMP001</b>"
            )
            return "ok"

        if text.startswith("/register"):
            parts = text.split()

            if len(parts) != 2:
                send_message(chat_id, "Use this format:\n/register EMP001")
                return "ok"

            success, response = register_employee(parts[1], telegram_user_id)
            send_message(chat_id, response)

            if success:
                send_message(chat_id, "Choose an option:", main_menu())

            return "ok"

        emp = get_employee_by_telegram_id(telegram_user_id)

        if not emp:
            send_message(chat_id, "You are not registered. Send /register EMP001 first.")
            return "ok"

        send_message(chat_id, "Choose an option:", main_menu())
        return "ok"

    if "callback_query" in update:
        callback = update["callback_query"]
        chat_id = callback["message"]["chat"]["id"]
        telegram_user_id = callback["from"]["id"]
        data = callback["data"]

        emp = get_employee_by_telegram_id(telegram_user_id)

        if not emp:
            send_message(chat_id, "You are not registered. Send /register EMP001 first.")
            return "ok"

        if data == "MARK_PRESENT":
            success, response = mark_attendance(emp["id"], "Present")
            send_message(chat_id, response)

        elif data == "MARK_LEAVE":
            success, response = mark_attendance(emp["id"], "Leave")
            send_message(chat_id, response)

        elif data == "BALANCE":
            leaves_used, leaves_remaining = get_leave_balance(emp["id"])
            send_message(
                chat_id,
                f"Leave Balance\n\n"
                f"Monthly leave limit: {MONTHLY_LEAVE_LIMIT}\n"
                f"Leaves used: {leaves_used}\n"
                f"Leaves remaining: {leaves_remaining}"
            )

        elif data == "SUMMARY":
            present, leaves_used, leaves_remaining = get_employee_summary(emp["id"])
            send_message(
                chat_id,
                f"Monthly Summary\n\n"
                f"Present days: {present}\n"
                f"Leave days: {leaves_used}\n"
                f"Leaves remaining: {leaves_remaining}"
            )

        send_message(chat_id, "Choose another option:", main_menu())
        return "ok"

    return "ok"


@app.route("/")
def home():
    return redirect("/dashboard")


@app.route("/dashboard")
def dashboard():
    pin = request.args.get("pin")

    if pin != ADMIN_PIN:
        return "Unauthorized. Add ?pin=1234 to the URL."

    conn = get_db()

    rows = conn.execute("""
        SELECT e.name, a.status, a.marked_at
        FROM employees e
        LEFT JOIN attendance a
            ON e.id = a.employee_id
            AND a.attendance_date = ?
        WHERE e.is_admin = 0
        ORDER BY e.name
    """, (today_str(),)).fetchall()

    conn.close()

    total_employees = len(rows)
    total_present = sum(1 for row in rows if row["status"] == "Present")
    total_leave = sum(1 for row in rows if row["status"] == "Leave")
    total_not_marked = total_employees - total_present - total_leave

    return render_template(
        "dashboard.html",
        today=today_str(),
        rows=rows,
        total_employees=total_employees,
        total_present=total_present,
        total_leave=total_leave,
        total_not_marked=total_not_marked
    )


def build_monthly_report():
    now = datetime.now(TIMEZONE)
    year = str(now.year)
    month = f"{now.month:02d}"

    conn = get_db()
    employees = conn.execute("""
        SELECT * FROM employees
        WHERE is_admin = 0
        ORDER BY name
    """).fetchall()

    report_rows = []

    for emp in employees:
        present = conn.execute("""
            SELECT COUNT(*) AS count
            FROM attendance
            WHERE employee_id = ?
            AND status = 'Present'
            AND strftime('%Y', attendance_date) = ?
            AND strftime('%m', attendance_date) = ?
        """, (emp["id"], year, month)).fetchone()["count"]

        leave = conn.execute("""
            SELECT COUNT(*) AS count
            FROM attendance
            WHERE employee_id = ?
            AND status = 'Leave'
            AND strftime('%Y', attendance_date) = ?
            AND strftime('%m', attendance_date) = ?
        """, (emp["id"], year, month)).fetchone()["count"]

        remaining = max(MONTHLY_LEAVE_LIMIT - leave, 0)

        report_rows.append({
            "name": emp["name"],
            "present": present,
            "leave": leave,
            "leaves_remaining": remaining
        })

    conn.close()
    return report_rows


@app.route("/report")
def report():
    pin = request.args.get("pin")

    if pin != ADMIN_PIN:
        return "Unauthorized."

    rows = build_monthly_report()
    return render_template("report.html", rows=rows)


@app.route("/download-report")
def download_report():
    pin = request.args.get("pin")

    if pin != ADMIN_PIN:
        return "Unauthorized."

    rows = build_monthly_report()

    output = StringIO()
    writer = csv.writer(output)

    writer.writerow(["Employee Name", "Present Days", "Leave Days", "Leaves Remaining"])

    for row in rows:
        writer.writerow([
            row["name"],
            row["present"],
            row["leave"],
            row["leaves_remaining"]
        ])

    response = Response(output.getvalue(), mimetype="text/csv")
    response.headers["Content-Disposition"] = "attachment; filename=monthly_report.csv"

    return response


init_db()

if __name__ == "__main__":
    app.run(debug=True)
    
