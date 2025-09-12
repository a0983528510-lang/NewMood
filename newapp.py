# -*- coding: utf-8 -*-
import os, datetime, secrets, re, json
from pathlib import Path

from flask import Flask, render_template, request, redirect, url_for, session, abort, jsonify, flash
from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine
from sqlalchemy.pool import StaticPool
from google.oauth2 import id_token
from google.auth.transport import requests as grequests
from dotenv import load_dotenv
import requests as pyrequests  # server-side autofill

APP_TITLE = "NewMood"
BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env")

# oEmbed / APIs
YOUTUBE_OEMBED = "https://www.youtube.com/oembed?format=json&url="
SPOTIFY_OEMBED = "https://open.spotify.com/oembed?url="
ITUNES_LOOKUP = "https://itunes.apple.com/lookup?id="  # Apple Music 使用 iTunes 公開查詢，不需 API 金鑰

# ===== Helpers for link parsing =====
def extract_yt_id(url: str) -> str | None:
    # 僅支援 YouTube Music / YouTube 影片 ID（11 碼）
    pats = [
        r"youtu\.be/([A-Za-z0-9_-]{11})",
        r"[?&]v=([A-Za-z0-9_-]{11})",
        r"/shorts/([A-Za-z0-9_-]{11})",
    ]
    for p in pats:
        m = re.search(p, url)
        if m:
            return m.group(1)
    return None

def extract_apple_id(url: str) -> str | None:
    # 盡量從 Apple Music URL 抓 track/album id
    # e.g. https://music.apple.com/.../id123456789?i=987654321
    m = re.search(r"[?&]i=(\d+)", url)
    if m: return m.group(1)
    m = re.search(r"/id(\d+)", url)
    if m: return m.group(1)
    return None

# ===== App =====
def create_app():
    app = Flask(__name__, instance_relative_config=True)
    app.config.from_mapping(
        SECRET_KEY=os.environ.get("SECRET_KEY") or secrets.token_hex(32),
        DATABASE=str((BASE_DIR / "instance" / "newmood.db").resolve()),
        GOOGLE_CLIENT_ID=(os.environ.get("GOOGLE_CLIENT_ID") or "").strip(),
        ADMIN_EMAILS=[e.strip().lower() for e in (os.environ.get("ADMIN_EMAILS") or "").split(",") if e.strip()],
        ADMIN_PASS=(os.environ.get("ADMIN_PASS") or "").strip(),
        GA_MEASUREMENT_ID=(os.environ.get("GA_MEASUREMENT_ID") or "").strip(),
    )

    os.makedirs(app.instance_path, exist_ok=True)

    engine = create_engine(
        f"sqlite:///{app.config['DATABASE']}",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
        future=True,
    )
    app.engine = engine  # type: ignore
    init_db(app, engine)

    # ----- session helpers -----
    def current_user():
        u = session.get("user")
        return u if isinstance(u, dict) else None

    def is_admin():
        u = current_user()
        return bool(u and (u.get("email","").lower() in app.config["ADMIN_EMAILS"]))

    # ===== Routes =====
    @app.get("/")
    def index():
        return render_template("index.html", title=APP_TITLE, user=current_user())

    @app.get("/login")
    def login():
        if current_user():
            return redirect(url_for("index"))
        return render_template("login.html", title=f"{APP_TITLE} · Login", google_client_id=app.config["GOOGLE_CLIENT_ID"])

    @app.post("/auth/google")
    def auth_google():
        data = request.get_json(silent=True) or request.form
        credential = data.get("credential", "")
        if not credential:
            return jsonify({"ok": False, "error": "missing_credential"}), 400
        try:
            idinfo = id_token.verify_oauth2_token(credential, grequests.Request(), app.config["GOOGLE_CLIENT_ID"])
            email = idinfo.get("email")
            sub = idinfo.get("sub")
            name = idinfo.get("name") or ""
            if not email or not sub:
                raise ValueError("Invalid token payload")

            with app.engine.begin() as conn:
                conn.execute(text("""
                    INSERT INTO accounts (google_sub, email, nickname, created_at)
                    VALUES (:sub, :email, :nickname, :now)
                    ON CONFLICT(email) DO UPDATE SET google_sub = excluded.google_sub
                """), {"sub": sub, "email": email, "nickname": name, "now": datetime.datetime.utcnow().isoformat()})
                row = conn.execute(text("SELECT id, email, nickname FROM accounts WHERE email = :e"), {"e": email}).mappings().first()

            session["user"] = {"id": row["id"], "email": row["email"], "nickname": row["nickname"]}
            return jsonify({"ok": True})
        except Exception as e:
            return jsonify({"ok": False, "error": str(e)}), 400

    @app.post("/logout")
    def logout():
        session.pop("user", None)
        return redirect(url_for("index"))

    @app.get("/profile")
    def profile_get():
        u = current_user()
        if not u: return redirect(url_for("login"))
        return render_template("profile.html", title=f"{APP_TITLE} · Profile", user=u)

    @app.post("/profile")
    def profile_post():
        u = current_user()
        if not u: return redirect(url_for("login"))
        nickname = (request.form.get("nickname") or "").strip()
        if not nickname:
            flash("Nickname cannot be empty", "error")
            return redirect(url_for("profile_get"))
        with app.engine.begin() as conn:
            conn.execute(text("UPDATE accounts SET nickname = :n WHERE id = :i"), {"n": nickname, "i": u["id"]})
            row = conn.execute(text("SELECT id, email, nickname FROM accounts WHERE id = :i"), {"i": u["id"]}).mappings().first()
        session["user"] = {"id": row["id"], "email": row["email"], "nickname": row["nickname"]}
        flash("Profile updated", "success")
        return redirect(url_for("index"))

    # ======= AutoFill (only YT Music / Spotify / Apple Music) =======
    @app.post("/autofill")
    def autofill():
        link = (request.json or {}).get("link", "").strip()
        if not link:
            return jsonify({"ok": False, "error": "missing_link"}), 400

        # 僅允許三種來源
        allow = any(s in link for s in ["music.youtube.com", "youtu.be", "youtube.com", "open.spotify.com", "music.apple.com"])
        if not allow:
            return jsonify({"ok": False, "error": "only_youtube_music_spotify_apple_music_supported"}), 400

        meta = {"title": "", "artist": "", "thumbnail": ""}

        try:
            # YouTube Music / YouTube
            if "youtube" in link or "youtu.be" in link:
                vid = extract_yt_id(link)
                if vid:
                    meta["thumbnail"] = f"https://img.youtube.com/vi/{vid}/hqdefault.jpg"
                r = pyrequests.get(YOUTUBE_OEMBED + pyrequests.utils.quote(link), timeout=6)
                if r.ok:
                    d = r.json()
                    meta["title"] = d.get("title","")
                    meta["artist"] = d.get("author_name","")

            # Spotify
            elif "open.spotify.com" in link:
                r = pyrequests.get(SPOTIFY_OEMBED + pyrequests.utils.quote(link), timeout=6)
                if r.ok:
                    d = r.json()
                    title = (d.get("title") or "").strip()
                    if "·" in title:
                        a,b = [x.strip() for x in title.split("·",1)]
                        meta["title"] = a
                        meta["artist"] = d.get("author_name") or b
                    else:
                        meta["title"] = title
                        meta["artist"] = d.get("author_name","")
                    if d.get("thumbnail_url"):
                        # Spotify 縮圖通常已是正方形
                        meta["thumbnail"] = d["thumbnail_url"]

            # Apple Music
            elif "music.apple.com" in link:
                aid = extract_apple_id(link)
                if not aid:
                    return jsonify({"ok": False, "error": "apple_music_id_not_found"}), 400
                r = pyrequests.get(ITUNES_LOOKUP + aid, timeout=6)
                if r.ok:
                    d = r.json()
                    if d.get("resultCount",0) > 0:
                        item = d["results"][0]
                        meta["title"]  = item.get("trackName") or item.get("collectionName") or ""
                        meta["artist"] = item.get("artistName","")
                        art = item.get("artworkUrl100","")
                        if art:
                            meta["thumbnail"] = art.replace("100x100", "600x600")

            return jsonify({"ok": True, "meta": meta})
        except Exception as e:
            return jsonify({"ok": False, "error": f"autofill_failed: {e}"}), 500

    # ====== Submit / Draw ======
    @app.post("/submit")
    def submit():
        u = current_user()
        if not u:
            return jsonify({"ok": False, "error": "not_logged_in"}), 401

        title  = (request.form.get("title")  or "").strip()
        artist = (request.form.get("artist") or "").strip()
        reason = (request.form.get("reason") or "").strip()
        link   = (request.form.get("link")   or "").strip()
        if not link:
            return jsonify({"ok": False, "error": "missing_link"}), 400
        # Title/Artist 由 AutoFill 帶入（隱藏欄位），保底再驗一次
        if not title or not artist:
            return jsonify({"ok": False, "error": "missing_title_artist_autofill_first"}), 400

        with app.engine.begin() as conn:
            conn.execute(text("""
                INSERT INTO recommendations (account_id, title, artist, reason, link, created_at)
                VALUES (:aid, :t, :a, :r, :l, :now)
            """), {"aid": u["id"], "t": title, "a": artist, "r": reason, "l": link, "now": datetime.datetime.utcnow().isoformat()})

            row = conn.execute(text("""
                SELECT r.id, r.title, r.artist, r.reason, r.link,
                       a.nickname,
                       CASE
                         WHEN instr(lower(r.link), 'youtu') > 0 THEN
                           'https://img.youtube.com/vi/' ||
                           substr(
                             replace(
                               replace(
                                 replace(lower(r.link),
                                   'https://www.youtube.com/watch?v=', ''),
                                 'https://youtu.be/', ''),
                               'https://youtube.com/shorts/', ''),
                             1, 11
                           ) || '/hqdefault.jpg'
                         ELSE NULL
                       END AS thumbnail
                FROM recommendations r
                LEFT JOIN accounts a ON a.id = r.account_id
                WHERE r.account_id != :aid
                ORDER BY RANDOM()
                LIMIT 1
            """), {"aid": u["id"]}).mappings().first()

            if row:
                conn.execute(text("""
                    INSERT INTO draws (account_id, recommendation_id, created_at)
                    VALUES (:aid, :rid, :now)
                """), {"aid": u["id"], "rid": row["id"], "now": datetime.datetime.utcnow().isoformat()})

        return jsonify({"ok": True, "drawn": dict(row) if row else None})

    @app.get("/history")
    def history():
        u = current_user()
        if not u:
            return redirect(url_for("login"))
        with app.engine.begin() as conn:
            rows = conn.execute(text("""
                SELECT d.id as draw_id, d.created_at,
                       r.title, r.artist, r.reason, r.link,
                       a.nickname,
                       CASE
                         WHEN instr(lower(r.link), 'youtu') > 0 THEN
                           'https://img.youtube.com/vi/' ||
                           substr(
                             replace(
                               replace(
                                 replace(lower(r.link),
                                   'https://www.youtube.com/watch?v=', ''),
                                 'https://youtu.be/', ''),
                               'https://youtube.com/shorts/', ''),
                             1, 11
                           ) || '/hqdefault.jpg'
                         ELSE NULL
                       END AS thumbnail
                FROM draws d
                JOIN recommendations r ON r.id = d.recommendation_id
                LEFT JOIN accounts a ON a.id = r.account_id
                WHERE d.account_id = :aid
                ORDER BY d.id DESC
                LIMIT 100
            """), {"aid": u["id"]}).mappings().all()
        return render_template("history.html", title=f"{APP_TITLE} · History", items=rows, user=u)

    # --- Admin ---
    import functools
    def require_admin(view):
        @functools.wraps(view)
        def wrapper(*args, **kwargs):
            token = request.args.get("token", "").strip()
            if app.config["ADMIN_PASS"] and token == app.config["ADMIN_PASS"]:
                return view(*args, **kwargs)
            if is_admin():
                return view(*args, **kwargs)
            return abort(403)
        return wrapper

    @app.get("/admin")
    @require_admin
    def admin():
        with app.engine.begin() as conn:
            metrics = conn.execute(text("""
                SELECT
                  (SELECT COUNT(*) FROM accounts) as users,
                  (SELECT COUNT(*) FROM recommendations) as recos,
                  (SELECT COUNT(*) FROM draws) as draws
            """)).mappings().first()
            latest = conn.execute(text("""
                SELECT r.id, r.title, r.artist, r.reason, r.link, r.created_at,
                       a.email, a.nickname
                FROM recommendations r
                LEFT JOIN accounts a ON a.id = r.account_id
                ORDER BY r.id DESC LIMIT 50
            """)).mappings().all()
        return render_template("admin.html", title=f"{APP_TITLE} · Admin", metrics=metrics, rows=latest)

    return app

def init_db(app: Flask, engine: Engine):
    with engine.begin() as conn:
        conn.execute(text("""
            CREATE TABLE IF NOT EXISTS accounts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                google_sub TEXT,
                email TEXT UNIQUE,
                nickname TEXT,
                created_at TEXT
            );
        """))
        conn.execute(text("""
            CREATE TABLE IF NOT EXISTS recommendations (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                account_id INTEGER NOT NULL,
                title TEXT NOT NULL,
                artist TEXT NOT NULL,
                reason TEXT,
                link TEXT,
                created_at TEXT,
                FOREIGN KEY(account_id) REFERENCES accounts(id)
            );
        """))
        conn.execute(text("""
            CREATE TABLE IF NOT EXISTS draws (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                account_id INTEGER NOT NULL,
                recommendation_id INTEGER NOT NULL,
                created_at TEXT,
                FOREIGN KEY(account_id) REFERENCES accounts(id),
                FOREIGN KEY(recommendation_id) REFERENCES recommendations(id)
            );
        """))
        conn.execute(text("CREATE INDEX IF NOT EXISTS idx_reco_created ON recommendations(created_at);"))
        conn.execute(text("CREATE INDEX IF NOT EXISTS idx_draws_account ON draws(account_id);"))

app = create_app()

if __name__ == "__main__":
    app.run(host="127.0.0.1", port=5000, debug=True, use_reloader=False)
