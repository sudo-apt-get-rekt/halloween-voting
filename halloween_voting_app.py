#!/usr/bin/env python3
"""
Halloween Costume Voting App — single-file Flask app with SQLite.
Self-hostable, lightweight, no external DB required.

Features
- Attendee entry: first name, last name, costume name, optional photo upload
- Admin dashboard: view/delete entries, enable/disable voting, manage categories (add/rename/delete)
- Voting wizard: name-only first page (/vote/name), then one category per screen with Back/Next/Finish
- Results tally (admin-only)
- Audit view (admin): see who voted for what + CSV export
- Public "Stats for Nerds": /stats (HTML) and /stats.json
- Duplicate-vote protection by unique voter full name (case-insensitive)
- One-click purge: wipe all data and reseed defaults

Quick Start
1) python3 -m venv .venv && source .venv/bin/activate
2) pip install flask==3.0.0 werkzeug==3.0.1
3) export FLASK_SECRET="change-me" ; export ADMIN_PASSWORD="changeme"
4) python halloween_voting_app.py
5) Browse http://127.0.0.1:5000

Notes
- Uploaded photos saved under ./uploads (created automatically)
- Max photo size 5 MB; allowed extensions: jpg, jpeg, png, gif
- Default categories seeded: Most Realistic Costume, Funniest Costume, Scariest Costume, Best Homemade Costume, Least Effort Costume, Classic Halloween Costume, Cutest Costume
- To reset database manually: stop the app and delete halloween.db and uploads/*
"""
from __future__ import annotations

import datetime as dt
import os
import secrets
import sqlite3
from pathlib import Path
from typing import Optional

from flask import (
    Flask,
    Response,
    abort,
    flash,
    redirect,
    render_template_string,
    request,
    send_from_directory,
    session,
    url_for,
)
from werkzeug.utils import secure_filename

# ---------------------------- CONFIG -----------------------------

APP_NAME = "Halloween Costume Voting"
DB_PATH = Path("halloween.db")
UPLOAD_DIR = Path("uploads")
UPLOAD_DIR.mkdir(exist_ok=True)

MAX_CONTENT_LENGTH = 5 * 1024 * 1024  # 5 MB
ALLOWED_EXTS = {"jpg", "jpeg", "png", "gif"}

EXPECTED_ATTENDEES = int(os.environ.get("EXPECTED_ATTENDEES", "0"))  # 0 = unknown
APP_START_TS = dt.datetime.utcnow()  # uptime origin

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = MAX_CONTENT_LENGTH
app.config["UPLOAD_FOLDER"] = str(UPLOAD_DIR)
app.secret_key = os.environ.get("FLASK_SECRET", secrets.token_hex(16))
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "changeme")

# -------------------------- DB UTILITIES -------------------------


def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON;")
    return conn


def init_db():
    with get_db() as conn:
        cur = conn.cursor()
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS entries (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                first_name TEXT NOT NULL,
                last_name TEXT NOT NULL,
                costume_name TEXT NOT NULL,
                photo_path TEXT,
                created_at TEXT NOT NULL
            );
            """
        )
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS categories (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL UNIQUE,
                enabled INTEGER NOT NULL DEFAULT 1
            );
            """
        )
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS settings (
                key TEXT PRIMARY KEY,
                value TEXT
            );
            """
        )
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS votes (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                voter_first TEXT NOT NULL,
                voter_last TEXT NOT NULL,
                created_at TEXT NOT NULL
            );
            """
        )
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS vote_items (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                vote_id INTEGER NOT NULL,
                category_id INTEGER NOT NULL,
                entry_id INTEGER NOT NULL,
                FOREIGN KEY(vote_id) REFERENCES votes(id) ON DELETE CASCADE,
                FOREIGN KEY(category_id) REFERENCES categories(id) ON DELETE CASCADE,
                FOREIGN KEY(entry_id) REFERENCES entries(id) ON DELETE CASCADE,
                UNIQUE(vote_id, category_id)
            );
            """
        )

        # Indexes for faster admin queries
        cur.execute(
            "CREATE INDEX IF NOT EXISTS idx_votes_name ON votes(LOWER(voter_first), LOWER(voter_last));"
        )
        cur.execute("CREATE INDEX IF NOT EXISTS idx_vote_items_vote ON vote_items(vote_id);")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_vote_items_cat ON vote_items(category_id);")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_vote_items_entry ON vote_items(entry_id);")

        # Seed categories if empty
        cur.execute("SELECT COUNT(*) FROM categories")
        if cur.fetchone()[0] == 0:
            cur.executemany(
                "INSERT OR IGNORE INTO categories(name, enabled) VALUES(?, 1)",
                [
                    ("Most Realistic Costume",),
                    ("Funniest Costume",),
                    ("Scariest Costume",),
                    ("Best Homemade Costume",),
                    ("Least Effort Costume",),
                    ("Classic Halloween Costume",),
                    ("Cutest Costume",),
                ],
            )

        # Seed voting_enabled setting
        cur.execute("SELECT value FROM settings WHERE key='voting_enabled'")
        if cur.fetchone() is None:
            cur.execute("INSERT INTO settings(key, value) VALUES('voting_enabled', '0')")


@app.before_request
def ensure_db():
    init_db()


# ---------------------------- HELPERS ----------------------------


def page(title: str, content_html: str):
    return render_template_string(TPL_BASE, page_title=title, content=content_html)


def voting_closed():
    return page(
        "Voting Closed",
        "<p class='text-center text-lg'>Voting is currently closed. Please check back later.</p>",
    )


def is_admin() -> bool:
    return session.get("admin", False) is True


def require_admin():
    if not is_admin():
        abort(403)


def allowed_file(filename: str) -> bool:
    return "." in filename and filename.rsplit(".", 1)[1].lower() in ALLOWED_EXTS


def get_setting(key: str, default: str = "") -> str:
    with get_db() as conn:
        cur = conn.execute("SELECT value FROM settings WHERE key=?", (key,))
        row = cur.fetchone()
        return row["value"] if row else default


def set_setting(key: str, value: str) -> None:
    with get_db() as conn:
        conn.execute(
            "INSERT INTO settings(key, value) VALUES(?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, value),
        )


def _enabled_categories():
    with get_db() as conn:
        return conn.execute(
            "SELECT id, name FROM categories WHERE enabled=1 ORDER BY name"
        ).fetchall()


def _all_entries():
    with get_db() as conn:
        return conn.execute(
            "SELECT id, first_name, last_name, costume_name, photo_path "
            "FROM entries ORDER BY created_at DESC"
        ).fetchall()


# Short redirect helpers
def to_admin():
    return redirect(url_for("admin"))


def to_home():
    return redirect(url_for("home"))


def to_name():
    return redirect(url_for("vote_name"))


def to_step(i: int):
    return redirect(url_for("vote_step", idx=i))


# ------------------------------ PUBLIC ----------------------------


@app.get("/")
def home():
    voting_enabled = get_setting("voting_enabled", "0") == "1"
    return page(
        "Halloween Costume Voting",
        render_template_string(TPL_HOME, voting_enabled=voting_enabled),
    )


@app.get("/stats")
def public_stats():
    data = stats_gather()
    return page("Stats for Nerds", render_template_string(TPL_ADMIN_STATS, d=data))


@app.get("/stats.json")
def public_stats_json():
    import json

    return Response(
        json.dumps(stats_gather(), indent=2), mimetype="application/json"
    )


# --- Entries ---
@app.get("/entry")
def entry_form():
    return page("Submit Your Costume", render_template_string(TPL_ENTRY_FORM))


@app.post("/entry")
def entry_submit():
    first = request.form.get("first_name", "").strip()
    last = request.form.get("last_name", "").strip()
    costume = request.form.get("costume_name", "").strip()
    if not (first and last and costume):
        flash("Please fill out first name, last name, and costume name.", "error")
        return redirect(url_for("entry_form"))

    photo_path: Optional[str] = None
    file = request.files.get("photo")
    if file and file.filename:
        if not allowed_file(file.filename):
            flash("Invalid photo type. Allowed: jpg, jpeg, png, gif", "error")
            return redirect(url_for("entry_form"))
        fname = secure_filename(file.filename)
        unique = f"{dt.datetime.utcnow().strftime('%Y%m%d%H%M%S')}_{secrets.token_hex(4)}_{fname}"
        save_path = UPLOAD_DIR / unique
        file.save(save_path)
        photo_path = unique

    with get_db() as conn:
        conn.execute(
            "INSERT INTO entries(first_name, last_name, costume_name, photo_path, created_at) "
            "VALUES(?,?,?,?,?)",
            (first, last, costume, photo_path, dt.datetime.utcnow().isoformat()),
        )

    flash("Costume submitted!", "success")
    return redirect(url_for("home"))


@app.get("/uploads/<path:filename>")
def uploaded_file(filename):
    return send_from_directory(app.config["UPLOAD_FOLDER"], filename)


# ------------------------------ VOTING ----------------------------


@app.get("/vote")
def vote_form():
    """Redirect into the wizard (name page first) if voting is enabled; otherwise show 'closed'."""
    if get_setting("voting_enabled", "0") != "1":
        return voting_closed()
    return to_name()


@app.get("/vote/name")
def vote_name():
    if get_setting("voting_enabled", "0") != "1":
        return voting_closed()

    cats = _enabled_categories()
    total_steps = len(cats) + 1  # name page + each category
    voter_first = session.get("voter_first", "")
    voter_last = session.get("voter_last", "")

    return page(
        "Cast Your Votes",
        render_template_string(
            TPL_VOTE_NAME,
            voter_first=voter_first,
            voter_last=voter_last,
            step=1,
            total=total_steps,
        ),
    )


@app.post("/vote/name")
def vote_name_post():
    if get_setting("voting_enabled", "0") != "1":
        abort(403)
    vf = request.form.get("voter_first", "").strip()
    vl = request.form.get("voter_last", "").strip()
    if not vf or not vl:
        flash("Please enter your first and last name.", "error")
        return to_name()
    session["voter_first"] = vf
    session["voter_last"] = vl
    return to_step(0)


@app.get("/vote/step/<int:idx>")
def vote_step(idx: int):
    if get_setting("voting_enabled", "0") != "1":
        return voting_closed()

    cats = _enabled_categories()
    if not cats:
        return page("Cast Your Votes", "<p class='muted'>No categories are enabled.</p>")
    if idx < 0 or idx >= len(cats):
        return to_name()

    entries = _all_entries()
    ballot = session.get("ballot", {})
    total_steps = len(cats) + 1
    step_display = 2 + idx  # 1=name, 2=first category

    return page(
        "Cast Your Votes",
        render_template_string(
            TPL_VOTE_WIZARD,
            categories=cats,
            category=cats[idx],
            entries=entries,
            idx=idx,
            ballot=ballot,
            step=step_display,
            total=total_steps,
        ),
    )


@app.post("/vote/step/<int:idx>")
def vote_step_post(idx: int):
    if get_setting("voting_enabled", "0") != "1":
        abort(403)

    cats = _enabled_categories()
    if not cats:
        flash("No categories are enabled.", "error")
        return to_home()
    if idx < 0 or idx >= len(cats):
        return to_name()

    current_cat = cats[idx]
    choice = request.form.get("choice_entry_id")
    ballot = session.get("ballot", {})
    if choice:
        try:
            ballot[str(current_cat["id"])] = int(choice)
        except ValueError:
            pass
        session["ballot"] = ballot

    nav = request.form.get("nav", "next")
    if nav == "prev":
        return to_step(idx - 1) if idx > 0 else to_name()
    elif nav == "next":
        return to_step(min(len(cats) - 1, idx + 1))
    else:  # finish
        return redirect(url_for("vote_finish"))


@app.get("/vote/finish")
def vote_finish():
    if get_setting("voting_enabled", "0") != "1":
        abort(403)

    voter_first = session.get("voter_first", "").strip()
    voter_last = session.get("voter_last", "").strip()
    if not voter_first or not voter_last:
        flash("Missing voter name; please start again.", "error")
        return to_name()

    with get_db() as conn:
        existing = conn.execute(
            "SELECT id FROM votes WHERE LOWER(voter_first)=LOWER(?) AND LOWER(voter_last)=LOWER(?)",
            (voter_first, voter_last),
        ).fetchone()
        if existing:
            flash("Our records show you've already submitted a ballot.", "error")
            return to_home()

        cur = conn.cursor()
        cur.execute(
            "INSERT INTO votes(voter_first, voter_last, created_at) VALUES(?,?,?)",
            (voter_first, voter_last, dt.datetime.utcnow().isoformat()),
        )
        vote_id = cur.lastrowid

        cats = _enabled_categories()
        ballot = session.get("ballot", {})
        for c in cats:
            key = str(c["id"])
            entry_id = ballot.get(key)
            if entry_id:
                try:
                    cur.execute(
                        "INSERT INTO vote_items(vote_id, category_id, entry_id) VALUES(?,?,?)",
                        (vote_id, c["id"], int(entry_id)),
                    )
                except Exception:
                    pass

    for k in ("voter_first", "voter_last", "ballot"):
        session.pop(k, None)

    flash("Thanks! Your ballot has been recorded.", "success")
    return to_home()


# ------------------------------ ADMIN ----------------------------


@app.get("/admin")
def admin():
    if not is_admin():
        return page("Admin Login", render_template_string(TPL_ADMIN_LOGIN))

    with get_db() as conn:
        entries = conn.execute(
            "SELECT id, first_name, last_name, costume_name, photo_path, created_at "
            "FROM entries ORDER BY created_at DESC"
        ).fetchall()
        cats = conn.execute(
            "SELECT id, name, enabled FROM categories ORDER BY name"
        ).fetchall()
    voting_enabled = get_setting("voting_enabled", "0") == "1"

    return page(
        "Admin Dashboard",
        render_template_string(
            TPL_ADMIN_DASH,
            entries=entries,
            categories=cats,
            voting_enabled=voting_enabled,
        ),
    )


@app.post("/admin/login")
def admin_login():
    pwd = request.form.get("password", "")
    if pwd == ADMIN_PASSWORD:
        session["admin"] = True
        flash("Logged in as admin.", "success")
        return to_admin()
    flash("Invalid password.", "error")
    return to_admin()


@app.post("/admin/logout")
def admin_logout():
    session.clear()
    flash("Logged out.", "success")
    return to_home()


@app.post("/admin/toggle_voting")
def toggle_voting():
    require_admin()
    current = get_setting("voting_enabled", "0")
    set_setting("voting_enabled", "0" if current == "1" else "1")
    flash(f"Voting {'enabled' if current == '0' else 'disabled'}.", "success")
    return to_admin()


@app.post("/admin/category/add")
def category_add():
    require_admin()
    name = request.form.get("name", "").strip()
    if not name:
        flash("Category name cannot be empty.", "error")
        return to_admin()
    try:
        with get_db() as conn:
            conn.execute("INSERT INTO categories(name, enabled) VALUES(?,1)", (name,))
        flash("Category added.", "success")
    except sqlite3.IntegrityError:
        flash("Category already exists.", "error")
    return to_admin()


@app.post("/admin/category/toggle/<int:cat_id>")
def category_toggle(cat_id: int):
    require_admin()
    with get_db() as conn:
        cur = conn.execute("SELECT enabled FROM categories WHERE id=?", (cat_id,)).fetchone()
        if cur is None:
            abort(404)
        conn.execute("UPDATE categories SET enabled=? WHERE id=?", (0 if cur["enabled"] else 1, cat_id))
    flash("Category updated.", "success")
    return to_admin()


@app.post("/admin/category/rename/<int:cat_id>")
def category_rename(cat_id: int):
    require_admin()
    new_name = request.form.get("new_name", "").strip()
    if not new_name:
        flash("New category name cannot be empty.", "error")
        return to_admin()
    try:
        with get_db() as conn:
            conn.execute("UPDATE categories SET name=? WHERE id=?", (new_name, cat_id))
        flash("Category renamed.", "success")
    except sqlite3.IntegrityError:
        flash("A category with that name already exists.", "error")
    return to_admin()


@app.post("/admin/entry/delete/<int:entry_id>")
def entry_delete(entry_id: int):
    require_admin()
    with get_db() as conn:
        row = conn.execute("SELECT photo_path FROM entries WHERE id=?", (entry_id,)).fetchone()
        conn.execute("DELETE FROM entries WHERE id=?", (entry_id,))
    if row and row["photo_path"]:
        try:
            (UPLOAD_DIR / row["photo_path"]).unlink(missing_ok=True)
        except Exception:
            pass
    flash("Entry deleted.", "success")
    return to_admin()


@app.post("/admin/category/delete/<int:cat_id>")
def category_delete(cat_id: int):
    require_admin()
    with get_db() as conn:
        cur = conn.execute("SELECT id FROM categories WHERE id=?", (cat_id,)).fetchone()
        if not cur:
            abort(404)
        conn.execute("DELETE FROM categories WHERE id=?", (cat_id,))
    flash("Category deleted.", "success")
    return to_admin()


@app.get("/admin/results")
def admin_results():
    require_admin()
    tallies = {}
    with get_db() as conn:
        cats = conn.execute("SELECT id, name FROM categories ORDER BY name").fetchall()
        for c in cats:
            rows = conn.execute(
                """
                SELECT e.id as entry_id, e.first_name, e.last_name, e.costume_name, e.photo_path,
                       COUNT(vi.id) as votes
                FROM entries e
                LEFT JOIN vote_items vi
                  ON vi.entry_id = e.id AND vi.category_id = ?
                GROUP BY e.id
                ORDER BY votes DESC, e.costume_name ASC
                """,
                (c["id"],),
            ).fetchall()
            tallies[c["name"]] = rows

    return page("Results", render_template_string(TPL_RESULTS, tallies=tallies))


# -------- Admin Audit (who voted for what) + CSV export ---------


@app.get("/admin/audit")
def admin_audit():
    require_admin()
    with get_db() as conn:
        rows = conn.execute(
            """
            SELECT
              v.id           AS vote_id,
              v.voter_first  AS voter_first,
              v.voter_last   AS voter_last,
              v.created_at   AS voted_at,
              c.name         AS category_name,
              e.costume_name AS costume_name,
              e.first_name   AS entry_first,
              e.last_name    AS entry_last
            FROM votes v
            JOIN vote_items vi ON vi.vote_id   = v.id
            JOIN categories c  ON c.id         = vi.category_id
            JOIN entries e     ON e.id         = vi.entry_id
            ORDER BY v.created_at DESC, c.name ASC, e.costume_name ASC
            """
        ).fetchall()

    return page("Audit — Who Voted For What", render_template_string(TPL_ADMIN_AUDIT, rows=rows))


@app.get("/admin/audit.csv")
def admin_audit_csv():
    require_admin()
    with get_db() as conn:
        rows = conn.execute(
            """
            SELECT
              v.id           AS vote_id,
              v.voter_first  AS voter_first,
              v.voter_last   AS voter_last,
              v.created_at   AS voted_at,
              c.name         AS category_name,
              e.costume_name AS costume_name,
              e.first_name   AS entry_first,
              e.last_name    AS entry_last
            FROM votes v
            JOIN vote_items vi ON vi.vote_id   = v.id
            JOIN categories c  ON c.id         = vi.category_id
            JOIN entries e     ON e.id         = vi.entry_id
            ORDER BY v.created_at DESC, c.name ASC, e.costume_name ASC
            """
        ).fetchall()

    # Build CSV
    import csv
    import io

    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(
        ["vote_id", "voter_first", "voter_last", "voted_at", "category", "costume_name", "entry_first", "entry_last"]
    )
    for r in rows:
        writer.writerow(
            [
                r["vote_id"],
                r["voter_first"],
                r["voter_last"],
                r["voted_at"],
                r["category_name"],
                r["costume_name"],
                r["entry_first"],
                r["entry_last"],
            ]
        )
    csv_bytes = buf.getvalue().encode("utf-8")
    return Response(
        csv_bytes, mimetype="text/csv", headers={"Content-Disposition": "attachment; filename=audit_votes.csv"}
    )


# ------------------------------ STATS CORE ------------------------


def _human_bytes(n: int) -> str:
    units = ["B", "KB", "MB", "GB", "TB"]
    i = 0
    x = float(n)
    while x >= 1024 and i < len(units) - 1:
        x /= 1024.0
        i += 1
    return f"{x:.1f} {units[i]}"


def _dir_size(path: Path) -> int:
    try:
        return sum(p.stat().st_size for p in path.glob("**/*") if p.is_file())
    except Exception:
        return 0


def stats_gather():
    """Compute stats without side-effects. Returns a dict safe to JSON-serialize."""
    now = dt.datetime.utcnow()
    with get_db() as conn:
        # Core counts
        total_entries = conn.execute("SELECT COUNT(*) FROM entries").fetchone()[0]
        total_votes = conn.execute("SELECT COUNT(*) FROM votes").fetchone()[0]
        last_vote_row = conn.execute(
            "SELECT created_at FROM votes ORDER BY created_at DESC LIMIT 1"
        ).fetchone()
        last_vote_at = last_vote_row["created_at"] if last_vote_row else None

        enabled_cats = conn.execute(
            "SELECT id, name FROM categories WHERE enabled=1 ORDER BY name"
        ).fetchall()
        disabled_cats = conn.execute(
            "SELECT id, name FROM categories WHERE enabled=0 ORDER BY name"
        ).fetchall()

        # Per-category participation and leaders
        per_category = []
        for c in conn.execute("SELECT id, name FROM categories ORDER BY name").fetchall():
            part = conn.execute(
                "SELECT COUNT(*) FROM vote_items WHERE category_id=?", (c["id"],)
            ).fetchone()[0]

            leaders = conn.execute(
                """
                SELECT e.costume_name AS name, e.first_name AS first, e.last_name AS last,
                       COUNT(vi.id) AS votes
                FROM entries e
                LEFT JOIN vote_items vi ON vi.entry_id = e.id AND vi.category_id = ?
                GROUP BY e.id
                ORDER BY votes DESC, e.costume_name ASC
                LIMIT 2
                """,
                (c["id"],),
            ).fetchall()
            leader = None
            margin = None
            if leaders:
                top = leaders[0]
                leader = {
                    "costume_name": top["name"],
                    "by": f'{top["first"]} {top["last"]}',
                    "votes": top["votes"],
                }
                if len(leaders) > 1:
                    margin = top["votes"] - leaders[1]["votes"]

            per_category.append(
                {
                    "id": c["id"],
                    "name": c["name"],
                    "participation": part,
                    "leader": leader,
                    "lead_margin": margin,
                }
            )

        # Timeline: hourly buckets (UTC)
        entries_by_hour = conn.execute(
            """
            SELECT substr(created_at, 1, 13) AS hour, COUNT(*) AS count
            FROM entries
            GROUP BY hour
            ORDER BY hour
            """
        ).fetchall()
        votes_by_hour = conn.execute(
            """
            SELECT substr(created_at, 1, 13) AS hour, COUNT(*) AS count
            FROM votes
            GROUP BY hour
            ORDER BY hour
            """
        ).fetchall()

        # Photo stats
        photos = conn.execute(
            "SELECT photo_path FROM entries WHERE photo_path IS NOT NULL AND photo_path <> ''"
        ).fetchall()
        photo_files = [UPLOAD_DIR / r["photo_path"] for r in photos if r["photo_path"]]
        sizes = []
        for p in photo_files:
            try:
                sizes.append(p.stat().st_size)
            except Exception:
                pass
        photos_with = len(photo_files)
        avg_photo_size = sum(sizes) / len(sizes) if sizes else 0

    # Storage info
    db_size = DB_PATH.stat().st_size if DB_PATH.exists() else 0
    uploads_size = _dir_size(UPLOAD_DIR)

    # Uptime
    uptime = (now - APP_START_TS).total_seconds()

    # Progress
    progress = None
    if EXPECTED_ATTENDEES > 0:
        progress = min(100, round(total_votes / EXPECTED_ATTENDEES * 100, 1))

    return {
        "now_utc": now.isoformat(timespec="seconds"),
        "voting_enabled": get_setting("voting_enabled", "0") == "1",
        "expected_attendees": EXPECTED_ATTENDEES,
        "progress_pct": progress,
        "counts": {
            "entries": total_entries,
            "votes": total_votes,
            "categories_enabled": len(enabled_cats),
            "categories_disabled": len(disabled_cats),
        },
        "last_vote_at": last_vote_at,
        "per_category": per_category,
        "timeline": {
            "entries_hourly": [{"hour": r["hour"], "count": r["count"]} for r in entries_by_hour],
            "votes_hourly": [{"hour": r["hour"], "count": r["count"]} for r in votes_by_hour],
        },
        "storage": {
            "db_size_bytes": db_size,
            "db_size_human": _human_bytes(db_size),
            "uploads_size_bytes": uploads_size,
            "uploads_size_human": _human_bytes(uploads_size),
            "avg_photo_size_bytes": int(avg_photo_size),
            "avg_photo_size_human": _human_bytes(int(avg_photo_size)),
            "photos_with": photos_with,
        },
        "uptime_seconds": int(uptime),
    }


# ------------------------------ PURGE -----------------------------


@app.post("/admin/purge")
def admin_purge():
    """Danger zone: wipe all entries, votes, and categories; reset settings; delete uploaded photos; reseed defaults."""
    require_admin()

    # Delete uploaded images
    try:
        for p in UPLOAD_DIR.iterdir():
            if p.is_file():
                try:
                    p.unlink()
                except Exception:
                    pass
    except Exception:
        pass

    # Clear tables in safe order
    with get_db() as conn:
        cur = conn.cursor()
        cur.execute("DELETE FROM vote_items;")
        cur.execute("DELETE FROM votes;")
        cur.execute("DELETE FROM entries;")
        cur.execute("DELETE FROM categories;")
        cur.execute("DELETE FROM settings;")

    # Reseed
    init_db()
    flash("All data purged. Defaults re-seeded and uploads cleared.", "success")
    return to_admin()


# ---------------------------- TEMPLATES ---------------------------

TPL_BASE = r"""
<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>{{ page_title }}</title>
  <style>
    :root { --bg:#0e0f12; --card:#16181d; --ink:#eaeef7; --muted:#aab3c5; --accent:#ff8c00; }
    html,body{margin:0;padding:0;background:var(--bg);color:var(--ink);font-family:system-ui,-apple-system,Segoe UI,Roboto,Inter,Arial,sans-serif;}
    a{color:var(--accent);text-decoration:none}
    .container{max-width:1000px;margin:0 auto;padding:24px}
    header{display:flex;gap:16px;align-items:center;justify-content:space-between;margin-bottom:24px}
    .brand{font-weight:700;font-size:1.25rem}
    nav a{margin-right:12px}
    .card{background:var(--card);border-radius:16px;padding:16px;box-shadow:0 10px 24px rgba(0,0,0,.35);}
    .grid{display:grid;gap:16px}
    .grid.cols-2{grid-template-columns:repeat(2,minmax(0,1fr))}
    .grid.cols-3{grid-template-columns:repeat(3,minmax(0,1fr))}
    .btn{display:inline-block;background:var(--accent);color:black;padding:10px 14px;border-radius:10px;font-weight:600;border:none;cursor:pointer}
    .btn.secondary{background:#2a2f39;color:var(--ink)}
    .btn.danger{background:#d84a4a;color:white}
    input,select{
      width:100%;
      padding:10px;
      border-radius:10px;
      border:1px solid #2b2f3a;
      background:#0f1115;
      color:var(--ink);
      box-sizing:border-box;
    }
    label{font-size:.9rem;color:var(--muted)}
    .muted{color:var(--muted)}
    .text-center{text-align:center}
    .text-lg{font-size:1.15rem}
    .mb-2{margin-bottom:8px}.mb-3{margin-bottom:12px}.mb-4{margin-bottom:16px}.mb-6{margin-bottom:24px}
    img.thumb{width:100%;height:180px;object-fit:cover;border-radius:12px;border:1px solid #2b2f3a;background:#0b0c10}
    .badge{display:inline-block;padding:4px 10px;border-radius:999px;background:#242833;color:var(--muted);font-size:.8rem}
    .row{display:flex;gap:12px;align-items:center;flex-wrap:wrap}
    .flash{padding:12px;border-radius:10px;margin-bottom:12px}
    .flash.success{background:#1c3b24;color:#c7f7d1}
    .flash.error{background:#3b1c1c;color:#f7c7c7}
    footer{opacity:.7;margin-top:28px;font-size:.9rem}

    /* Shared utility classes */
    .progress{height:10px;background:#222;border-radius:999px;overflow:hidden;margin-top:6px}
    .progress > div{height:100%;background:var(--accent)}
    .notice-box{margin:12px 0 18px 0;padding:12px 14px;background:#1b1e25;border:1px solid #2b2f3a;border-radius:12px}
    .category-section{margin:1.0em 0;padding:1em;background:#222;border-radius:12px}
    .entries-grid{display:grid;gap:16px;grid-template-columns:repeat(2,minmax(0,1fr))}
    @media (min-width:1024px){ .entries-grid{grid-template-columns:repeat(3,minmax(0,1fr))} }
  </style>
</head>
<body>
  <div class="container">
    <header>
      <div class="brand">🎃 {{page_title}}</div>
      <nav>
        <a href="{{ url_for('home') }}">Home</a>
        <a href="{{ url_for('entry_form') }}">Submit Costume</a>
        <a href="{{ url_for('vote_form') }}">Vote</a>
        <a href="{{ url_for('public_stats') }}">Stats</a>
        <a href="{{ url_for('admin') }}">Admin</a>
      </nav>
    </header>

    {% with messages = get_flashed_messages(with_categories=true) %}
      {% if messages %}
        {% for category, message in messages %}
          <div class="flash {{category}}">{{ message }}</div>
        {% endfor %}
      {% endif %}
    {% endwith %}

    <div class="grid">
      <div class="card">{{ content|safe }}</div>
    </div>

    <footer class="muted">Halloween Voting App - almn.io</footer>
  </div>
</body>
</html>
"""

TPL_HOME = r"""
<h2>Welcome!</h2>
<p>Use this app to submit your costume and vote for awards during the party.</p>
<div class="row mb-6">
  <span class="badge">Voting status: <strong>{{ 'OPEN' if voting_enabled else 'CLOSED' }}</strong></span>
  <a class="btn" href="{{ url_for('entry_form') }}">Submit Costume</a>
  <a class="btn secondary" href="{{ url_for('vote_form') }}">Go to Voting</a>
  <a class="btn" href="{{ url_for('public_stats') }}">Stats for Nerds</a>
</div>
"""

TPL_ENTRY_FORM = r"""
<h3>Submit Your Costume</h3>
<form action="{{ url_for('entry_submit') }}" method="post" enctype="multipart/form-data">
  <div class="grid cols-2">
    <div>
      <label>First Name</label>
      <input name="first_name" required />
    </div>
    <div>
      <label>Last Name</label>
      <input name="last_name" required />
    </div>
  </div>
  <div class="mb-3">
    <label>Costume Name</label>
    <input name="costume_name" required />
  </div>
  <div class="mb-4">
    <label>Photo (optional) — JPG/PNG/GIF up to 5 MB</label>
    <input type="file" name="photo" accept="image/*" />
  </div>
  <button class="btn" type="submit">Submit</button>
</form>
"""

# Name-only first page (with inline instructions box)
TPL_VOTE_NAME = r"""
<h3>Cast Your Votes</h3>

<div style="margin: 12px 0;">
  <div class="muted">Step {{ step }} of {{ total }}</div>
  {% set pct = (step / total * 100)|round(0, 'floor') %}
  <div class="progress"><div style="width:{{ pct }}%"></div></div>
</div>

<div class="notice-box">
  <strong>Instructions:</strong> Enter your name to begin. On the next pages, pick one costume per category.
  You can go back to change selections before submitting.
</div>

<style>
  .form-name-grid{display:grid; grid-template-columns:1fr; gap:16px;}
  @media (min-width:720px){ .form-name-grid{ grid-template-columns:1fr 1fr; } }
</style>

<form action="{{ url_for('vote_name_post') }}" method="post">
  <div class="form-name-grid mb-4">
    <div>
      <label>Your First Name</label>
      <input name="voter_first" value="{{ voter_first or '' }}" required />
    </div>
    <div>
      <label>Your Last Name</label>
      <input name="voter_last" value="{{ voter_last or '' }}" required />
    </div>
  </div>

  <div class="row" style="justify-content:flex-end">
    <button class="btn" name="nav" value="next" type="submit">Start Voting →</button>
  </div>
</form>
"""

# Wizard template: one category per step
TPL_VOTE_WIZARD = r"""
<h3>Cast Your Votes</h3>

<div style="margin: 12px 0;">
  <div class="muted">Category {{ step - 1 }} of {{ total - 1 }}</div>
  {% set pct = (step / total * 100)|round(0, 'floor') %}
  <div class="progress"><div style="width:{{ pct }}%"></div></div>
</div>

<form action="{{ url_for('vote_step_post', idx=idx) }}" method="post">
  <div class="category-section">
    <h2 style="font-size:1.2em; color:#ffd700; border-bottom:1px solid #555; padding-bottom:0.3em; margin-top:0;">
      {{ category.name }}
    </h2>
    <div class="entries-grid">
      {% for e in entries %}
        <label class="card" style="cursor:pointer">
          <input type="radio" name="choice_entry_id" value="{{e.id}}" style="margin-bottom:8px"
                 {% if ballot.get(category.id|string) == e.id %}checked{% endif %} />
          {% if e.photo_path %}
            <img class="thumb" src="{{ url_for('uploaded_file', filename=e.photo_path) }}" alt="{{ e.costume_name }}" />
          {% else %}
            <div class="thumb" style="display:flex;align-items:center;justify-content:center">No Photo</div>
          {% endif %}
          <div class="mb-2"><strong>{{ e.costume_name }}</strong></div>
          <div class="muted">By {{ e.first_name }} {{ e.last_name }}</div>
        </label>
      {% endfor %}
    </div>
  </div>

  <div class="row" style="justify-content:space-between">
    <button class="btn secondary" name="nav" value="prev" type="submit">← Back</button>
    {% if idx + 1 < categories|length %}
      <button class="btn" name="nav" value="next" type="submit">Next →</button>
    {% else %}
      <button class="btn" name="nav" value="finish" type="submit">Submit Ballot ✅</button>
    {% endif %}
  </div>
</form>
"""

TPL_ADMIN_LOGIN = r"""
<form action="{{ url_for('admin_login') }}" method="post">
  <div class="mb-3">
    <label>Password</label>
    <input name="password" type="password" required />
  </div>
  <button class="btn" type="submit">Log In</button>
</form>
<p class="muted mb-0">Login using administrator credentials.</p>
"""

TPL_ADMIN_DASH = r"""
<div class="row mb-4">
  <form action="{{ url_for('toggle_voting') }}" method="post">
    <button class="btn" type="submit">{{ 'Disable' if voting_enabled else 'Enable' }} Voting</button>
  </form>
  <form action="{{ url_for('admin_logout') }}" method="post">
    <button class="btn secondary" type="submit">Log Out</button>
  </form>
  <a class="btn secondary" href="{{ url_for('admin_results') }}">View Results</a>
  <a class="btn" href="{{ url_for('admin_audit') }}">Audit: Who Voted</a>
  <a class="btn" href="{{ url_for('public_stats') }}">Stats</a>
  <form action="{{ url_for('admin_purge') }}" method="post" onsubmit="return confirm('Purge ALL data (entries, votes, categories) and delete uploaded photos? This cannot be undone.');">
    <button class="btn danger" type="submit">Purge All Data</button>
  </form>
</div>

<h3>Categories</h3>
<form class="mb-3" action="{{ url_for('category_add') }}" method="post">
  <div class="row">
    <input name="name" placeholder="Add new category" required />
    <button class="btn" type="submit">Add</button>
  </div>
</form>
<div class="grid cols-3 mb-6">
  {% for c in categories %}
    <div class="card">
      <div class="row" style="justify-content:space-between;align-items:center">
        <strong>{{ c.name }}</strong>
        <form action="{{ url_for('category_toggle', cat_id=c.id) }}" method="post">
          <button class="btn secondary" type="submit">{{ 'Disable' if c.enabled else 'Enable' }}</button>
        </form>
      </div>
      <div class="muted mb-2">Status: {{ 'Enabled' if c.enabled else 'Disabled' }}</div>

      <form class="row mb-2" action="{{ url_for('category_rename', cat_id=c.id) }}" method="post">
        <input name="new_name" value="{{ c.name }}" required />
        <button class="btn" type="submit">Rename</button>
      </form>

      <form action="{{ url_for('category_delete', cat_id=c.id) }}" method="post" onsubmit="return confirm('Delete this category? All associated votes for this category will be removed.');">
        <button class="btn danger" type="submit">Delete</button>
      </form>
    </div>
  {% endfor %}
</div>

<h3>Entries</h3>
<div class="grid cols-3">
  {% for e in entries %}
    <div class="card">
      {% if e.photo_path %}
        <img class="thumb" src="{{ url_for('uploaded_file', filename=e.photo_path) }}" alt="{{ e.costume_name }}" />
      {% else %}
        <div class="thumb" style="display:flex;align-items:center;justify-content:center">No Photo</div>
      {% endif %}
      <div class="mb-2"><strong>{{ e.costume_name }}</strong></div>
      <div class="muted">By {{ e.first_name }} {{ e.last_name }}</div>
      <form class="mt-2" action="{{ url_for('entry_delete', entry_id=e.id) }}" method="post" onsubmit="return confirm('Delete this entry?')">
        <button class="btn danger" type="submit">Delete</button>
      </form>
    </div>
  {% endfor %}
</div>
"""

TPL_RESULTS = r"""
<h3>Live Results</h3>
{% for cat_name, rows in tallies.items() %}
  <div class="card mb-3">
    <h4>{{ cat_name }}</h4>
    {% if not rows %}
      <p class="muted">No entries.</p>
    {% else %}
      <div>
        {% for r in rows %}
          <div class="row" style="justify-content:space-between;align-items:center;border-bottom:1px solid #262a34;padding:10px 0">
            <div class="row" style="gap:10px;align-items:center">
              {% if r.photo_path %}
                <img src="{{ url_for('uploaded_file', filename=r.photo_path) }}" alt="{{ r.costume_name }}" style="width:56px;height:56px;object-fit:cover;border-radius:8px;border:1px solid #2b2f3a" />
              {% endif %}
              <div>
                <div><strong>{{ r.costume_name }}</strong></div>
                <div class="muted">by {{ r.first_name }} {{ r.last_name }}</div>
              </div>
            </div>
            <div><span class="badge">{{ r.votes }} vote{{ '' if r.votes == 1 else 's' }}</span></div>
          </div>
        {% endfor %}
      </div>
    {% endif %}
  </div>
{% endfor %}
"""

TPL_ADMIN_AUDIT = r"""
<h3>Audit — Who Voted For What</h3>
<div class="row mb-4">
  <a class="btn secondary" href="{{ url_for('admin') }}">← Back to Admin</a>
  <a class="btn" href="{{ url_for('admin_audit_csv') }}">Download CSV</a>
</div>

{% if not rows %}
  <p class="muted">No ballots have been submitted yet.</p>
{% else %}
  {% set current_vote = None %}
  {% for r in rows %}
    {% if current_vote != r.vote_id %}
      {% if not loop.first %}
        </div>
      </div>
      {% endif %}

      <div class="card mb-3">
        <div class="row" style="justify-content:space-between; align-items:center;">
          <div><strong>Voter:</strong> {{ r.voter_first }} {{ r.voter_last }}</div>
          <div class="muted">Ballot ID {{ r.vote_id }} • {{ r.voted_at }}</div>
        </div>
        <div style="margin-top:10px;">
      {% set current_vote = r.vote_id %}
    {% endif %}

    <div class="row" style="justify-content:space-between; align-items:center; border-bottom:1px solid #262a34; padding:8px 0;">
      <div class="muted">{{ r.category_name }}</div>
      <div>
        <strong>{{ r.costume_name }}</strong>
        <span class="muted">by {{ r.entry_first }} {{ r.entry_last }}</span>
      </div>
    </div>

    {% if loop.last %}
        </div>
      </div>
    {% endif %}
  {% endfor %}
{% endif %}
"""

# Reuse this template for public /stats
TPL_ADMIN_STATS = r"""
<h3>Stats for Nerds</h3>
<div class="row mb-4">
  <a class="btn" href="{{ url_for('public_stats_json') }}">View JSON</a>
</div>

<div class="grid cols-3 mb-6">
  <div class="card">
    <strong>Overview</strong>
    <div class="muted">Now (UTC)</div>
    <div class="mb-2">{{ d.now_utc }}</div>

    <div class="muted">Voting</div>
    <div class="mb-2"><span class="badge">{{ 'ENABLED' if d.voting_enabled else 'DISABLED' }}</span></div>

    <div class="muted">Expected Attendees</div>
    <div class="mb-2">{{ d.expected_attendees or 'Unknown' }}</div>

    <div class="muted">Progress</div>
    <div class="mb-2">
      {% if d.progress_pct is not none %}
        <span class="badge">{{ d.progress_pct }}%</span>
      {% else %}
        <span class="badge">n/a</span>
      {% endif %}
    </div>

    <div class="muted">Uptime</div>
    <div>{{ (d.uptime_seconds // 3600) }}h {{ (d.uptime_seconds // 60) % 60 }}m</div>
  </div>

  <div class="card">
    <strong>Counts</strong>
    <div class="row"><div class="muted">Entries</div><div class="badge">{{ d.counts.entries }}</div></div>
    <div class="row"><div class="muted">Votes</div><div class="badge">{{ d.counts.votes }}</div></div>
    <div class="row"><div class="muted">Categories (enabled)</div><div class="badge">{{ d.counts.categories_enabled }}</div></div>
    <div class="row"><div class="muted">Categories (disabled)</div><div class="badge">{{ d.counts.categories_disabled }}</div></div>
    <div class="muted" style="margin-top:10px;">Last ballot</div>
    <div>{{ d.last_vote_at or 'No ballots yet' }}</div>
  </div>

  <div class="card">
    <strong>Storage</strong>
    <div class="row"><div class="muted">DB size</div><div class="badge">{{ d.storage.db_size_human }}</div></div>
    <div class="row"><div class="muted">Uploads size</div><div class="badge">{{ d.storage.uploads_size_human }}</div></div>
    <div class="row"><div class="muted">Photos (count)</div><div class="badge">{{ d.storage.photos_with }}</div></div>
    <div class="row"><div class="muted">Avg photo size</div><div class="badge">{{ d.storage.avg_photo_size_human }}</div></div>
  </div>
</div>

<h4>Per-Category</h4>
<div class="grid cols-3 mb-6">
  {% for c in d.per_category %}
    <div class="card">
      <div class="row" style="justify-content:space-between;">
        <strong>{{ c.name }}</strong>
        <span class="badge">Ballots: {{ c.participation }}</span>
      </div>
      {% if c.leader %}
        <div class="muted" style="margin-top:6px;">Leader</div>
        <div><strong>{{ c.leader.costume_name }}</strong> <span class="muted">by {{ c.leader.by }}</span></div>
        <div class="row"><div class="muted">Votes</div><div class="badge">{{ c.leader.votes }}</div></div>
        {% if c.lead_margin is not none %}
          <div class="row"><div class="muted">Lead margin</div><div class="badge">{{ c.lead_margin }}</div></div>
        {% endif %}
      {% else %}
        <div class="muted">No entries.</div>
      {% endif %}
    </div>
  {% endfor %}
</div>

<h4>Timeline (UTC)</h4>
<div class="grid cols-2">
  <div class="card">
    <strong>Entries per hour</strong>
    {% if not d.timeline.entries_hourly %}
      <div class="muted">No data</div>
    {% else %}
      <div>
        {% for r in d.timeline.entries_hourly %}
          <div class="row" style="justify-content:space-between;border-bottom:1px solid #262a34;padding:6px 0;">
            <div class="muted">{{ r.hour }}:00</div>
            <div class="badge">{{ r.count }}</div>
          </div>
        {% endfor %}
      </div>
    {% endif %}
  </div>

  <div class="card">
    <strong>Votes per hour</strong>
    {% if not d.timeline.votes_hourly %}
      <div class="muted">No data</div>
    {% else %}
      <div>
        {% for r in d.timeline.votes_hourly %}
          <div class="row" style="justify-content:space-between;border-bottom:1px solid #262a34;padding:6px 0;">
            <div class="muted">{{ r.hour }}:00</div>
            <div class="badge">{{ r.count }}</div>
          </div>
        {% endfor %}
      </div>
    {% endif %}
  </div>
</div>
"""

if __name__ == "__main__":
    app.run(debug=True, host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))