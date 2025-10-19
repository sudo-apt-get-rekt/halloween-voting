# 🎃 Halloween Costume Voting App

A lightweight, self-hosted **Flask + SQLite** web app for running Halloween costume awards at your party.  
Guests submit costumes (optionally with a photo), then vote.
Hosts manage entries, categories, voting state, and view live results — all from a simple admin panel.

---

## ✨ Features

- 🧙 **Costume Submissions:** Name, costume, and optional photo upload  
- 🗳️ **Voting:** 2-wide photo grid on mobile, 3-wide on larger devices; radio-button based
- 🧑‍💻 **Admin Dashboard**
  - Enable/disable voting
  - Add / rename / enable / disable / delete categories
  - Delete entries (photo cleaned up)
  - View live results
  - **One-click purge** (resets database and uploaded photos)
- 💾 **No external DB required** – uses a local SQLite file
- 🐋 **Docker ready** – containerized and ready to go
- 🔒 **Secure by default** – session key and admin password configurable via environment variables

---

## 🧱 Tech Stack

| Component | Technology |
|------------|-------------|
| Runtime | Python 3.11 |
| Framework | Flask |
| Database | SQLite |
| Web Server | Gunicorn |
| Storage | Local volumes (`/data`, `/uploads`) |

---

## 🚀 Quick Start (Docker)

```bash
docker run -d --name halloween \
  -p 8000:8000 \
  -e FLASK_SECRET="$(openssl rand -hex 32)" \
  -e ADMIN_PASSWORD="changeme" \
  -v "$(pwd)/data:/data" \
  -v "$(pwd)/uploads:/uploads" \
  docker.io/sudoaptgetrekt/halloween-voting:latest