#!/usr/bin/env python3
"""
Halloween Costume Voting App — single-file Flask app with SQLite.
Self-hostable, lightweight, no external DB required.

Features
- Attendee entry: first name, last name, costume name, optional photo upload
- Admin dashboard: view/delete entries, enable/disable voting, manage categories (add/rename/delete)
- Voting wizard (when enabled): voter name + one category per screen with Back/Next/Finish
- Results tally (admin-only)
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
import os
import sqlite3
import secrets
import datetime as dt
from pathlib import Path
from typing import Optional

from flask import (
    Flask,
    request,
    redirect,
    url_for,
    render_template_string,
    flash,
    send_from_directory,
    session,
    abort,
)
from werkzeug.utils import secure_filename

APP_NAME = "Halloween Costume Voting"
DB_PATH = Path("halloween.db")
UPLOAD_DIR = Path("uploads")
UPLOAD_DIR.mkdir(exist_ok=True)

MAX_CONTENT_LENGTH = 5 * 1024 * 1024  # 5 MB
ALLOWED_EXTS = {"jpg", "jpeg", "png", "gif"}

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = MAX_CONTENT_LENGTH
app.config["UPLOAD_FOLDER"] = str(UPLOAD_DIR)
app.secret_key = os.environ.get("FLASK_SECRET", secrets.token_hex(16))
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "changeme")

# -------------------------- DB UTILITIES ---------------------------

def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    # enable cascades for safety
    conn.execute("PRAGMA foreign_keys = ON;")
    return conn

def init_db():
    conn = get_db()
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

    # seed categories if empty
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

    # seed voting_enabled setting
    cur.execute("SELECT value FROM settings WHERE key='voting_enabled'")
    if cur.fetchone() is None:
        cur.execute("INSERT INTO settings(key, value) VALUES('voting_enabled', '0')")

    conn.commit()
    conn.close()

@app.before_request
def ensure_db():
    init_db()

# ---------------------------- HELPERS ------------------------------

def is_admin() -> bool:
    return session.get("admin", False) is True

def require_admin():
    if not is_admin():
        abort(403)

def allowed_file(filename: str) -> bool:
    if "." not in filename:
        return False
    ext = filename.rsplit(".", 1)[1].lower()
    return ext in ALLOWED_EXTS

def get_setting(key: str, default: str = "") -> str:
    conn = get_db()
    cur = conn.execute("SELECT value FROM settings WHERE key=?", (key,))
    row = cur.fetchone()
    conn.close()
    return row["value"] if row else default

def set_setting(key: str, value: str) -> None:
    conn = get_db()
    conn.execute(
        "INSERT INTO settings(key, value) VALUES(?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (key, value),
    )
    conn.commit()
    conn.close()

def _enabled_categories():
    conn = get_db()
    rows = conn.execute("SELECT id, name FROM categories WHERE enabled=1 ORDER BY name").fetchall()
    conn.close()
    return rows

def _all_entries():
    conn = get_db()
    rows = conn.execute(
        "SELECT id, first_name, last_name, costume_name, photo_path FROM entries ORDER BY created_at DESC"
    ).fetchall()
    conn.close()
    return rows

# ------------------------------ ROUTES -----------------------------

@app.get("/")
def home():
    voting_enabled = get_setting("voting_enabled", "0") == "1"
    return render_template_string(
        TPL_BASE,
        **{
            "page_title": APP_NAME,
            "content": render_template_string(
                TPL_HOME,
                voting_enabled=voting_enabled,
            ),
        },
    )

# --- Entries ---
@app.get("/entry")
def entry_form():
    return render_template_string(
        TPL_BASE,
        **{
            "page_title": "Submit Your Costume",
            "content": render_template_string(TPL_ENTRY_FORM),
        },
    )

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
        # prevent collisions
        unique = f"{dt.datetime.utcnow().strftime('%Y%m%d%H%M%S')}_{secrets.token_hex(4)}_{fname}"
        save_path = UPLOAD_DIR / unique
        file.save(save_path)
        photo_path = unique

    conn = get_db()
    conn.execute(
        "INSERT INTO entries(first_name, last_name, costume_name, photo_path, created_at) VALUES(?,?,?,?,?)",
        (first, last, costume, photo_path, dt.datetime.utcnow().isoformat()),
    )
    conn.commit()
    conn.close()
    flash("Costume submitted!", "success")
    return redirect(url_for("home"))

@app.get("/uploads/<path:filename>")
def uploaded_file(filename):
    return send_from_directory(app.config["UPLOAD_FOLDER"], filename)

# --- Voting (Wizard) ---
@app.get("/vote")
def vote_form():
    """Redirect into the wizard if voting is enabled; otherwise show 'closed'."""
    if get_setting("voting_enabled", "0") != "1":
        return render_template_string(
            TPL_BASE,
            **{
                "page_title": "Voting Closed",
                "content": "<p class='text-center text-lg'>Voting is currently closed. Please check back later.</p>",
            },
        )
    # start at first category
    return redirect(url_for("vote_step", idx=0))

@app.get("/vote/step/<int:idx>")
def vote_step(idx: int):
    if get_setting("voting_enabled", "0") != "1":
        return render_template_string(
            TPL_BASE,
            **{
                "page_title": "Voting Closed",
                "content": "<p class='text-center text-lg'>Voting is currently closed. Please check back later.</p>",
            },
        )

    cats = _enabled_categories()
    if not cats:
        return render_template_string(
            TPL_BASE,
            **{
                "page_title": "Cast Your Votes",
                "content": "<p class='muted'>No categories are enabled.</p>",
            },
        )
    if idx < 0 or idx >= len(cats):
        return redirect(url_for("vote_step", idx=0))

    entries = _all_entries()
    ballot = session.get("ballot", {})  # {category_id(str): entry_id}
    voter_first = session.get("voter_first", "")
    voter_last = session.get("voter_last", "")

    return render_template_string(
        TPL_BASE,
        **{
            "page_title": "Cast Your Votes",
            "content": render_template_string(
                TPL_VOTE_WIZARD,
                categories=cats,
                category=cats[idx],
                entries=entries,
                idx=idx,
                ballot=ballot,
                voter_first=voter_first,
                voter_last=voter_last,
            ),
        },
    )

@app.post("/vote/step/<int:idx>")
def vote_step_post(idx: int):
    if get_setting("voting_enabled", "0") != "1":
        abort(403)

    # Update voter name on first step
    if idx == 0:
        vf = request.form.get("voter_first", "").strip()
        vl = request.form.get("voter_last", "").strip()
        if not vf or not vl:
            flash("Please enter your first and last name.", "error")
            return redirect(url_for("vote_step", idx=idx))
        session["voter_first"] = vf
        session["voter_last"] = vl

    cats = _enabled_categories()
    if not cats:
        flash("No categories are enabled.", "error")
        return redirect(url_for("home"))
    if idx < 0 or idx >= len(cats):
        return redirect(url_for("vote_step", idx=0))

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
        return redirect(url_for("vote_step", idx=max(0, idx - 1)))
    elif nav == "next":
        return redirect(url_for("vote_step", idx=min(len(cats) - 1, idx + 1)))
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
        return redirect(url_for("vote_step", idx=0))

    conn = get_db()
    existing = conn.execute(
        "SELECT id FROM votes WHERE LOWER(voter_first)=LOWER(?) AND LOWER(voter_last)=LOWER(?)",
        (voter_first, voter_last),
    ).fetchone()
    if existing:
        conn.close()
        flash("Our records show you've already submitted a ballot.", "error")
        return redirect(url_for("home"))

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

    conn.commit()
    conn.close()

    # Clear session ballot
    for k in ("voter_first", "voter_last", "ballot"):
        session.pop(k, None)

    flash("Thanks! Your ballot has been recorded.", "success")
    return redirect(url_for("home"))

# --- Admin ---
@app.get("/admin")
def admin():
    if not is_admin():
        return render_template_string(
            TPL_BASE,
            **{
                "page_title": "Admin Login",
                "content": render_template_string(TPL_ADMIN_LOGIN),
            },
        )

    conn = get_db()
    entries = conn.execute(
        "SELECT id, first_name, last_name, costume_name, photo_path, created_at FROM entries ORDER BY created_at DESC"
    ).fetchall()
    cats = conn.execute(
        "SELECT id, name, enabled FROM categories ORDER BY name"
    ).fetchall()
    voting_enabled = get_setting("voting_enabled", "0") == "1"
    conn.close()

    return render_template_string(
        TPL_BASE,
        **{
            "page_title": "Admin Dashboard",
            "content": render_template_string(
                TPL_ADMIN_DASH,
                entries=entries,
                categories=cats,
                voting_enabled=voting_enabled,
            ),
        },
    )

@app.post("/admin/login")
def admin_login():
    pwd = request.form.get("password", "")
    if pwd == ADMIN_PASSWORD:
        session["admin"] = True
        flash("Logged in as admin.", "success")
        return redirect(url_for("admin"))
    flash("Invalid password.", "error")
    return redirect(url_for("admin"))

@app.post("/admin/logout")
def admin_logout():
    session.clear()
    flash("Logged out.", "success")
    return redirect(url_for("home"))

@app.post("/admin/toggle_voting")
def toggle_voting():
    require_admin()
    current = get_setting("voting_enabled", "0")
    new_val = "0" if current == "1" else "1"
    set_setting("voting_enabled", new_val)
    flash(f"Voting {'enabled' if new_val=='1' else 'disabled'}.", "success")
    return redirect(url_for("admin"))

@app.post("/admin/category/add")
def category_add():
    require_admin()
    name = request.form.get("name", "").strip()
    if not name:
        flash("Category name cannot be empty.", "error")
        return redirect(url_for("admin"))
    conn = get_db()
    try:
        conn.execute("INSERT INTO categories(name, enabled) VALUES(?,1)", (name,))
        conn.commit()
        flash("Category added.", "success")
    except sqlite3.IntegrityError:
        flash("Category already exists.", "error")
    finally:
        conn.close()
    return redirect(url_for("admin"))

@app.post("/admin/category/toggle/<int:cat_id>")
def category_toggle(cat_id: int):
    require_admin()
    conn = get_db()
    cur = conn.execute(
        "SELECT enabled FROM categories WHERE id=?", (cat_id,)
    ).fetchone()
    if cur is None:
        conn.close()
        abort(404)
    new_val = 0 if cur["enabled"] else 1
    conn.execute("UPDATE categories SET enabled=? WHERE id=?", (new_val, cat_id))
    conn.commit()
    conn.close()
    flash("Category updated.", "success")
    return redirect(url_for("admin"))

@app.post("/admin/category/rename/<int:cat_id>")
def category_rename(cat_id: int):
    require_admin()
    new_name = request.form.get("new_name", "").strip()
    if not new_name:
        flash("New category name cannot be empty.", "error")
        return redirect(url_for("admin"))
    conn = get_db()
    try:
        conn.execute("UPDATE categories SET name=? WHERE id=?", (new_name, cat_id))
        conn.commit()
        flash("Category renamed.", "success")
    except sqlite3.IntegrityError:
        flash("A category with that name already exists.", "error")
    finally:
        conn.close()
    return redirect(url_for("admin"))

@app.post("/admin/entry/delete/<int:entry_id>")
def entry_delete(entry_id: int):
    require_admin()
    conn = get_db()
    row = conn.execute(
        "SELECT photo_path FROM entries WHERE id=?", (entry_id,)
    ).fetchone()
    conn.execute("DELETE FROM entries WHERE id=?", (entry_id,))
    conn.commit()
    conn.close()

    if row and row["photo_path"]:
        try:
            (UPLOAD_DIR / row["photo_path"]).unlink(missing_ok=True)
        except Exception:
            pass

    flash("Entry deleted.", "success")
    return redirect(url_for("admin"))

@app.post("/admin/category/delete/<int:cat_id>")
def category_delete(cat_id: int):
    require_admin()
    conn = get_db()
    cur = conn.execute("SELECT id FROM categories WHERE id=?", (cat_id,)).fetchone()
    if not cur:
        conn.close()
        abort(404)
    conn.execute("DELETE FROM categories WHERE id=?", (cat_id,))
    conn.commit()
    conn.close()
    flash("Category deleted.", "success")
    return redirect(url_for("admin"))

@app.get("/admin/results")
def admin_results():
    require_admin()
    conn = get_db()
    cats = conn.execute("SELECT id, name FROM categories ORDER BY name").fetchall()
    tallies = {}
    for c in cats:
        rows = conn.execute(
            """
            SELECT e.id as entry_id, e.first_name, e.last_name, e.costume_name, e.photo_path, COUNT(vi.id) as votes
            FROM entries e
            LEFT JOIN vote_items vi ON vi.entry_id = e.id AND vi.category_id = ?
            GROUP BY e.id
            ORDER BY votes DESC, e.costume_name ASC
            """,
            (c["id"],),
        ).fetchall()
        tallies[c["name"]] = rows
    conn.close()

    return render_template_string(
        TPL_BASE,
        **{
            "page_title": "Results",
            "content": render_template_string(TPL_RESULTS, tallies=tallies),
        },
    )

@app.post("/admin/purge")
def admin_purge():
    """Danger zone: wipe all entries, votes, and categories; reset settings; delete uploaded photos; reseed defaults."""
    require_admin()

    # delete uploaded images
    try:
        for p in UPLOAD_DIR.iterdir():
            if p.is_file():
                try:
                    p.unlink()
                except Exception:
                    pass
    except Exception:
        pass

    # clear tables in safe order
    conn = get_db()
    cur = conn.cursor()
    cur.execute("DELETE FROM vote_items;")
    cur.execute("DELETE FROM votes;")
    cur.execute("DELETE FROM entries;")
    cur.execute("DELETE FROM categories;")
    cur.execute("DELETE FROM settings;")
    conn.commit()
    conn.close()

    # reseed
    init_db()
    flash("All data purged. Defaults re-seeded and uploads cleared.", "success")
    return redirect(url_for("admin"))

# ---------------------------- TEMPLATES ----------------------------

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

# Wizard template: one category per step
TPL_VOTE_WIZARD = r"""
<h3>Cast Your Votes</h3>

{% set total = categories|length %}
{% set step = idx + 1 %}
<div style="margin: 12px 0;">
  <div class="muted">Category {{ step }} of {{ total }}</div>
  <div style="height:10px;background:#222;border-radius:999px;overflow:hidden;margin-top:6px;">
    <div style="height:100%;width:{{ (step / total * 100)|round(0,'floor') }}%;background:var(--accent);"></div>
  </div>
</div>

<form action="{{ url_for('vote_step_post', idx=idx) }}" method="post">
  {% if idx == 0 %}
  <div class="grid cols-2 mb-4">
    <div>
      <label>Your First Name</label>
      <input name="voter_first" value="{{ voter_first or '' }}" required />
    </div>
    <div>
      <label>Your Last Name</label>
      <input name="voter_last" value="{{ voter_last or '' }}" required />
    </div>
  </div>
  {% endif %}

  <div class="category-section" style="margin:1.0em 0; padding:1em; background:#222; border-radius:12px;">
    <h2 style="font-size:1.2em; color:#ffd700; border-bottom:1px solid #555; padding-bottom:0.3em; margin-top:0;">
      {{ category.name }}
    </h2>

    <style>
      .entries-grid {display:grid; gap:16px; grid-template-columns:repeat(2,minmax(0,1fr));}
      @media (min-width:1024px) {.entries-grid {grid-template-columns:repeat(3,minmax(0,1fr));}}
    </style>
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
    {% if idx > 0 %}
      <button class="btn secondary" name="nav" value="prev" type="submit">← Back</button>
    {% else %}
      <span></span>
    {% endif %}

    {% if idx + 1 < total %}
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

if __name__ == "__main__":
    app.run(debug=True, host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
