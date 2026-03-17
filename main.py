import os
import re
import time
import secrets
import hashlib
import unicodedata
from pathlib import Path
from datetime import datetime, timedelta, timezone
from collections import defaultdict

import aiofiles
from fastapi import FastAPI, Request, Form, HTTPException, UploadFile, File
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from motor.motor_asyncio import AsyncIOMotorClient
from jose import jwt
from contextlib import asynccontextmanager
import bcrypt

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
MONGODB_URI = os.getenv("MONGODB_URI", "mongodb://localhost:27017")
SECRET_KEY = os.getenv("SECRET_KEY", "dev-secret-key-change-me")
ALGORITHM = "HS256"
STUDENT_SESSION_HOURS = 2
TEACHER_SESSION_HOURS = 8
MAX_UPLOAD_SIZE = 2 * 1024 * 1024  # 2MB

BASE_DIR = Path(__file__).resolve().parent
INTERACTIVES_DIR = BASE_DIR / "interactives"

# Subject colour map
SUBJECT_COLOURS = {
    "English": "#1565C0",
    "Mathematics": "#4CAF50",
    "Additional Mathematics": "#2E7D32",
    "Physics": "#FF9800",
    "Chemistry": "#9C27B0",
    "Biology": "#F44336",
    "Computing": "#2196F3",
    "History": "#795548",
    "Geography": "#00897B",
    "Economics": "#009688",
    "Literature": "#AD1457",
    "Art": "#E91E63",
    "Music": "#7B1FA2",
    "Drama": "#D81B60",
    "Design & Technology": "#FF6F00",
    "Principles of Accounts": "#546E7A",
    "Nutrition and Food Science": "#8D6E63",
    "Exercise and Sports Science": "#EF6C00",
}

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def slugify(text: str) -> str:
    """Convert text to a URL-safe slug."""
    text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode()
    text = re.sub(r"[^\w\s-]", "", text.lower())
    return re.sub(r"[-\s]+", "-", text).strip("-")


# ---------------------------------------------------------------------------
# Rate limiter (in-memory)
# ---------------------------------------------------------------------------
_rate_limits: dict[str, list[float]] = defaultdict(list)
RATE_LIMIT_MAX = 5
RATE_LIMIT_WINDOW = 60  # seconds


def _check_rate_limit(key: str) -> bool:
    """Return True if request is allowed, False if rate-limited."""
    now = time.time()
    window_start = now - RATE_LIMIT_WINDOW
    _rate_limits[key] = [t for t in _rate_limits[key] if t > window_start]
    if len(_rate_limits[key]) >= RATE_LIMIT_MAX:
        return False
    _rate_limits[key].append(now)
    return True


# ---------------------------------------------------------------------------
# Database helpers
# ---------------------------------------------------------------------------
db_client: AsyncIOMotorClient | None = None
db = None


async def get_db():
    return db


async def scan_interactives():
    """Walk the interactives/ directory and upsert new entries into MongoDB."""
    if not INTERACTIVES_DIR.exists():
        return 0

    count = 0
    for html_file in sorted(INTERACTIVES_DIR.rglob("*.html")):
        rel = html_file.relative_to(INTERACTIVES_DIR)
        slug = str(rel.with_suffix(""))  # e.g. "computing/logic-gates"
        parts = slug.split("/")
        if len(parts) != 2:
            continue

        subject_folder, filename = parts
        subject = subject_folder.replace("-", " ").title()
        title = filename.replace("-", " ").title()

        existing = await db.interactives.find_one({"slug": slug})
        if existing is None:
            await db.interactives.insert_one(
                {
                    "slug": slug,
                    "title": title,
                    "subject": subject,
                    "description": "",
                    "thumbnail": "",
                    "passcode": None,
                    "teacher": None,
                    "uploaded_by": None,
                    "uploaded_by_email": None,
                    "is_active": True,
                    "created_at": datetime.now(timezone.utc),
                    "updated_at": datetime.now(timezone.utc),
                }
            )
            count += 1
    return count


async def ensure_default_admin():
    """Create default admin if no teachers exist."""
    teacher_count = await db.teachers.count_documents({})
    if teacher_count == 0:
        pw_hash = bcrypt.hashpw("changeme123".encode(), bcrypt.gensalt()).decode()
        await db.teachers.insert_one(
            {
                "name": "Administrator",
                "email": "joe_tay@moe.edu.sg",
                "password_hash": pw_hash,
                "role": "admin",
                "subjects": ["Computing", "Economics", "Chemistry"],
                "created_at": datetime.now(timezone.utc),
            }
        )
        print(
            "\u26a0\ufe0f  Default admin account created. Please change the password immediately."
        )


# ---------------------------------------------------------------------------
# JWT helpers
# ---------------------------------------------------------------------------
def create_token(data: dict, expires_hours: int) -> str:
    payload = data.copy()
    payload["exp"] = datetime.now(timezone.utc) + timedelta(hours=expires_hours)
    return jwt.encode(payload, SECRET_KEY, algorithm=ALGORITHM)


def decode_token(token: str) -> dict | None:
    try:
        return jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
    except Exception:
        return None


# CSRF token helpers
def generate_csrf_token(session_id: str = "") -> str:
    raw = f"{SECRET_KEY}:{session_id}:{int(time.time()) // 3600}"
    return hashlib.sha256(raw.encode()).hexdigest()[:32]


def verify_csrf_token(token: str, session_id: str = "") -> bool:
    for offset in (0, -1):
        raw = f"{SECRET_KEY}:{session_id}:{int(time.time()) // 3600 + offset}"
        expected = hashlib.sha256(raw.encode()).hexdigest()[:32]
        if token == expected:
            return True
    return False


# ---------------------------------------------------------------------------
# App lifespan
# ---------------------------------------------------------------------------
@asynccontextmanager
async def lifespan(app: FastAPI):
    global db_client, db
    db_client = AsyncIOMotorClient(MONGODB_URI)
    db = db_client.ctss_hub
    # Migrate old "Mr Joe" admin to "Administrator"
    old_admin = await db.teachers.find_one({"name": "Mr Joe", "email": "joe@ctss.edu.sg"})
    if old_admin:
        await db.teachers.update_one(
            {"_id": old_admin["_id"]},
            {"$set": {"name": "Administrator", "email": "joe_tay@moe.edu.sg"}},
        )
        print("Migrated admin: Mr Joe -> Administrator (joe_tay@moe.edu.sg)")
    await ensure_default_admin()
    n = await scan_interactives()
    if n:
        print(f"Auto-discovered {n} new interactive(s).")
    yield
    db_client.close()


# ---------------------------------------------------------------------------
# FastAPI app
# ---------------------------------------------------------------------------
app = FastAPI(title="CTSS Interactive Hub", lifespan=lifespan)

app.mount("/static", StaticFiles(directory=str(BASE_DIR / "static")), name="static")
app.mount(
    "/interactives",
    StaticFiles(directory=str(INTERACTIVES_DIR)),
    name="interactives",
)

templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))
templates.env.globals["subject_colours"] = SUBJECT_COLOURS


# ---------------------------------------------------------------------------
# Dependency: get current teacher from cookie
# ---------------------------------------------------------------------------
async def get_current_teacher(request: Request):
    token = request.cookies.get("teacher_session")
    if not token:
        return None
    payload = decode_token(token)
    if not payload:
        return None
    teacher = await db.teachers.find_one({"name": payload.get("name")})
    return teacher


# ---------------------------------------------------------------------------
# PUBLIC ROUTES — Students
# ---------------------------------------------------------------------------
@app.get("/", response_class=HTMLResponse)
async def student_home(request: Request):
    cursor = db.interactives.find({"is_active": True}).sort("subject", 1)
    interactives = await cursor.to_list(length=200)

    grouped: dict[str, list] = {}
    for item in interactives:
        grouped.setdefault(item["subject"], []).append(item)

    return templates.TemplateResponse(
        "student_home.html",
        {"request": request, "grouped": grouped, "subject_colours": SUBJECT_COLOURS},
    )


@app.get("/i/{subject}/{name}", response_class=HTMLResponse)
async def passcode_gate(request: Request, subject: str, name: str):
    slug = f"{subject}/{name}"
    interactive = await db.interactives.find_one({"slug": slug, "is_active": True})
    if not interactive:
        raise HTTPException(status_code=404, detail="Interactive not found")

    if not interactive.get("passcode"):
        return RedirectResponse(url=f"/i/{subject}/{name}/view", status_code=303)

    token = request.cookies.get(f"access_{slug.replace('/', '_')}")
    if token:
        payload = decode_token(token)
        if payload and payload.get("slug") == slug:
            return RedirectResponse(url=f"/i/{subject}/{name}/view", status_code=303)

    csrf = generate_csrf_token(slug)
    return templates.TemplateResponse(
        "passcode_gate.html",
        {
            "request": request,
            "interactive": interactive,
            "subject": subject,
            "name": name,
            "error": None,
            "csrf_token": csrf,
            "subject_colours": SUBJECT_COLOURS,
        },
    )


@app.post("/i/{subject}/{name}/verify", response_class=HTMLResponse)
async def verify_passcode(
    request: Request, subject: str, name: str, passcode: str = Form(...), csrf_token: str = Form("")
):
    slug = f"{subject}/{name}"

    if not verify_csrf_token(csrf_token, slug):
        raise HTTPException(status_code=403, detail="Invalid request")

    client_ip = request.client.host if request.client else "unknown"
    if not _check_rate_limit(f"passcode:{client_ip}"):
        interactive = await db.interactives.find_one({"slug": slug})
        csrf = generate_csrf_token(slug)
        return templates.TemplateResponse(
            "passcode_gate.html",
            {
                "request": request,
                "interactive": interactive or {},
                "subject": subject,
                "name": name,
                "error": "Too many attempts. Please wait a minute.",
                "csrf_token": csrf,
                "subject_colours": SUBJECT_COLOURS,
            },
        )

    interactive = await db.interactives.find_one({"slug": slug, "is_active": True})
    if not interactive:
        raise HTTPException(status_code=404, detail="Interactive not found")

    if interactive.get("passcode") and passcode.strip() != interactive["passcode"]:
        await db.access_logs.insert_one(
            {"interactive_slug": slug, "timestamp": datetime.now(timezone.utc), "success": False}
        )
        csrf = generate_csrf_token(slug)
        return templates.TemplateResponse(
            "passcode_gate.html",
            {
                "request": request,
                "interactive": interactive,
                "subject": subject,
                "name": name,
                "error": "Incorrect passcode. Please try again.",
                "csrf_token": csrf,
                "subject_colours": SUBJECT_COLOURS,
            },
        )

    await db.access_logs.insert_one(
        {"interactive_slug": slug, "timestamp": datetime.now(timezone.utc), "success": True}
    )

    token = create_token({"slug": slug, "type": "student"}, STUDENT_SESSION_HOURS)
    response = RedirectResponse(url=f"/i/{subject}/{name}/view", status_code=303)
    response.set_cookie(
        key=f"access_{slug.replace('/', '_')}",
        value=token,
        httponly=True,
        max_age=STUDENT_SESSION_HOURS * 3600,
        samesite="lax",
    )
    return response


@app.get("/i/{subject}/{name}/view", response_class=HTMLResponse)
async def interactive_viewer(request: Request, subject: str, name: str):
    slug = f"{subject}/{name}"
    interactive = await db.interactives.find_one({"slug": slug, "is_active": True})
    if not interactive:
        raise HTTPException(status_code=404, detail="Interactive not found")

    if interactive.get("passcode"):
        token = request.cookies.get(f"access_{slug.replace('/', '_')}")
        if not token:
            return RedirectResponse(url=f"/i/{subject}/{name}", status_code=303)
        payload = decode_token(token)
        if not payload or payload.get("slug") != slug:
            return RedirectResponse(url=f"/i/{subject}/{name}", status_code=303)

    html_path = INTERACTIVES_DIR / f"{slug}.html"
    if not html_path.exists():
        raise HTTPException(status_code=404, detail="Interactive file not found")

    return templates.TemplateResponse(
        "interactive_viewer.html",
        {
            "request": request,
            "interactive": interactive,
            "subject": subject,
            "name": name,
            "iframe_src": f"/interactives/{slug}.html",
            "subject_colours": SUBJECT_COLOURS,
        },
    )


# ---------------------------------------------------------------------------
# PUBLIC — Teacher pages
# ---------------------------------------------------------------------------
@app.get("/t/{teacher_slug}", response_class=HTMLResponse)
async def teacher_page(request: Request, teacher_slug: str):
    teachers_list = await db.teachers.find().to_list(length=100)
    page_teacher = None
    for t in teachers_list:
        if slugify(t["name"]) == teacher_slug:
            page_teacher = t
            break

    if not page_teacher:
        raise HTTPException(status_code=404, detail="Teacher not found")

    cursor = db.interactives.find(
        {"uploaded_by": page_teacher["name"], "is_active": True}
    ).sort("subject", 1)
    interactives = await cursor.to_list(length=200)

    grouped: dict[str, list] = {}
    for item in interactives:
        grouped.setdefault(item["subject"], []).append(item)

    return templates.TemplateResponse(
        "teacher_page.html",
        {
            "request": request,
            "page_teacher": page_teacher,
            "grouped": grouped,
            "subject_colours": SUBJECT_COLOURS,
        },
    )


# ---------------------------------------------------------------------------
# PUBLIC — Invite registration
# ---------------------------------------------------------------------------
@app.get("/join/{token}", response_class=HTMLResponse)
async def invite_registration_page(request: Request, token: str):
    invite = await db.invites.find_one({"token": token})
    if not invite:
        raise HTTPException(status_code=404, detail="Invalid invite link")
    if invite.get("use_count", 0) >= invite.get("max_uses", 10):
        raise HTTPException(status_code=410, detail="This invite link has reached its maximum number of registrations")
    if invite.get("expires_at") and invite["expires_at"].replace(tzinfo=None) < datetime.now(timezone.utc).replace(tzinfo=None):
        raise HTTPException(status_code=410, detail="This invite has expired")

    csrf = generate_csrf_token(token)
    return templates.TemplateResponse(
        "invite_register.html",
        {
            "request": request,
            "token": token,
            "email_hint": invite.get("email_hint", ""),
            "error": None,
            "csrf_token": csrf,
            "subject_colours": SUBJECT_COLOURS,
        },
    )


@app.post("/join/{token}/register", response_class=HTMLResponse)
async def invite_register(
    request: Request,
    token: str,
    name: str = Form(...),
    email: str = Form(...),
    password: str = Form(...),
    subjects: str = Form(""),
    csrf_token: str = Form(""),
):
    if not verify_csrf_token(csrf_token, token):
        raise HTTPException(status_code=403, detail="Invalid request")

    invite = await db.invites.find_one({"token": token})
    if not invite or invite.get("use_count", 0) >= invite.get("max_uses", 10):
        raise HTTPException(status_code=410, detail="Invalid or fully used invite")
    if invite.get("expires_at") and invite["expires_at"].replace(tzinfo=None) < datetime.now(timezone.utc).replace(tzinfo=None):
        raise HTTPException(status_code=410, detail="Invite expired")

    name = name.strip()
    email = email.strip()

    if not name or not email or not password:
        csrf = generate_csrf_token(token)
        return templates.TemplateResponse("invite_register.html", {
            "request": request, "token": token,
            "email_hint": invite.get("email_hint", ""),
            "error": "All fields are required.",
            "csrf_token": csrf, "subject_colours": SUBJECT_COLOURS,
        })

    existing = await db.teachers.find_one({"$or": [{"name": name}, {"email": email}]})
    if existing:
        csrf = generate_csrf_token(token)
        return templates.TemplateResponse("invite_register.html", {
            "request": request, "token": token,
            "email_hint": invite.get("email_hint", ""),
            "error": "A teacher with that name or email already exists.",
            "csrf_token": csrf, "subject_colours": SUBJECT_COLOURS,
        })

    subject_list = [s.strip() for s in subjects.split(",") if s.strip()]

    pw_hash = bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode()
    await db.teachers.insert_one({
        "name": name, "email": email, "password_hash": pw_hash,
        "role": "teacher", "subjects": subject_list,
        "created_at": datetime.now(timezone.utc),
        "invited_by": invite["created_by"],
    })

    await db.invites.update_one(
        {"token": token},
        {
            "$inc": {"use_count": 1},
            "$push": {"registrations": {"name": name, "email": email, "registered_at": datetime.now(timezone.utc)}},
        },
    )

    jwt_token = create_token({"name": name, "role": "teacher", "type": "teacher"}, TEACHER_SESSION_HOURS)
    response = RedirectResponse(url="/admin/dashboard", status_code=303)
    response.set_cookie(key="teacher_session", value=jwt_token, httponly=True, max_age=TEACHER_SESSION_HOURS * 3600, samesite="lax")
    return response


# ---------------------------------------------------------------------------
# ADMIN ROUTES — Teachers
# ---------------------------------------------------------------------------
@app.get("/admin", response_class=HTMLResponse)
async def admin_login_page(request: Request):
    teacher = await get_current_teacher(request)
    if teacher:
        return RedirectResponse(url="/admin/dashboard", status_code=303)

    teacher_names = await db.teachers.distinct("name")
    csrf = generate_csrf_token("admin-login")
    return templates.TemplateResponse(
        "admin_login.html",
        {"request": request, "teacher_names": sorted(teacher_names), "error": None, "csrf_token": csrf},
    )


@app.post("/admin/login", response_class=HTMLResponse)
async def admin_login(request: Request, name: str = Form(...), password: str = Form(...), csrf_token: str = Form("")):
    if not verify_csrf_token(csrf_token, "admin-login"):
        raise HTTPException(status_code=403, detail="Invalid request")

    client_ip = request.client.host if request.client else "unknown"
    if not _check_rate_limit(f"admin:{client_ip}"):
        teacher_names = await db.teachers.distinct("name")
        csrf = generate_csrf_token("admin-login")
        return templates.TemplateResponse(
            "admin_login.html",
            {"request": request, "teacher_names": sorted(teacher_names), "error": "Too many attempts. Please wait.", "csrf_token": csrf},
        )

    teacher = await db.teachers.find_one({"name": name})
    if not teacher or not bcrypt.checkpw(password.encode(), teacher["password_hash"].encode()):
        teacher_names = await db.teachers.distinct("name")
        csrf = generate_csrf_token("admin-login")
        return templates.TemplateResponse(
            "admin_login.html",
            {"request": request, "teacher_names": sorted(teacher_names), "error": "Invalid credentials.", "csrf_token": csrf},
        )

    token = create_token(
        {"name": teacher["name"], "role": teacher.get("role", "teacher"), "type": "teacher"},
        TEACHER_SESSION_HOURS,
    )
    response = RedirectResponse(url="/admin/dashboard", status_code=303)
    response.set_cookie(key="teacher_session", value=token, httponly=True, max_age=TEACHER_SESSION_HOURS * 3600, samesite="lax")
    return response


@app.get("/admin/dashboard", response_class=HTMLResponse)
async def admin_dashboard(request: Request):
    teacher = await get_current_teacher(request)
    if not teacher:
        return RedirectResponse(url="/admin", status_code=303)

    # All teachers can view all interactives
    query = {}

    cursor = db.interactives.find(query).sort([("subject", 1), ("title", 1)])
    interactives = await cursor.to_list(length=200)

    csrf = generate_csrf_token(teacher["name"])
    return templates.TemplateResponse(
        "admin_dashboard.html",
        {
            "request": request,
            "teacher": teacher,
            "interactives": interactives,
            "subject_colours": SUBJECT_COLOURS,
            "csrf_token": csrf,
            "teacher_slug": slugify(teacher["name"]),
        },
    )


@app.post("/admin/api/update-interactive")
async def api_update_interactive(request: Request):
    teacher = await get_current_teacher(request)
    if not teacher:
        return JSONResponse({"error": "Unauthorized"}, status_code=401)

    data = await request.json()
    slug = data.get("slug")
    if not slug:
        return JSONResponse({"error": "Missing slug"}, status_code=400)

    # Ownership check
    if teacher.get("role") != "admin":
        interactive = await db.interactives.find_one({"slug": slug})
        if not interactive or interactive.get("uploaded_by") != teacher["name"]:
            return JSONResponse({"error": "Not authorized"}, status_code=403)

    update_fields: dict = {"updated_at": datetime.now(timezone.utc)}

    if "passcode" in data:
        passcode_val = data["passcode"].strip() if data["passcode"] else None
        update_fields["passcode"] = passcode_val
        update_fields["teacher"] = teacher["name"] if passcode_val else None
    if "is_active" in data:
        update_fields["is_active"] = bool(data["is_active"])
    if "title" in data:
        update_fields["title"] = data["title"].strip()
    if "description" in data:
        update_fields["description"] = data["description"].strip()

    result = await db.interactives.update_one({"slug": slug}, {"$set": update_fields})
    if result.matched_count == 0:
        return JSONResponse({"error": "Not found"}, status_code=404)

    return JSONResponse({"ok": True})


@app.post("/admin/api/delete-interactive")
async def api_delete_interactive(request: Request):
    teacher = await get_current_teacher(request)
    if not teacher:
        return JSONResponse({"error": "Unauthorized"}, status_code=401)

    data = await request.json()
    slug = data.get("slug")
    if not slug:
        return JSONResponse({"error": "Missing slug"}, status_code=400)

    interactive = await db.interactives.find_one({"slug": slug})
    if not interactive:
        return JSONResponse({"error": "Not found"}, status_code=404)

    if teacher.get("role") != "admin" and interactive.get("uploaded_by") != teacher["name"]:
        return JSONResponse({"error": "Not authorized"}, status_code=403)

    html_path = INTERACTIVES_DIR / f"{slug}.html"
    if html_path.exists():
        html_path.unlink()

    await db.interactives.delete_one({"slug": slug})
    return JSONResponse({"ok": True})


@app.post("/admin/api/upload-interactive")
async def api_upload_interactive(
    request: Request,
    title: str = Form(...),
    subject: str = Form(...),
    description: str = Form(""),
    file: UploadFile = File(...),
):
    teacher = await get_current_teacher(request)
    if not teacher:
        return JSONResponse({"error": "Unauthorized"}, status_code=401)

    if subject not in SUBJECT_COLOURS:
        return JSONResponse({"error": f"Invalid subject. Choose from: {', '.join(SUBJECT_COLOURS.keys())}"}, status_code=400)

    if not file.filename or not file.filename.lower().endswith(".html"):
        return JSONResponse({"error": "Only .html files are allowed"}, status_code=400)

    content = await file.read()
    if len(content) > MAX_UPLOAD_SIZE:
        return JSONResponse({"error": "File too large (max 2MB)"}, status_code=400)

    if not title.strip():
        return JSONResponse({"error": "Title is required"}, status_code=400)

    subject_folder = slugify(subject)
    title_slug = slugify(title.strip())
    if not title_slug:
        return JSONResponse({"error": "Invalid title"}, status_code=400)

    slug = f"{subject_folder}/{title_slug}"

    existing = await db.interactives.find_one({"slug": slug})
    if existing:
        return JSONResponse({"error": f"An interactive with this title already exists in {subject}. Choose a different title."}, status_code=409)

    subject_dir = INTERACTIVES_DIR / subject_folder
    subject_dir.mkdir(parents=True, exist_ok=True)

    file_path = subject_dir / f"{title_slug}.html"
    async with aiofiles.open(str(file_path), "wb") as f:
        await f.write(content)

    await db.interactives.insert_one({
        "slug": slug,
        "title": title.strip(),
        "subject": subject,
        "description": description.strip(),
        "thumbnail": "",
        "passcode": None,
        "teacher": None,
        "uploaded_by": teacher["name"],
        "uploaded_by_email": teacher.get("email"),
        "is_active": True,
        "created_at": datetime.now(timezone.utc),
        "updated_at": datetime.now(timezone.utc),
    })

    return JSONResponse({"ok": True, "slug": slug})


@app.post("/admin/api/scan")
async def api_scan(request: Request):
    teacher = await get_current_teacher(request)
    if not teacher:
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    count = await scan_interactives()
    return JSONResponse({"ok": True, "new_count": count})


@app.get("/admin/logout")
async def admin_logout():
    response = RedirectResponse(url="/admin", status_code=303)
    response.delete_cookie("teacher_session")
    return response


# ---------------------------------------------------------------------------
# ADMIN-ONLY — Teacher management
# ---------------------------------------------------------------------------
@app.get("/admin/teachers", response_class=HTMLResponse)
async def admin_teachers_page(request: Request):
    teacher = await get_current_teacher(request)
    if not teacher or teacher.get("role") != "admin":
        return RedirectResponse(url="/admin", status_code=303)

    teachers_list = await db.teachers.find().to_list(length=100)
    csrf = generate_csrf_token(teacher["name"])
    return templates.TemplateResponse(
        "admin_teachers.html",
        {"request": request, "teacher": teacher, "teachers": teachers_list, "csrf_token": csrf},
    )


@app.post("/admin/api/add-teacher")
async def api_add_teacher(request: Request):
    teacher = await get_current_teacher(request)
    if not teacher or teacher.get("role") != "admin":
        return JSONResponse({"error": "Unauthorized"}, status_code=401)

    data = await request.json()
    name = data.get("name", "").strip()
    email = data.get("email", "").strip()
    password = data.get("password", "").strip()
    role = data.get("role", "teacher")
    subjects = data.get("subjects", [])

    if not name or not email or not password:
        return JSONResponse({"error": "Name, email, and password are required"}, status_code=400)

    existing = await db.teachers.find_one({"$or": [{"name": name}, {"email": email}]})
    if existing:
        return JSONResponse({"error": "A teacher with that name or email already exists"}, status_code=409)

    pw_hash = bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode()
    await db.teachers.insert_one({
        "name": name, "email": email, "password_hash": pw_hash,
        "role": role if role in ("admin", "teacher") else "teacher",
        "subjects": subjects, "created_at": datetime.now(timezone.utc),
    })
    return JSONResponse({"ok": True})


@app.post("/admin/api/remove-teacher")
async def api_remove_teacher(request: Request):
    teacher = await get_current_teacher(request)
    if not teacher or teacher.get("role") != "admin":
        return JSONResponse({"error": "Unauthorized"}, status_code=401)

    data = await request.json()
    email = data.get("email", "").strip()
    if not email:
        return JSONResponse({"error": "Email required"}, status_code=400)

    if email == teacher.get("email"):
        return JSONResponse({"error": "Cannot remove your own account"}, status_code=400)

    target = await db.teachers.find_one({"email": email})
    if not target:
        return JSONResponse({"error": "Teacher not found"}, status_code=404)

    # Reassign their interactives to the admin performing the removal
    await db.interactives.update_many(
        {"uploaded_by": target["name"]},
        {"$set": {"uploaded_by": teacher["name"], "uploaded_by_email": teacher.get("email")}},
    )

    await db.teachers.delete_one({"email": email})
    return JSONResponse({"ok": True})


@app.post("/admin/api/change-password")
async def api_change_password(request: Request):
    teacher = await get_current_teacher(request)
    if not teacher:
        return JSONResponse({"error": "Unauthorized"}, status_code=401)

    data = await request.json()
    current_password = data.get("current_password", "").strip()
    new_password = data.get("new_password", "").strip()
    confirm_password = data.get("confirm_password", "").strip()

    if not current_password or not new_password or not confirm_password:
        return JSONResponse({"error": "All fields are required"}, status_code=400)
    if new_password != confirm_password:
        return JSONResponse({"error": "New passwords do not match"}, status_code=400)
    if len(new_password) < 8:
        return JSONResponse({"error": "Password must be at least 8 characters"}, status_code=400)
    if not bcrypt.checkpw(current_password.encode(), teacher["password_hash"].encode()):
        return JSONResponse({"error": "Current password is incorrect"}, status_code=401)

    new_hash = bcrypt.hashpw(new_password.encode(), bcrypt.gensalt()).decode()
    await db.teachers.update_one(
        {"email": teacher["email"]},
        {"$set": {"password_hash": new_hash, "updated_at": datetime.now(timezone.utc)}},
    )
    return JSONResponse({"ok": True})


# ---------------------------------------------------------------------------
# ADMIN-ONLY — Invite management
# ---------------------------------------------------------------------------
@app.get("/admin/invites", response_class=HTMLResponse)
async def admin_invites_page(request: Request):
    teacher = await get_current_teacher(request)
    if not teacher or teacher.get("role") != "admin":
        return RedirectResponse(url="/admin", status_code=303)

    invites_list = await db.invites.find().sort("created_at", -1).to_list(length=100)
    # Strip timezone info so Jinja comparisons don't mix naive/aware datetimes
    for inv in invites_list:
        if inv.get("expires_at") and inv["expires_at"].tzinfo is not None:
            inv["expires_at"] = inv["expires_at"].replace(tzinfo=None)
    csrf = generate_csrf_token(teacher["name"])
    return templates.TemplateResponse(
        "admin_invites.html",
        {
            "request": request, "teacher": teacher,
            "invites": invites_list, "csrf_token": csrf,
            "now": datetime.utcnow(),
        },
    )


@app.post("/admin/api/create-invite")
async def api_create_invite(request: Request):
    teacher = await get_current_teacher(request)
    if not teacher or teacher.get("role") != "admin":
        return JSONResponse({"error": "Unauthorized"}, status_code=401)

    data = await request.json()
    email_hint = data.get("email_hint", "").strip() or None
    token = secrets.token_hex(16)
    now = datetime.now(timezone.utc)

    await db.invites.insert_one({
        "token": token,
        "created_by": teacher["name"],
        "created_by_email": teacher.get("email"),
        "created_at": now,
        "expires_at": now + timedelta(days=7),
        "email_hint": email_hint,
        "max_uses": 10,
        "use_count": 0,
        "registrations": [],
    })

    return JSONResponse({"ok": True, "token": token})


@app.post("/admin/api/delete-invite")
async def api_delete_invite(request: Request):
    teacher = await get_current_teacher(request)
    if not teacher or teacher.get("role") != "admin":
        return JSONResponse({"error": "Unauthorized"}, status_code=401)

    data = await request.json()
    token = data.get("token", "").strip()
    if not token:
        return JSONResponse({"error": "Token required"}, status_code=400)

    result = await db.invites.delete_one({"token": token})
    if result.deleted_count == 0:
        return JSONResponse({"error": "Invite not found"}, status_code=404)
    return JSONResponse({"ok": True})
