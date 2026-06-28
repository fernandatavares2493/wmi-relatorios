"""
Portal de Relatórios WMI — Backend
FastAPI + PostgreSQL (asyncpg) + MinIO (S3)

Autenticação: SSO Microsoft da plataforma WMI (header X-Auth-Request-Email,
injetado pelo proxy / repassado pelo nginx do frontend). Autorização por papéis
na tabela user_roles — apenas admins criam/editam/excluem.
"""
from fastapi import FastAPI, UploadFile, File, HTTPException, Form, Depends, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse, JSONResponse
from starlette.concurrency import run_in_threadpool
from contextlib import asynccontextmanager
from typing import Optional
from urllib.parse import quote
import asyncpg
import boto3
from botocore.exceptions import ClientError
from botocore.config import Config
import os
import re
import uuid

# ──────────────────────────────────────────
# CONFIG (via env vars)
# ──────────────────────────────────────────
DATABASE_URL   = os.environ["DATABASE_URL"]          # postgres://user:pass@host:5432/db
MINIO_URL      = os.environ["MINIO_URL"]             # http://minio:9000
MINIO_ACCESS   = os.environ["MINIO_ACCESS_KEY"]
MINIO_SECRET   = os.environ["MINIO_SECRET_KEY"]
MINIO_BUCKET   = os.environ.get("MINIO_BUCKET", "relatorios")
ALLOWED_ORIGIN = os.environ.get("FRONTEND_URL", "http://localhost:3000")
# Segredo compartilhado com o nginx do frontend (defesa em profundidade): se definido,
# o backend só aceita requests que tragam o header X-Internal-Proxy com este valor —
# impede que alguém com acesso à rede interna fale direto com o backend forjando o SSO.
INTERNAL_PROXY_SECRET = os.environ.get("INTERNAL_PROXY_SECRET")

# Ambiente: em "development" aceitamos um e-mail de fallback (sem o proxy SSO na frente).
APP_ENV        = os.environ.get("APP_ENV", "production").lower()
DEV_USER_EMAIL = os.environ.get("DEV_USER_EMAIL")
# E-mails que recebem papel admin automaticamente (separados por vírgula).
ADMIN_EMAILS   = {e.strip().lower() for e in os.environ.get("ADMIN_EMAILS", "").split(",") if e.strip()}
# Tamanho máximo de upload por arquivo.
MAX_UPLOAD_MB    = int(os.environ.get("MAX_UPLOAD_MB", "50"))
MAX_UPLOAD_BYTES = MAX_UPLOAD_MB * 1024 * 1024

# Mime types que podem ser servidos inline no navegador (resto vira download).
INLINE_MIME_PREFIXES = ("image/",)
INLINE_MIME_EXACT    = {"application/pdf"}

# ──────────────────────────────────────────
# DB pool + MinIO client
# ──────────────────────────────────────────
db_pool: asyncpg.Pool = None
s3 = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    global db_pool, s3

    # PostgreSQL
    db_pool = await asyncpg.create_pool(DATABASE_URL, min_size=2, max_size=10)
    async with db_pool.acquire() as conn:
        await conn.execute(SCHEMA_SQL)
        # Garante papel admin para os e-mails configurados (idempotente).
        for email in ADMIN_EMAILS:
            await conn.execute(
                """INSERT INTO user_roles (email, role) VALUES ($1, 'admin')
                   ON CONFLICT (email) DO UPDATE SET role='admin'""",
                email,
            )

    # MinIO / S3
    s3 = boto3.client(
        "s3",
        endpoint_url=MINIO_URL,
        aws_access_key_id=MINIO_ACCESS,
        aws_secret_access_key=MINIO_SECRET,
        config=Config(signature_version="s3v4"),
        region_name="us-east-1",
    )
    # Cria bucket se não existir (só em 404 — não mascara erro de credencial/permissão)
    try:
        s3.head_bucket(Bucket=MINIO_BUCKET)
    except ClientError as e:
        code = str(e.response.get("Error", {}).get("Code", ""))
        if code in ("404", "NoSuchBucket"):
            s3.create_bucket(Bucket=MINIO_BUCKET)
        else:
            raise

    yield

    await db_pool.close()


app = FastAPI(title="Portal de Relatórios WMI", version="1.1.0", lifespan=lifespan)

# CORS: nunca "*". Em produção o tráfego é mesma-origem (nginx faz proxy de /api),
# mas mantemos a origem do front liberada para acesso direto em dev.
app.add_middleware(
    CORSMiddleware,
    allow_origins=[ALLOWED_ORIGIN],
    allow_credentials=True,
    allow_methods=["GET", "POST", "PATCH", "DELETE", "OPTIONS"],
    allow_headers=["*"],
)

# ──────────────────────────────────────────
# SCHEMA
# ──────────────────────────────────────────
SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS categories (
    id     SERIAL PRIMARY KEY,
    name   TEXT UNIQUE NOT NULL,
    color  TEXT DEFAULT 'blue',
    is_default BOOLEAN DEFAULT FALSE,
    created_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS reports (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    name        TEXT NOT NULL,
    description TEXT,
    author      TEXT,
    application TEXT DEFAULT '',
    category    TEXT NOT NULL,
    created_at  TIMESTAMPTZ DEFAULT NOW(),
    updated_at  TIMESTAMPTZ DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS report_files (
    id          SERIAL PRIMARY KEY,
    report_id   UUID REFERENCES reports(id) ON DELETE CASCADE,
    role        TEXT NOT NULL,  -- 'main' | 'extra' | 'pdf' | 'img'
    original_name TEXT NOT NULL,
    storage_key TEXT NOT NULL,  -- chave no MinIO
    mime_type   TEXT,
    size_bytes  BIGINT,
    created_at  TIMESTAMPTZ DEFAULT NOW()
);

-- Papéis de usuário (autorização). Chave = e-mail do SSO.
CREATE TABLE IF NOT EXISTS user_roles (
    email      TEXT PRIMARY KEY,
    role       TEXT NOT NULL DEFAULT 'user',  -- 'user' | 'admin'
    created_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_report_files_report ON report_files(report_id);
CREATE INDEX IF NOT EXISTS idx_reports_category ON reports(category);
ALTER TABLE reports ADD COLUMN IF NOT EXISTS application TEXT DEFAULT '';
CREATE INDEX IF NOT EXISTS idx_reports_created ON reports(created_at DESC);

-- Categorias padrão WMI
INSERT INTO categories (name, color, is_default) VALUES
    ('GERENCIAL',   'blue',   TRUE),
    ('ATENDIMENTO', 'cyan',   TRUE),
    ('FINANCEIRO',  'green',  TRUE),
    ('FATURAMENTO', 'yellow', TRUE)
ON CONFLICT (name) DO NOTHING;
"""

# ──────────────────────────────────────────
# AUTENTICAÇÃO / AUTORIZAÇÃO (SSO da plataforma)
# ──────────────────────────────────────────
async def current_user(request: Request) -> dict:
    """
    Lê a identidade injetada pelo SSO Microsoft (header X-Auth-Request-Email).
    Em produção, sem header → 403 (login é responsabilidade do proxy).
    Em desenvolvimento, cai no DEV_USER_EMAIL. Provisiona o usuário na primeira
    visita (lazy provisioning) com papel 'user', ou 'admin' se estiver em ADMIN_EMAILS.
    """
    # Defesa em profundidade: exige o segredo injetado pelo nginx do frontend.
    if INTERNAL_PROXY_SECRET and request.headers.get("x-internal-proxy") != INTERNAL_PROXY_SECRET:
        raise HTTPException(403, "Origem não autorizada")

    email = request.headers.get("x-auth-request-email")
    if not email and APP_ENV == "development":
        email = DEV_USER_EMAIL
    if not email:
        raise HTTPException(403, "Sem identidade SSO")

    email = email.strip().lower()
    name = request.headers.get("x-auth-request-user") or email

    async with db_pool.acquire() as conn:
        row = await conn.fetchrow("SELECT role FROM user_roles WHERE email=$1", email)
        if row is None:
            role = "admin" if email in ADMIN_EMAILS else "user"
            await conn.execute(
                """INSERT INTO user_roles (email, role) VALUES ($1, $2)
                   ON CONFLICT (email) DO NOTHING""",
                email, role,
            )
        else:
            role = row["role"]

    return {"email": email, "name": name, "role": role, "is_admin": role == "admin"}


async def require_admin(user: dict = Depends(current_user)) -> dict:
    if not user["is_admin"]:
        raise HTTPException(403, "Requer privilégio de administrador")
    return user


# ──────────────────────────────────────────
# HELPERS
# ──────────────────────────────────────────
async def get_db():
    async with db_pool.acquire() as conn:
        yield conn


def parse_uuid(value: str) -> uuid.UUID:
    try:
        return uuid.UUID(value)
    except (ValueError, AttributeError, TypeError):
        raise HTTPException(400, "ID inválido")


def safe_storage_name(name: str) -> str:
    """Nome seguro para compor a chave do MinIO — evita path traversal/colisão."""
    base = os.path.basename((name or "").replace("\\", "/").split("/")[-1])
    base = re.sub(r"[^A-Za-z0-9._-]", "_", base).strip("._")
    return (base or "arquivo")[:120]


def make_storage_key(report_id, role: str, filename: str) -> str:
    """Chave única por upload. O prefixo aleatório garante que um arquivo novo nunca
    colida com a versão anterior do mesmo role — evita que a remoção do antigo (após
    substituição) apague o objeto recém-enviado."""
    return f"{report_id}/{role}/{uuid.uuid4().hex[:12]}-{safe_storage_name(filename)}"


def clean_display_name(name: str) -> str:
    """Nome para exibição/Content-Disposition — remove controles/quebras de linha."""
    return re.sub(r"[\r\n\x00-\x1f\x7f]", "", (name or "")).strip()[:255] or "arquivo"


def content_disposition(filename: str, as_attachment: bool) -> str:
    disp = "attachment" if as_attachment else "inline"
    ascii_fallback = re.sub(r"[^A-Za-z0-9._-]", "_", filename) or "arquivo"
    return f"{disp}; filename=\"{ascii_fallback}\"; filename*=UTF-8''{quote(filename)}"


async def read_limited(upload: UploadFile) -> bytes:
    """Lê o upload em pedaços, abortando se exceder o limite (evita DoS por arquivo gigante)."""
    chunks, size = [], 0
    while True:
        chunk = await upload.read(1024 * 1024)
        if not chunk:
            break
        size += len(chunk)
        if size > MAX_UPLOAD_BYTES:
            raise HTTPException(413, f"Arquivo excede o limite de {MAX_UPLOAD_MB} MB")
        chunks.append(chunk)
    return b"".join(chunks)


def _put_object(file_bytes: bytes, key: str, content_type: str):
    s3.put_object(Bucket=MINIO_BUCKET, Key=key, Body=file_bytes, ContentType=content_type)


def _get_object(key: str) -> bytes:
    obj = s3.get_object(Bucket=MINIO_BUCKET, Key=key)
    return obj["Body"].read()


def delete_from_minio(key: str):
    try:
        s3.delete_object(Bucket=MINIO_BUCKET, Key=key)
    except Exception:
        pass


async def ensure_category_exists(conn, category: str):
    ok = await conn.fetchval("SELECT 1 FROM categories WHERE name=$1", category)
    if not ok:
        raise HTTPException(400, f"Categoria inexistente: {category}")


async def serialize_report(conn, report_uuid: uuid.UUID) -> dict:
    row = await conn.fetchrow("SELECT * FROM reports WHERE id=$1", report_uuid)
    if not row:
        raise HTTPException(404, "Relatório não encontrado")
    d = dict(row)
    d["created_at"] = d["created_at"].isoformat()
    d["updated_at"] = d["updated_at"].isoformat()

    files = await conn.fetch(
        "SELECT * FROM report_files WHERE report_id=$1 ORDER BY role", report_uuid
    )
    d["files"] = []
    for f in files:
        # Não expõe storage_key. Download e view passam pelo proxy autenticado.
        d["files"].append({
            "id": f["id"],
            "role": f["role"],
            "original_name": f["original_name"],
            "mime_type": f["mime_type"],
            "size_bytes": f["size_bytes"],
            "created_at": f["created_at"].isoformat(),
            "view_url": f'/api/files/view/{f["id"]}',
            "download_url": f'/api/files/view/{f["id"]}?download=1',
        })
    return d


# ──────────────────────────────────────────
# HEALTH (sem auth — checa dependências críticas)
# ──────────────────────────────────────────
@app.get("/health")
async def health():
    try:
        async with db_pool.acquire() as conn:
            await conn.execute("SELECT 1")
        await run_in_threadpool(s3.head_bucket, Bucket=MINIO_BUCKET)
        return {"status": "ok"}
    except Exception:
        return JSONResponse(status_code=503, content={"status": "degraded"})


# ──────────────────────────────────────────
# ME (identidade do usuário logado)
# ──────────────────────────────────────────
@app.get("/api/me")
async def me(user: dict = Depends(current_user)):
    return user


# ──────────────────────────────────────────
# CATEGORIES
# ──────────────────────────────────────────
@app.get("/api/categories")
async def list_categories(user: dict = Depends(current_user), conn=Depends(get_db)):
    rows = await conn.fetch("SELECT * FROM categories ORDER BY is_default DESC, name")
    return [dict(r) for r in rows]


@app.post("/api/categories")
async def create_category(body: dict, user: dict = Depends(require_admin), conn=Depends(get_db)):
    name = body.get("name", "").strip().upper()
    color = body.get("color", "blue")
    if not name:
        raise HTTPException(400, "Nome obrigatório")
    try:
        row = await conn.fetchrow(
            "INSERT INTO categories (name, color) VALUES ($1, $2) RETURNING *", name, color
        )
        return dict(row)
    except asyncpg.UniqueViolationError:
        raise HTTPException(409, "Categoria já existe")


@app.patch("/api/categories/{name}")
async def rename_category(name: str, body: dict, user: dict = Depends(require_admin), conn=Depends(get_db)):
    new_name = body.get("name", "").strip().upper()
    if not new_name:
        raise HTTPException(400, "Nome obrigatório")
    if new_name == name:
        return {"name": name}
    exists = await conn.fetchval("SELECT 1 FROM categories WHERE name=$1", new_name)
    if exists:
        raise HTTPException(409, "Categoria já existe")
    async with conn.transaction():
        await conn.execute("UPDATE categories SET name=$1 WHERE name=$2", new_name, name)
        await conn.execute("UPDATE reports SET category=$1 WHERE category=$2", new_name, name)
    return {"name": new_name}


@app.delete("/api/categories/{name}")
async def delete_category(name: str, user: dict = Depends(require_admin), conn=Depends(get_db)):
    in_use = await conn.fetchval("SELECT 1 FROM reports WHERE category=$1 LIMIT 1", name)
    if in_use:
        raise HTTPException(409, "Categoria em uso por relatórios existentes")
    await conn.execute("DELETE FROM categories WHERE name=$1", name)
    return {"deleted": name}


# ──────────────────────────────────────────
# REPORTS — LIST
# ──────────────────────────────────────────
@app.get("/api/reports")
async def list_reports(
    q: Optional[str] = None,
    category: Optional[str] = None,
    application: Optional[str] = None,
    user: dict = Depends(current_user),
    conn=Depends(get_db),
):
    filters = ["1=1"]
    params = []
    if q:
        # Escapa curingas do LIKE para evitar wildcard injection.
        like = "%" + q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
        params.append(like)
        filters.append(
            f"(r.name ILIKE ${len(params)} ESCAPE '\\' OR r.description ILIKE ${len(params)} ESCAPE '\\')"
        )
    if category:
        params.append(category)
        filters.append(f"r.category = ${len(params)}")
    if application:
        params.append(application)
        filters.append(f"r.application = ${len(params)}")

    sql = f"""
        SELECT r.*,
               COUNT(f.id) AS file_count,
               (SELECT pf.id FROM report_files pf
                 WHERE pf.report_id = r.id AND pf.role = 'img'
                 LIMIT 1) AS preview_id
        FROM reports r
        LEFT JOIN report_files f ON f.report_id = r.id
        WHERE {' AND '.join(filters)}
        GROUP BY r.id
        ORDER BY r.created_at DESC
    """
    rows = await conn.fetch(sql, *params)

    result = []
    for row in rows:
        d = dict(row)
        d["created_at"] = d["created_at"].isoformat()
        d["updated_at"] = d["updated_at"].isoformat()
        result.append(d)
    return result


# ──────────────────────────────────────────
# REPORTS — GET ONE
# ──────────────────────────────────────────
@app.get("/api/reports/{report_id}")
async def get_report(report_id: str, user: dict = Depends(current_user), conn=Depends(get_db)):
    return await serialize_report(conn, parse_uuid(report_id))


# ──────────────────────────────────────────
# REPORTS — CREATE
# ──────────────────────────────────────────
@app.post("/api/reports")
async def create_report(
    name:        str = Form(...),
    category:    str = Form(...),
    description: str = Form(""),
    author:      str = Form(""),
    application: str = Form(""),
    main_file:   UploadFile = File(...),
    extra_file:  Optional[UploadFile] = File(None),
    pdf_file:    Optional[UploadFile] = File(None),
    img_file:    Optional[UploadFile] = File(None),
    user: dict = Depends(require_admin),
    conn=Depends(get_db),
):
    await ensure_category_exists(conn, category)
    report_id = uuid.uuid4()
    uploaded_keys: list[str] = []
    try:
        files_meta = []
        for upload, role in [(main_file, "main"), (extra_file, "extra"),
                             (pdf_file, "pdf"), (img_file, "img")]:
            if not upload or not upload.filename:
                continue
            content = await read_limited(upload)
            key = make_storage_key(report_id, role, upload.filename)
            mime = upload.content_type or "application/octet-stream"
            await run_in_threadpool(_put_object, content, key, mime)
            uploaded_keys.append(key)
            files_meta.append((role, clean_display_name(upload.filename), key, mime, len(content)))

        async with conn.transaction():
            await conn.execute(
                """INSERT INTO reports (id, name, description, author, category, application)
                   VALUES ($1, $2, $3, $4, $5, $6)""",
                report_id, name.strip(), description.strip(), author.strip(),
                category, application.strip(),
            )
            for role, oname, key, mime, size in files_meta:
                await conn.execute(
                    """INSERT INTO report_files
                       (report_id, role, original_name, storage_key, mime_type, size_bytes)
                       VALUES ($1, $2, $3, $4, $5, $6)""",
                    report_id, role, oname, key, mime, size,
                )
    except Exception:
        for key in uploaded_keys:
            delete_from_minio(key)
        raise

    return await serialize_report(conn, report_id)


# ──────────────────────────────────────────
# REPORTS — UPDATE
# ──────────────────────────────────────────
@app.patch("/api/reports/{report_id}")
async def update_report(
    report_id:   str,
    name:        Optional[str] = Form(None),
    category:    Optional[str] = Form(None),
    description: Optional[str] = Form(None),
    author:      Optional[str] = Form(None),
    application: Optional[str] = Form(None),
    main_file:   Optional[UploadFile] = File(None),
    extra_file:  Optional[UploadFile] = File(None),
    pdf_file:    Optional[UploadFile] = File(None),
    img_file:    Optional[UploadFile] = File(None),
    user: dict = Depends(require_admin),
    conn=Depends(get_db),
):
    rid = parse_uuid(report_id)
    exists = await conn.fetchval("SELECT 1 FROM reports WHERE id=$1", rid)
    if not exists:
        raise HTTPException(404, "Relatório não encontrado")
    if category is not None:
        await ensure_category_exists(conn, category)

    updates, params = [], []
    for field, val in [("name", name), ("category", category),
                       ("description", description), ("author", author),
                       ("application", application)]:
        if val is not None:
            params.append(val.strip())
            updates.append(f"{field}=${len(params)}")

    # Faz upload dos novos arquivos primeiro (fora da transação do DB).
    uploaded_keys: list[str] = []     # rollback se o DB falhar
    old_keys_to_delete: list[str] = []  # apaga só após commit
    try:
        replacements = []
        for upload, role in [(main_file, "main"), (extra_file, "extra"),
                             (pdf_file, "pdf"), (img_file, "img")]:
            if not upload or not upload.filename:
                continue
            content = await read_limited(upload)
            key = make_storage_key(rid, role, upload.filename)
            mime = upload.content_type or "application/octet-stream"
            await run_in_threadpool(_put_object, content, key, mime)
            uploaded_keys.append(key)
            replacements.append((role, clean_display_name(upload.filename), key, mime, len(content)))

        async with conn.transaction():
            if updates:
                params.append(rid)
                await conn.execute(
                    f"UPDATE reports SET {', '.join(updates)}, updated_at=NOW() WHERE id=${len(params)}",
                    *params,
                )
            for role, oname, key, mime, size in replacements:
                old = await conn.fetchrow(
                    "SELECT storage_key FROM report_files WHERE report_id=$1 AND role=$2", rid, role
                )
                if old:
                    old_keys_to_delete.append(old["storage_key"])
                    await conn.execute(
                        "DELETE FROM report_files WHERE report_id=$1 AND role=$2", rid, role
                    )
                await conn.execute(
                    """INSERT INTO report_files
                       (report_id, role, original_name, storage_key, mime_type, size_bytes)
                       VALUES ($1, $2, $3, $4, $5, $6)""",
                    rid, role, oname, key, mime, size,
                )
    except Exception:
        for key in uploaded_keys:
            delete_from_minio(key)
        raise

    # Commit ok → remove os antigos do storage.
    for key in old_keys_to_delete:
        delete_from_minio(key)

    return await serialize_report(conn, rid)


# ──────────────────────────────────────────
# REPORTS — DELETE
# ──────────────────────────────────────────
@app.delete("/api/reports/{report_id}")
async def delete_report(report_id: str, user: dict = Depends(require_admin), conn=Depends(get_db)):
    rid = parse_uuid(report_id)
    files = await conn.fetch("SELECT storage_key FROM report_files WHERE report_id=$1", rid)
    result = await conn.execute("DELETE FROM reports WHERE id=$1", rid)
    if result.rsplit(" ", 1)[-1] == "0":   # command tag "DELETE N"
        raise HTTPException(404, "Relatório não encontrado")
    for f in files:
        delete_from_minio(f["storage_key"])
    return {"deleted": report_id}


# ──────────────────────────────────────────
# FILE PROXY — serve arquivo do MinIO direto ao navegador
# (resolve acesso interno e mantém o download autenticado)
# ──────────────────────────────────────────
@app.get("/api/files/view/{file_id}")
async def view_file(
    file_id: int,
    download: bool = False,
    user: dict = Depends(current_user),
    conn=Depends(get_db),
):
    row = await conn.fetchrow(
        "SELECT storage_key, mime_type, original_name FROM report_files WHERE id=$1", file_id
    )
    if not row:
        raise HTTPException(404, "Arquivo não encontrado")

    try:
        data = await run_in_threadpool(_get_object, row["storage_key"])
    except ClientError:
        raise HTTPException(404, "Arquivo não encontrado no storage")

    mime = row["mime_type"] or "application/octet-stream"
    viewable = mime in INLINE_MIME_EXACT or mime.startswith(INLINE_MIME_PREFIXES)
    as_attachment = download or not viewable
    # Nunca serve tipos não-visualizáveis (ex.: text/html) com o mime do cliente:
    # força octet-stream para impedir XSS armazenado servido na mesma origem.
    served_mime = mime if (viewable and not download) else "application/octet-stream"

    filename = clean_display_name(row["original_name"])
    return StreamingResponse(
        iter([data]),
        media_type=served_mime,
        headers={
            "Content-Disposition": content_disposition(filename, as_attachment),
            "X-Content-Type-Options": "nosniff",
        },
    )
