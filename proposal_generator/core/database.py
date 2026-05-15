import sqlite3
import os
import json
from datetime import datetime

DB_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'data')
DB_PATH = os.path.join(DB_DIR, 'proposals.db')

SAMPLE_TECHNICIANS = [
    ('김석일', '정보통신기술자', '특급', '2018.09.17'),
    ('박준성', '정보통신기술자', '특급', '2022.01.26'),
    ('양용민', '정보통신기술자', '특급', '2023.04.24'),
    ('유서준', '정보통신기술자', '특급', '2023.04.03'),
    ('이대규', '정보통신기술자', '특급', '2016.04.01'),
    ('장우영', '정보통신기술자', '특급', '2023.02.21'),
    ('주영근', '정보통신기술자', '특급', '2019.12.30'),
    ('김만진', '정보통신기술자', '고급', '2021.06.14'),
    ('권동주', '정보통신기술자', '고급', '2018.08.01'),
    ('장지원', '정보통신기술자', '고급', '2018.10.08'),
    ('김평삼', '정보통신기술자', '고급', '2020.01.02'),
    ('최양진', '정보통신기술자', '고급', '2020.07.10'),
    ('박기대', '정보통신기술자', '고급', '2024.11.01'),
    ('국종필', '정보통신기술자', '고급', '2018.01.02'),
    ('김동연', '정보통신기술자', '고급', '2020.11.23'),
    ('이봉성', '정보통신기술자', '고급', '2023.03.06'),
    ('정지욱', '정보통신기술자', '고급', '2023.05.02'),
    ('정찬호', '정보통신기술자', '고급', '2021.01.01'),
    ('양재협', '정보통신기술자', '중급', '2024.12.02'),
    ('박성용', '정보통신기술자', '중급', '2022.01.01'),
    ('양수헌', '정보통신기술자', '중급', '2020.05.18'),
    ('김재효', '정보통신기술자', '초급', '2024.12.16'),
    ('여경주', '정보통신기술자', '초급', '2025.04.21'),
    ('엄정수', '정보통신기술자', '초급', '2020.04.01'),
    ('강기영', '정보통신기술자', '초급', '2020.01.02'),
    ('윤제영', '정보통신기술자', '초급', '2023.01.01'),
    ('이정훈', '정보통신기술자', '초급', '2021.02.01'),
]


def get_connection():
    os.makedirs(DB_DIR, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db():
    os.makedirs(DB_DIR, exist_ok=True)
    conn = get_connection()
    c = conn.cursor()

    c.executescript("""
        CREATE TABLE IF NOT EXISTS technicians (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            qualification TEXT NOT NULL DEFAULT '정보통신기술자',
            grade TEXT NOT NULL CHECK(grade IN ('특급','고급','중급','초급')),
            hire_date TEXT,
            is_active INTEGER NOT NULL DEFAULT 1
        );

        CREATE TABLE IF NOT EXISTS projects (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            client TEXT,
            amount INTEGER DEFAULT 0,
            contract_date TEXT,
            completion_date TEXT,
            certificate_path TEXT
        );

        CREATE TABLE IF NOT EXISTS bids (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            bid_name TEXT NOT NULL,
            bid_number TEXT,
            client TEXT,
            bid_date TEXT,
            company_name TEXT,
            bid_amount INTEGER DEFAULT 0,
            score_management REAL DEFAULT 6.0,
            score_experience REAL DEFAULT 6.0,
            score_technician REAL DEFAULT 6.0,
            score_reputation REAL DEFAULT 2.0,
            credit_rating TEXT DEFAULT 'BBB+',
            restriction_status TEXT DEFAULT 'none',
            retention_thresholds TEXT,
            deployment_thresholds TEXT,
            created_at TEXT,
            updated_at TEXT
        );

        CREATE TABLE IF NOT EXISTS bid_technicians (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            bid_id INTEGER NOT NULL,
            technician_id INTEGER NOT NULL,
            role TEXT NOT NULL CHECK(role IN ('retention','deployment')),
            FOREIGN KEY (bid_id) REFERENCES bids(id) ON DELETE CASCADE,
            FOREIGN KEY (technician_id) REFERENCES technicians(id) ON DELETE CASCADE
        );

        CREATE TABLE IF NOT EXISTS bid_projects (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            bid_id INTEGER NOT NULL,
            project_id INTEGER NOT NULL,
            FOREIGN KEY (bid_id) REFERENCES bids(id) ON DELETE CASCADE,
            FOREIGN KEY (project_id) REFERENCES projects(id) ON DELETE CASCADE
        );

        CREATE TABLE IF NOT EXISTS extra_items (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            bid_id INTEGER NOT NULL,
            name TEXT NOT NULL,
            max_score REAL DEFAULT 0.0,
            actual_score REAL DEFAULT 0.0,
            description TEXT,
            documents TEXT,
            FOREIGN KEY (bid_id) REFERENCES bids(id) ON DELETE CASCADE
        );
    """)

    # Insert sample technicians if table is empty
    c.execute("SELECT COUNT(*) FROM technicians")
    count = c.fetchone()[0]
    if count == 0:
        for name, qual, grade, hire_date in SAMPLE_TECHNICIANS:
            c.execute(
                "INSERT INTO technicians (name, qualification, grade, hire_date, is_active) VALUES (?,?,?,?,1)",
                (name, qual, grade, hire_date)
            )

    conn.commit()
    conn.close()


def row_to_dict(row):
    if row is None:
        return None
    return dict(row)


# ---- Technicians ----

def get_all_technicians():
    conn = get_connection()
    rows = conn.execute(
        "SELECT * FROM technicians ORDER BY CASE grade WHEN '특급' THEN 1 WHEN '고급' THEN 2 WHEN '중급' THEN 3 WHEN '초급' THEN 4 END, name"
    ).fetchall()
    conn.close()
    return [row_to_dict(r) for r in rows]


def add_technician(name, qualification, grade, hire_date):
    conn = get_connection()
    c = conn.cursor()
    c.execute(
        "INSERT INTO technicians (name, qualification, grade, hire_date, is_active) VALUES (?,?,?,?,1)",
        (name, qualification, grade, hire_date)
    )
    new_id = c.lastrowid
    conn.commit()
    conn.close()
    return new_id


def update_technician(tech_id, name=None, qualification=None, grade=None, hire_date=None, is_active=None):
    conn = get_connection()
    fields = []
    values = []
    if name is not None:
        fields.append('name=?'); values.append(name)
    if qualification is not None:
        fields.append('qualification=?'); values.append(qualification)
    if grade is not None:
        fields.append('grade=?'); values.append(grade)
    if hire_date is not None:
        fields.append('hire_date=?'); values.append(hire_date)
    if is_active is not None:
        fields.append('is_active=?'); values.append(is_active)
    if fields:
        values.append(tech_id)
        conn.execute(f"UPDATE technicians SET {', '.join(fields)} WHERE id=?", values)
        conn.commit()
    conn.close()


def delete_technician(tech_id):
    conn = get_connection()
    conn.execute("DELETE FROM technicians WHERE id=?", (tech_id,))
    conn.commit()
    conn.close()


# ---- Projects ----

def get_all_projects():
    conn = get_connection()
    rows = conn.execute(
        "SELECT * FROM projects ORDER BY contract_date DESC, name"
    ).fetchall()
    conn.close()
    return [row_to_dict(r) for r in rows]


def add_project(name, client, amount, contract_date, completion_date, certificate_path=''):
    conn = get_connection()
    c = conn.cursor()
    c.execute(
        "INSERT INTO projects (name, client, amount, contract_date, completion_date, certificate_path) VALUES (?,?,?,?,?,?)",
        (name, client, amount, contract_date, completion_date, certificate_path)
    )
    new_id = c.lastrowid
    conn.commit()
    conn.close()
    return new_id


def update_project(project_id, name=None, client=None, amount=None, contract_date=None,
                   completion_date=None, certificate_path=None):
    conn = get_connection()
    fields = []
    values = []
    if name is not None:
        fields.append('name=?'); values.append(name)
    if client is not None:
        fields.append('client=?'); values.append(client)
    if amount is not None:
        fields.append('amount=?'); values.append(amount)
    if contract_date is not None:
        fields.append('contract_date=?'); values.append(contract_date)
    if completion_date is not None:
        fields.append('completion_date=?'); values.append(completion_date)
    if certificate_path is not None:
        fields.append('certificate_path=?'); values.append(certificate_path)
    if fields:
        values.append(project_id)
        conn.execute(f"UPDATE projects SET {', '.join(fields)} WHERE id=?", values)
        conn.commit()
    conn.close()


def delete_project(project_id):
    conn = get_connection()
    conn.execute("DELETE FROM projects WHERE id=?", (project_id,))
    conn.commit()
    conn.close()


# ---- Bids ----

DEFAULT_RETENTION_THRESHOLDS = json.dumps([
    [40, 1.0], [30, 0.8], [20, 0.6], [10, 0.4]
])
DEFAULT_DEPLOYMENT_THRESHOLDS = json.dumps([
    [10, 1.0], [7, 0.7], [4, 0.4]
])


def get_all_bids():
    conn = get_connection()
    rows = conn.execute(
        "SELECT * FROM bids ORDER BY created_at DESC"
    ).fetchall()
    conn.close()
    return [row_to_dict(r) for r in rows]


def create_bid(bid_name, bid_number='', client='', bid_date='', company_name='',
               bid_amount=0, score_management=6.0, score_experience=6.0,
               score_technician=6.0, score_reputation=2.0):
    conn = get_connection()
    c = conn.cursor()
    now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    c.execute(
        """INSERT INTO bids (bid_name, bid_number, client, bid_date, company_name, bid_amount,
           score_management, score_experience, score_technician, score_reputation,
           credit_rating, restriction_status, retention_thresholds, deployment_thresholds,
           created_at, updated_at)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (bid_name, bid_number, client, bid_date, company_name, bid_amount,
         score_management, score_experience, score_technician, score_reputation,
         'BBB+', 'none', DEFAULT_RETENTION_THRESHOLDS, DEFAULT_DEPLOYMENT_THRESHOLDS,
         now, now)
    )
    new_id = c.lastrowid
    conn.commit()
    conn.close()
    return new_id


def get_bid(bid_id):
    conn = get_connection()
    row = conn.execute("SELECT * FROM bids WHERE id=?", (bid_id,)).fetchone()
    conn.close()
    return row_to_dict(row)


def update_bid(bid_id, **kwargs):
    conn = get_connection()
    allowed = ['bid_name', 'bid_number', 'client', 'bid_date', 'company_name', 'bid_amount',
               'score_management', 'score_experience', 'score_technician', 'score_reputation',
               'credit_rating', 'restriction_status', 'retention_thresholds', 'deployment_thresholds']
    fields = []
    values = []
    for k, v in kwargs.items():
        if k in allowed:
            fields.append(f'{k}=?')
            values.append(v)
    if fields:
        now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        fields.append('updated_at=?')
        values.append(now)
        values.append(bid_id)
        conn.execute(f"UPDATE bids SET {', '.join(fields)} WHERE id=?", values)
        conn.commit()
    conn.close()


def delete_bid(bid_id):
    conn = get_connection()
    conn.execute("DELETE FROM bids WHERE id=?", (bid_id,))
    conn.commit()
    conn.close()


def get_bid_technicians(bid_id, role):
    conn = get_connection()
    rows = conn.execute(
        """SELECT t.* FROM technicians t
           JOIN bid_technicians bt ON t.id = bt.technician_id
           WHERE bt.bid_id=? AND bt.role=?
           ORDER BY CASE t.grade WHEN '특급' THEN 1 WHEN '고급' THEN 2 WHEN '중급' THEN 3 WHEN '초급' THEN 4 END, t.name""",
        (bid_id, role)
    ).fetchall()
    conn.close()
    return [row_to_dict(r) for r in rows]


def set_bid_technicians(bid_id, role, technician_ids):
    conn = get_connection()
    conn.execute("DELETE FROM bid_technicians WHERE bid_id=? AND role=?", (bid_id, role))
    for tid in technician_ids:
        conn.execute(
            "INSERT INTO bid_technicians (bid_id, technician_id, role) VALUES (?,?,?)",
            (bid_id, tid, role)
        )
    conn.commit()
    conn.close()


def get_bid_projects(bid_id):
    conn = get_connection()
    rows = conn.execute(
        """SELECT p.* FROM projects p
           JOIN bid_projects bp ON p.id = bp.project_id
           WHERE bp.bid_id=?
           ORDER BY p.contract_date DESC""",
        (bid_id,)
    ).fetchall()
    conn.close()
    return [row_to_dict(r) for r in rows]


def set_bid_projects(bid_id, project_ids):
    conn = get_connection()
    conn.execute("DELETE FROM bid_projects WHERE bid_id=?", (bid_id,))
    for pid in project_ids:
        conn.execute(
            "INSERT INTO bid_projects (bid_id, project_id) VALUES (?,?)",
            (bid_id, pid)
        )
    conn.commit()
    conn.close()


def get_extra_items(bid_id):
    conn = get_connection()
    rows = conn.execute(
        "SELECT * FROM extra_items WHERE bid_id=? ORDER BY id",
        (bid_id,)
    ).fetchall()
    conn.close()
    return [row_to_dict(r) for r in rows]


def add_extra_item(bid_id, name, max_score=0.0, actual_score=0.0, description=''):
    conn = get_connection()
    c = conn.cursor()
    c.execute(
        "INSERT INTO extra_items (bid_id, name, max_score, actual_score, description, documents) VALUES (?,?,?,?,?,?)",
        (bid_id, name, max_score, actual_score, description, '[]')
    )
    new_id = c.lastrowid
    conn.commit()
    conn.close()
    return new_id


def update_extra_item(item_id, name=None, max_score=None, actual_score=None,
                      description=None, documents=None):
    conn = get_connection()
    fields = []
    values = []
    if name is not None:
        fields.append('name=?'); values.append(name)
    if max_score is not None:
        fields.append('max_score=?'); values.append(max_score)
    if actual_score is not None:
        fields.append('actual_score=?'); values.append(actual_score)
    if description is not None:
        fields.append('description=?'); values.append(description)
    if documents is not None:
        fields.append('documents=?'); values.append(documents)
    if fields:
        values.append(item_id)
        conn.execute(f"UPDATE extra_items SET {', '.join(fields)} WHERE id=?", values)
        conn.commit()
    conn.close()


def delete_extra_item(item_id):
    conn = get_connection()
    conn.execute("DELETE FROM extra_items WHERE id=?", (item_id,))
    conn.commit()
    conn.close()
