import base64
import hashlib
import os
import secrets
import sqlite3
import subprocess
import threading
import time
from pathlib import Path
from urllib.parse import urlencode

import requests
from dotenv import load_dotenv

# Always load .env from the same directory as this file, regardless of the current working directory.
BASE = Path(__file__).resolve().parent
load_dotenv(BASE / ".env")
from cryptography.fernet import Fernet
from flask import Flask, abort, g, redirect, render_template_string, request, session, url_for

DB = BASE / "app.db"
UPLOADS = BASE / "uploads"
UPLOADS.mkdir(exist_ok=True)

app = Flask(__name__)
app.secret_key = os.getenv("APP_SECRET", "dev-only-change-me")
app.config["MAX_CONTENT_LENGTH"] = 600 * 1024 * 1024

TIKTOK_CLIENT_KEY = os.getenv("TIKTOK_CLIENT_KEY", "").strip()
TIKTOK_CLIENT_SECRET = os.getenv("TIKTOK_CLIENT_SECRET", "").strip()
TIKTOK_REDIRECT_URI = os.getenv("TIKTOK_REDIRECT_URI", "http://localhost:8000/auth/callback/").strip()
INTERNAL_RTMP_URL = os.getenv("INTERNAL_RTMP_URL", "").strip()
INTERNAL_STREAM_KEY = os.getenv("INTERNAL_STREAM_KEY", "").strip()

# Encrypt OAuth tokens at rest. Set APP_SECRET to a strong random value in production.
FERNET_KEY = base64.urlsafe_b64encode(hashlib.sha256(app.secret_key.encode()).digest())
fernet = Fernet(FERNET_KEY)

workers = {}
workers_lock = threading.Lock()


def db():
    if "db" not in g:
        g.db = sqlite3.connect(DB)
        g.db.row_factory = sqlite3.Row
    return g.db


@app.teardown_appcontext
def close_db(exc=None):
    conn = g.pop("db", None)
    if conn is not None:
        conn.close()


def init_db():
    conn = sqlite3.connect(DB)
    conn.executescript("""
    CREATE TABLE IF NOT EXISTS users (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        open_id TEXT UNIQUE NOT NULL,
        display_name TEXT NOT NULL DEFAULT '',
        avatar_url TEXT NOT NULL DEFAULT '',
        access_token TEXT NOT NULL,
        refresh_token TEXT NOT NULL,
        expires_at INTEGER NOT NULL DEFAULT 0,
        created_at INTEGER NOT NULL,
        updated_at INTEGER NOT NULL
    );
    CREATE TABLE IF NOT EXISTS videos (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER NOT NULL,
        filename TEXT NOT NULL,
        path TEXT NOT NULL,
        created_at INTEGER NOT NULL,
        FOREIGN KEY(user_id) REFERENCES users(id)
    );
    CREATE TABLE IF NOT EXISTS live_sessions (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER NOT NULL,
        video_id INTEGER NOT NULL,
        status TEXT NOT NULL,
        pid INTEGER,
        started_at INTEGER,
        ended_at INTEGER,
        error TEXT NOT NULL DEFAULT '',
        FOREIGN KEY(user_id) REFERENCES users(id),
        FOREIGN KEY(video_id) REFERENCES videos(id)
    );
    CREATE TABLE IF NOT EXISTS oauth_states (
        state TEXT PRIMARY KEY,
        code_verifier TEXT NOT NULL,
        created_at INTEGER NOT NULL
    );
    """)
    conn.commit()
    conn.close()


def enc(value):
    return fernet.encrypt((value or "").encode()).decode()


def dec(value):
    return fernet.decrypt(value.encode()).decode()


def current_user():
    uid = session.get("user_id")
    if not uid:
        return None
    return db().execute("SELECT * FROM users WHERE id=?", (uid,)).fetchone()


def require_user():
    user = current_user()
    if not user:
        return redirect(url_for("index"))
    return user


def oauth_url():
    state = secrets.token_urlsafe(32)
    verifier = secrets.token_urlsafe(64)
    challenge = hashlib.sha256(verifier.encode()).hexdigest()
    db().execute("INSERT INTO oauth_states(state, code_verifier, created_at) VALUES(?,?,?)", (state, verifier, int(time.time())))
    db().commit()
    params = {
        "client_key": TIKTOK_CLIENT_KEY,
        "response_type": "code",
        "scope": "user.info.basic",
        "redirect_uri": TIKTOK_REDIRECT_URI,
        "state": state,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
    }
    return "https://www.tiktok.com/v2/auth/authorize/?" + urlencode(params)


def token_request(code, verifier):
    r = requests.post("https://open.tiktokapis.com/v2/oauth/token/", data={
        "client_key": TIKTOK_CLIENT_KEY,
        "client_secret": TIKTOK_CLIENT_SECRET,
        "code": code,
        "grant_type": "authorization_code",
        "redirect_uri": TIKTOK_REDIRECT_URI,
        "code_verifier": verifier,
    }, timeout=30)
    data = r.json()
    if r.status_code >= 400 or data.get("error"):
        raise RuntimeError(data.get("error_description") or str(data))
    return data


def get_profile(access_token):
    r = requests.get(
        "https://open.tiktokapis.com/v2/user/info/",
        params={"fields": "open_id,avatar_url,display_name"},
        headers={"Authorization": f"Bearer {access_token}"},
        timeout=30,
    )
    data = r.json()
    if r.status_code >= 400 or not data.get("data", {}).get("user"):
        raise RuntimeError(str(data))
    return data["data"]["user"]


def refresh_user(user):
    if int(user["expires_at"]) > int(time.time()) + 300:
        return user
    try:
        refresh = dec(user["refresh_token"])
        r = requests.post("https://open.tiktokapis.com/v2/oauth/token/", data={
            "client_key": TIKTOK_CLIENT_KEY,
            "client_secret": TIKTOK_CLIENT_SECRET,
            "grant_type": "refresh_token",
            "refresh_token": refresh,
        }, timeout=30)
        data = r.json()
        if r.status_code >= 400 or data.get("error"):
            raise RuntimeError(data.get("error_description") or str(data))
        now = int(time.time())
        db().execute("UPDATE users SET access_token=?, refresh_token=?, expires_at=?, updated_at=? WHERE id=?", (
            enc(data["access_token"]), enc(data.get("refresh_token", refresh)), now + int(data.get("expires_in", 86400)), now, user["id"]
        ))
        db().commit()
        return db().execute("SELECT * FROM users WHERE id=?", (user["id"],)).fetchone()
    except Exception:
        return user


def ffmpeg_target():
    if not INTERNAL_RTMP_URL or not INTERNAL_STREAM_KEY:
        raise RuntimeError("Nenhum Live Provider está configurado. O Login Kit não fornece RTMP/Stream Key por si só.")
    if not INTERNAL_RTMP_URL.startswith(("rtmp://", "rtmps://")):
        raise RuntimeError("INTERNAL_RTMP_URL inválido.")
    return INTERNAL_RTMP_URL.rstrip("/") + "/" + INTERNAL_STREAM_KEY.strip()


def start_worker(user_id, video):
    with workers_lock:
        existing = workers.get(user_id)
        if existing and existing.poll() is None:
            raise RuntimeError("Sua LIVE já está rodando.")

    target = ffmpeg_target()
    cmd = ["ffmpeg", "-hide_banner", "-loglevel", "warning", "-re", "-stream_loop", "-1", "-i", video["path"],
           "-c:v", "libx264", "-preset", "veryfast", "-pix_fmt", "yuv420p", "-r", "30", "-g", "60",
           "-b:v", "2500k", "-maxrate", "2500k", "-bufsize", "5000k", "-c:a", "aac", "-b:a", "128k",
           "-ar", "44100", "-f", "flv", target]
    try:
        p = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True, encoding="utf-8", errors="replace",
                             creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except FileNotFoundError:
        raise RuntimeError("FFmpeg não encontrado. Instale o FFmpeg e coloque-o no PATH.")

    conn = sqlite3.connect(DB)
    cur = conn.execute("INSERT INTO live_sessions(user_id,video_id,status,pid,started_at) VALUES(?,?,?,?,?)",
                       (user_id, video["id"], "live", p.pid, int(time.time())))
    session_id = cur.lastrowid
    conn.commit(); conn.close()

    with workers_lock:
        workers[user_id] = p

    def monitor():
        error = ""
        try:
            for line in p.stderr:
                line = line.strip()
                if line:
                    error = line[-1000:]
        except Exception:
            pass
        code = p.wait()
        with workers_lock:
            if workers.get(user_id) is p:
                workers.pop(user_id, None)
        conn2 = sqlite3.connect(DB)
        conn2.execute("UPDATE live_sessions SET status=?, ended_at=?, error=? WHERE id=?", ("offline", int(time.time()), error if code else "", session_id))
        conn2.commit(); conn2.close()

    threading.Thread(target=monitor, daemon=True).start()
    return session_id


def stop_worker(user_id):
    with workers_lock:
        p = workers.get(user_id)
    if not p or p.poll() is not None:
        return False
    try:
        p.terminate(); p.wait(timeout=8)
    except Exception:
        try: p.kill()
        except Exception: pass
    return True


BASE_HTML = """
<!doctype html><html lang='pt-BR'><head><meta charset='utf-8'><meta name='viewport' content='width=device-width,initial-scale=1'>
<title>TikTok LIVE SaaS</title><style>
*{box-sizing:border-box}body{margin:0;background:#09090b;color:#f5f5f5;font-family:Inter,Arial,sans-serif}.wrap{max-width:900px;margin:40px auto;padding:0 18px}
.card{background:#141416;border:1px solid #29292e;border-radius:18px;padding:22px;margin:14px 0}.top{display:flex;justify-content:space-between;align-items:center;gap:15px}
button,a.btn{border:0;border-radius:11px;padding:12px 17px;font-weight:800;text-decoration:none;cursor:pointer;display:inline-block}.primary{background:#fff;color:#000}.danger{background:#e5484d;color:#fff}.secondary{background:#29292e;color:#fff}.disabled{opacity:.45;pointer-events:none}.status{font-weight:800}.live{color:#55e08a}.off{color:#aaa}.err,.ok{padding:12px;border-radius:11px;margin:10px 0}.err{background:#38181a;color:#ffb8bd}.ok{background:#12351f;color:#a8f5bd}.account{display:flex;align-items:center;gap:12px}.avatar{width:48px;height:48px;border-radius:50%;object-fit:cover;background:#29292e}.muted{color:#a1a1aa}.file{width:100%;padding:12px;background:#0d0d0f;border:1px solid #333;color:#ddd;border-radius:10px}.row{display:flex;gap:10px;flex-wrap:wrap}.badge{background:#29292e;padding:7px 10px;border-radius:999px}.note{line-height:1.5;color:#aaa}.hero{padding:20px 0 8px}h1{font-size:34px;margin:0 0 5px}h2{font-size:18px}code{background:#222;padding:2px 5px;border-radius:5px}
</style></head><body><div class='wrap'>{% block content %}{% endblock %}</div></body></html>
"""

LOGIN_TEMPLATE = """
{% extends base %}{% block content %}<div class='hero'><h1>TikTok LIVE</h1><p class='muted'>Conecte sua conta, envie seu vídeo e inicie a LIVE manualmente.</p></div>
<div class='card'><h2>Entrar</h2><p class='note'>Você será levado para a página oficial do TikTok. Sua senha não é enviada para este sistema.</p>
{% if error %}<div class='err'>{{error}}</div>{% endif %}<a class='btn primary' href='{{url_for("auth_tiktok")}}'>Entrar com TikTok</a></div>
<div class='card'><p class='note'>O Login Kit usa OAuth 2.0 e retorna tokens autorizados ao servidor. A criação da LIVE é uma camada separada.</p></div>{% endblock %}
"""

DASH_TEMPLATE = """
{% extends base %}{% block content %}<div class='hero top'><div><h1>TikTok LIVE</h1><p class='muted'>Painel do cliente</p></div><a class='btn secondary' href='{{url_for("logout")}}'>Sair</a></div>
{% if error %}<div class='err'>{{error}}</div>{% endif %}{% if ok %}<div class='ok'>{{ok}}</div>{% endif %}
<div class='card'><h2>1. Conta</h2><div class='account'>{% if user['avatar_url'] %}<img class='avatar' src='{{user["avatar_url"]}}'>{% else %}<div class='avatar'></div>{% endif %}<div><b>{{user['display_name'] or 'Conta TikTok'}}</b><div class='muted'>TikTok conectado</div></div></div></div>
<div class='card'><h2>2. Vídeo</h2><form method='post' action='{{url_for("upload")}}' enctype='multipart/form-data'><input class='file' type='file' name='video' accept='.mp4,.mov,.mkv,.webm,video/*' required><br><br><button class='primary'>Enviar vídeo</button></form>
{% if video %}<p class='note'>Vídeo atual: <b>{{video['filename']}}</b></p>{% else %}<p class='note'>Nenhum vídeo enviado.</p>{% endif %}</div>
<div class='card'><h2>3. LIVE</h2><p>Status: <span class='status {{'live' if live else 'off'}}'>{{'🟢 AO VIVO' if live else '🔴 OFFLINE'}}</span></p>
<div class='row'>{% if video and not live %}<form method='post' action='{{url_for("start")}}'><button class='primary'>Iniciar LIVE</button></form>{% endif %}{% if live %}<form method='post' action='{{url_for("stop")}}'><button class='danger'>Parar LIVE</button></form>{% endif %}</div>
<p class='note'>O reinício automático está desativado. Se a LIVE cair, o cliente volta ao painel e clica novamente em <b>Iniciar LIVE</b>.</p>
{% if not provider_ready %}<div class='err'>O painel está pronto, mas nenhum Live Provider está configurado no servidor. O Login Kit sozinho não gera RTMP/Stream Key.</div>{% endif %}</div>
<div class='card'><h2>Histórico</h2>{% for item in history %}<p><span class='badge'>{{item['status']}}</span> {{item['filename']}} — {{item['started_at'] or ''}}</p>{% else %}<p class='muted'>Nenhuma transmissão registrada.</p>{% endfor %}</div>{% endblock %}
"""

app.jinja_loader = type("Loader", (), {"get_source": lambda self, env, template: (BASE_HTML, None, lambda: True)})()
# Simpler: render complete strings through helper below rather than template inheritance.

def render_login(error=""):
    return render_template_string(BASE_HTML.replace("{% block content %}{% endblock %}", LOGIN_TEMPLATE.replace("{% extends base %}", "").replace("{% block content %}", "").replace("{% endblock %}", "")), error=error)


def render_dash(**ctx):
    return render_template_string(BASE_HTML.replace("{% block content %}{% endblock %}", DASH_TEMPLATE.replace("{% extends base %}", "").replace("{% block content %}", "").replace("{% endblock %}", "")), **ctx)


@app.route("/")
def index():
    user = current_user()
    if not user:
        return render_login()
    user = refresh_user(user)
    video = db().execute("SELECT * FROM videos WHERE user_id=? ORDER BY id DESC LIMIT 1", (user["id"],)).fetchone()
    with workers_lock:
        live = bool(workers.get(user["id"]) and workers[user["id"]].poll() is None)
    history = db().execute("SELECT l.*, v.filename FROM live_sessions l JOIN videos v ON v.id=l.video_id WHERE l.user_id=? ORDER BY l.id DESC LIMIT 10", (user["id"],)).fetchall()
    return render_dash(user=user, video=video, live=live, history=history, provider_ready=bool(INTERNAL_RTMP_URL and INTERNAL_STREAM_KEY), error=request.args.get("error", ""), ok=request.args.get("ok", ""))


@app.route("/auth/tiktok")
def auth_tiktok():
    if not TIKTOK_CLIENT_KEY or not TIKTOK_CLIENT_SECRET:
        return render_login("Configure TIKTOK_CLIENT_KEY e TIKTOK_CLIENT_SECRET no .env antes de conectar."), 500
    return redirect(oauth_url())


@app.route("/auth/callback/")
def auth_callback():
    state = request.args.get("state", "")
    row = db().execute("SELECT * FROM oauth_states WHERE state=?", (state,)).fetchone()
    db().execute("DELETE FROM oauth_states WHERE state=?", (state,)); db().commit()
    if not row or int(time.time()) - row["created_at"] > 600:
        return render_login("Sessão OAuth inválida ou expirada."), 400
    if request.args.get("error"):
        return render_login(request.args.get("error_description") or request.args.get("error")), 400
    try:
        tokens = token_request(request.args.get("code", ""), row["code_verifier"])
        profile = get_profile(tokens["access_token"])
        now = int(time.time())
        old = db().execute("SELECT id FROM users WHERE open_id=?", (profile["open_id"],)).fetchone()
        if old:
            db().execute("UPDATE users SET display_name=?, avatar_url=?, access_token=?, refresh_token=?, expires_at=?, updated_at=? WHERE id=?",
                         (profile.get("display_name", ""), profile.get("avatar_url", ""), enc(tokens["access_token"]), enc(tokens["refresh_token"]), now + int(tokens.get("expires_in", 86400)), now, old["id"]))
            uid = old["id"]
        else:
            cur = db().execute("INSERT INTO users(open_id,display_name,avatar_url,access_token,refresh_token,expires_at,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?)",
                               (profile["open_id"], profile.get("display_name", ""), profile.get("avatar_url", ""), enc(tokens["access_token"]), enc(tokens["refresh_token"]), now + int(tokens.get("expires_in", 86400)), now, now))
            uid = cur.lastrowid
        db().commit(); session["user_id"] = uid
        return redirect(url_for("index", ok="Conta TikTok conectada."))
    except Exception as e:
        return render_login(f"Falha ao conectar ao TikTok: {e}"), 400


@app.route("/logout")
def logout():
    session.clear(); return redirect(url_for("index"))


@app.route("/upload", methods=["POST"])
def upload():
    user = require_user()
    if not hasattr(user, "__getitem__"):
        return user
    file = request.files.get("video")
    if not file or not file.filename:
        return redirect(url_for("index", error="Selecione um vídeo."))
    ext = Path(file.filename).suffix.lower()
    if ext not in {".mp4", ".mov", ".mkv", ".webm"}:
        return redirect(url_for("index", error="Use MP4, MOV, MKV ou WEBM."))
    safe = Path(file.filename).name
    name = f"{user['id']}_{int(time.time())}_{secrets.token_hex(4)}{ext}"
    dest = UPLOADS / name
    file.save(dest)
    db().execute("INSERT INTO videos(user_id,filename,path,created_at) VALUES(?,?,?,?)", (user["id"], safe, str(dest), int(time.time())))
    db().commit()
    return redirect(url_for("index", ok="Vídeo enviado."))


@app.route("/start", methods=["POST"])
def start():
    user = require_user()
    if not hasattr(user, "__getitem__"):
        return user
    video = db().execute("SELECT * FROM videos WHERE user_id=? ORDER BY id DESC LIMIT 1", (user["id"],)).fetchone()
    if not video:
        return redirect(url_for("index", error="Envie um vídeo primeiro."))
    try:
        start_worker(user["id"], video)
        return redirect(url_for("index", ok="LIVE iniciada."))
    except Exception as e:
        return redirect(url_for("index", error=str(e)))


@app.route("/stop", methods=["POST"])
def stop():
    user = require_user()
    if not hasattr(user, "__getitem__"):
        return user
    stop_worker(user["id"])
    return redirect(url_for("index", ok="LIVE parada."))


if __name__ == "__main__":
    init_db()
    print(f"TikTok config: client_key={'OK' if TIKTOK_CLIENT_KEY else 'MISSING'}, client_secret={'OK' if TIKTOK_CLIENT_SECRET else 'MISSING'}")
    print("TikTok LIVE SaaS MVP: http://localhost:8000")
    app.run(host="127.0.0.1", port=8000, debug=False)
