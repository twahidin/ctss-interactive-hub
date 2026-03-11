import os
import time
import hashlib
from pathlib import Path
from datetime import datetime, timedelta, timezone
from collections import defaultdict

from fastapi import FastAPI, Request, Form, HTTPException, Depends
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

BASE_DIR = Path(__file__).resolve().parent
INTERACTIVES_DIR = BASE_DIR / "interactives"

# Subject colour map
SUBJECT_COLOURS = {
    "Computing": "#2196F3",
    "Physics": "#FF9800",
    "Chemistry": "#9C27B0",
    "Biology": "#F44336",
    "Mathematics": "#4CAF50",
    "Economics": "#009688",
}

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
                "name": "Mr Joe",
                "email": "joe@ctss.edu.sg",
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
    # Accept current hour and previous hour
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

# Make helpers available in all templates
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


async def require_teacher(request: Request):
    teacher = await get_current_teacher(request)
    if not teacher:
        raise HTTPException(status_code=303, headers={"Location": "/admin"})
    return teacher


async def require_admin(request: Request):
    teacher = await get_current_teacher(request)
    if not teacher or teacher.get("role") != "admin":
        raise HTTPException(status_code=303, headers={"Location": "/admin"})
    return teacher


# ---------------------------------------------------------------------------
# PUBLIC ROUTES — Students
# ---------------------------------------------------------------------------
@app.get("/", response_class=HTMLResponse)
async def student_home(request: Request):
    cursor = db.interactives.find({"is_active": True}).sort("subject", 1)
    interactives = await cursor.to_list(length=200)

    # Group by subject
    grouped: dict[str, list] = {}
    for item in interactives:
        subj = item["subject"]
        grouped.setdefault(subj, []).append(item)

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

    # If no passcode, redirect directly to viewer
    if not interactive.get("passcode"):
        return RedirectResponse(url=f"/i/{subject}/{name}/view", status_code=303)

    # Check if student already has valid session for this slug
    token = request.cookies.get(f"access_{slug.replace('/', '_')}")
    if token:
        payload = decode_token(token)
        if payload and payload.get("slug") == slug:
            return RedirectResponse(
                url=f"/i/{subject}/{name}/view", status_code=303
            )

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

    # CSRF check
    if not verify_csrf_token(csrf_token, slug):
        raise HTTPException(status_code=403, detail="Invalid request")

    # Rate limit
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
        # Log failed attempt
        await db.access_logs.insert_one(
            {
                "interactive_slug": slug,
                "timestamp": datetime.now(timezone.utc),
                "success": False,
            }
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

    # Log success
    await db.access_logs.insert_one(
        {
            "interactive_slug": slug,
            "timestamp": datetime.now(timezone.utc),
            "success": True,
        }
    )

    # Set session cookie
    token = create_token({"slug": slug, "type": "student"}, STUDENT_SESSION_HOURS)
    response = RedirectResponse(url=f"/i/{subject}/{name}/view", status_code=303)
    cookie_name = f"access_{slug.replace('/', '_')}"
    response.set_cookie(
        key=cookie_name,
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

    # Check access: either no passcode or valid session
    if interactive.get("passcode"):
        cookie_name = f"access_{slug.replace('/', '_')}"
        token = request.cookies.get(cookie_name)
        if not token:
            return RedirectResponse(url=f"/i/{subject}/{name}", status_code=303)
        payload = decode_token(token)
        if not payload or payload.get("slug") != slug:
            return RedirectResponse(url=f"/i/{subject}/{name}", status_code=303)

    # Check the HTML file exists
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
# ADMIN ROUTES — Teachers
# ---------------------------------------------------------------------------
@app.get("/admin", response_class=HTMLResponse)
async def admin_login_page(request: Request):
    # If already logged in, redirect to dashboard
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
    if not teacher or not bcrypt.checkpw(
        password.encode(), teacher["password_hash"].encode()
    ):
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
    response.set_cookie(
        key="teacher_session",
        value=token,
        httponly=True,
        max_age=TEACHER_SESSION_HOURS * 3600,
        samesite="lax",
    )
    return response


@app.get("/admin/dashboard", response_class=HTMLResponse)
async def admin_dashboard(request: Request):
    teacher = await get_current_teacher(request)
    if not teacher:
        return RedirectResponse(url="/admin", status_code=303)

    cursor = db.interactives.find().sort([("subject", 1), ("title", 1)])
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

    result = await db.interactives.update_one(
        {"slug": slug}, {"$set": update_fields}
    )
    if result.matched_count == 0:
        return JSONResponse({"error": "Not found"}, status_code=404)

    return JSONResponse({"ok": True})


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
# ADMIN-ONLY ROUTES — Teacher management
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
        {
            "request": request,
            "teacher": teacher,
            "teachers": teachers_list,
            "csrf_token": csrf,
        },
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
    await db.teachers.insert_one(
        {
            "name": name,
            "email": email,
            "password_hash": pw_hash,
            "role": role if role in ("admin", "teacher") else "teacher",
            "subjects": subjects,
            "created_at": datetime.now(timezone.utc),
        }
    )
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

    # Don't allow removing yourself
    if email == teacher.get("email"):
        return JSONResponse({"error": "Cannot remove your own account"}, status_code=400)

    result = await db.teachers.delete_one({"email": email})
    if result.deleted_count == 0:
        return JSONResponse({"error": "Teacher not found"}, status_code=404)

    return JSONResponse({"ok": True})
