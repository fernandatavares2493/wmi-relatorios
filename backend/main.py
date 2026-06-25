"""
Portal de Relatórios WMI — Backend
FastAPI + PostgreSQL (asyncpg) + MinIO (S3)
"""
from fastapi import FastAPI, UploadFile, File, HTTPException, Form, Depends
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse, JSONResponse
from contextlib import asynccontextmanager
from typing import Optional
import asyncpg
import boto3
from botocore.exceptions import ClientError
from botocore.config import Config
import os
import uuid
import json
from datetime import datetime

# ──────────────────────────────────────────
# CONFIG (via env vars)
# ──────────────────────────────────────────
DATABASE_URL  = os.environ["DATABASE_URL"]          # postgres://user:pass@host:5432/db
MINIO_URL     = os.environ["MINIO_URL"]             # http://minio:9000
MINIO_ACCESS  = os.environ["MINIO_ACCESS_KEY"]
MINIO_SECRET  = os.environ["MINIO_SECRET_KEY"]
MINIO_BUCKET  = os.environ.get("MINIO_BUCKET", "relatorios")
ALLOWED_ORIGIN = os.environ.get("FRONTEND_URL", "*")

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

    # MinIO / S3
    s3 = boto3.client(
        "s3",
        endpoint_url=MINIO_URL,
        aws_access_key_id=MINIO_ACCESS,
        aws_secret_access_key=MINIO_SECRET,
        config=Config(signature_version="s3v4"),
        region_name="us-east-1",
    )
    # Cria bucket se não existir
    try:
        s3.head_bucket(Bucket=MINIO_BUCKET)
    except ClientError:
        s3.create_bucket(Bucket=MINIO_BUCKET)

    yield

    await db_pool.close()

app = FastAPI(title="Portal de Relatórios WMI", version="1.0.0", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=[ALLOWED_ORIGIN] if ALLOWED_ORIGIN != "*" else ["*"],
    allow_methods=["*"],
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
# HELPERS
# ──────────────────────────────────────────
async def get_db():
    async with db_pool.acquire() as conn:
        yield conn

def upload_to_minio(file_bytes: bytes, key: str, content_type: str):
    s3.put_object(
        Bucket=MINIO_BUCKET,
        Key=key,
        Body=file_bytes,
        ContentType=content_type,
    )

def presigned_url(key: str, expires: int = 3600) -> str:
    return s3.generate_presigned_url(
        "get_object",
        Params={"Bucket": MINIO_BUCKET, "Key": key},
        ExpiresIn=expires,
    )

def delete_from_minio(key: str):
    try:
        s3.delete_object(Bucket=MINIO_BUCKET, Key=key)
    except Exception:
        pass

# ──────────────────────────────────────────
# HEALTH
# ──────────────────────────────────────────
@app.get("/health")
async def health():
    return {"status": "ok"}

# ──────────────────────────────────────────
# CATEGORIES
# ──────────────────────────────────────────
@app.get("/api/categories")
async def list_categories(conn=Depends(get_db)):
    rows = await conn.fetch("SELECT * FROM categories ORDER BY is_default DESC, name")
    return [dict(r) for r in rows]

@app.post("/api/categories")
async def create_category(body: dict, conn=Depends(get_db)):
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
async def rename_category(name: str, body: dict, conn=Depends(get_db)):
    new_name = body.get("name", "").strip().upper()
    if not new_name:
        raise HTTPException(400, "Nome obrigatório")
    if new_name == name:
        return {"name": name}
    # Check new name not already taken
    exists = await conn.fetchval("SELECT 1 FROM categories WHERE name=$1", new_name)
    if exists:
        raise HTTPException(409, "Categoria já existe")
    # Rename in categories table
    await conn.execute("UPDATE categories SET name=$1 WHERE name=$2", new_name, name)
    # Update all reports that use this category
    await conn.execute("UPDATE reports SET category=$1 WHERE category=$2", new_name, name)
    return {"name": new_name}

@app.delete("/api/categories/{name}")
async def delete_category(name: str, conn=Depends(get_db)):
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
    conn=Depends(get_db)
):
    filters = ["1=1"]
    params = []
    if q:
        params.append(f"%{q}%")
        filters.append(f"(r.name ILIKE ${len(params)} OR r.description ILIKE ${len(params)})")
    if category:
        params.append(category)
        filters.append(f"r.category = ${len(params)}")
    if application:
        params.append(application)
        filters.append(f"r.application = ${len(params)}")

    sql = f"""
        SELECT r.*,
               COUNT(f.id) AS file_count
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
        # Busca thumbnail (img) para preview
        img = await conn.fetchrow(
            "SELECT id FROM report_files WHERE report_id=$1 AND role='img' LIMIT 1",
            d["id"]
        )
        d["preview_id"] = img["id"] if img else None
        result.append(d)
    return result

# ──────────────────────────────────────────
# REPORTS — GET ONE
# ──────────────────────────────────────────
@app.get("/api/reports/{report_id}")
async def get_report(report_id: str, conn=Depends(get_db)):
    row = await conn.fetchrow("SELECT * FROM reports WHERE id=$1", uuid.UUID(report_id))
    if not row:
        raise HTTPException(404, "Relatório não encontrado")
    d = dict(row)
    d["created_at"] = d["created_at"].isoformat()
    d["updated_at"] = d["updated_at"].isoformat()

    files = await conn.fetch(
        "SELECT * FROM report_files WHERE report_id=$1 ORDER BY role", uuid.UUID(report_id)
    )
    d["files"] = []
    for f in files:
        fd = dict(f)
        fd["created_at"] = fd["created_at"].isoformat()
        fd["download_url"] = presigned_url(fd["storage_key"])
        fd["view_url"] = f'/api/files/view/{fd["id"]}'
        d["files"].append(fd)
    return d

# ──────────────────────────────────────────
# REPORTS — CREATE
# ──────────────────────────────────────────
@app.post("/api/reports")
async def create_report(
    name:        str = Form(...),
    category:    str = Form(...),
    description:  str = Form(""),
    author:       str = Form(""),
    application:  str = Form(""),
    main_file:   UploadFile = File(...),
    extra_file:  Optional[UploadFile] = File(None),
    pdf_file:    Optional[UploadFile] = File(None),
    img_file:    Optional[UploadFile] = File(None),
    conn=Depends(get_db),
):
    report_id = uuid.uuid4()

    await conn.execute(
        """INSERT INTO reports (id, name, description, author, category, application)
           VALUES ($1, $2, $3, $4, $5, $6)""",
        report_id, name.strip(), description.strip(), author.strip(), category, application.strip()
    )

    async def save_file(upload: UploadFile, role: str):
        if not upload or not upload.filename:
            return
        content = await upload.read()
        key = f"{report_id}/{role}/{upload.filename}"
        upload_to_minio(content, key, upload.content_type or "application/octet-stream")
        await conn.execute(
            """INSERT INTO report_files
               (report_id, role, original_name, storage_key, mime_type, size_bytes)
               VALUES ($1, $2, $3, $4, $5, $6)""",
            report_id, role, upload.filename, key,
            upload.content_type, len(content)
        )

    await save_file(main_file,  "main")
    await save_file(extra_file, "extra")
    await save_file(pdf_file,   "pdf")
    await save_file(img_file,   "img")

    return await get_report(str(report_id), conn)

# ──────────────────────────────────────────
# REPORTS — UPDATE
# ──────────────────────────────────────────
@app.patch("/api/reports/{report_id}")
async def update_report(
    report_id:   str,
    name:        Optional[str] = Form(None),
    category:    Optional[str] = Form(None),
    description:  Optional[str] = Form(None),
    author:       Optional[str] = Form(None),
    application:  Optional[str] = Form(None),
    main_file:   Optional[UploadFile] = File(None),
    extra_file:  Optional[UploadFile] = File(None),
    pdf_file:    Optional[UploadFile] = File(None),
    img_file:    Optional[UploadFile] = File(None),
    conn=Depends(get_db),
):
    rid = uuid.UUID(report_id)
    updates, params = [], []

    for field, val in [("name", name), ("category", category),
                       ("description", description), ("author", author),
                       ("application", application)]:
        if val is not None:
            params.append(val.strip())
            updates.append(f"{field}=${len(params)}")

    if updates:
        params.append(rid)
        await conn.execute(
            f"UPDATE reports SET {', '.join(updates)}, updated_at=NOW() WHERE id=${len(params)}",
            *params
        )

    async def replace_file(upload: UploadFile, role: str):
        if not upload or not upload.filename:
            return
        old = await conn.fetchrow(
            "SELECT storage_key FROM report_files WHERE report_id=$1 AND role=$2", rid, role
        )
        if old:
            delete_from_minio(old["storage_key"])
            await conn.execute(
                "DELETE FROM report_files WHERE report_id=$1 AND role=$2", rid, role
            )
        content = await upload.read()
        key = f"{rid}/{role}/{upload.filename}"
        upload_to_minio(content, key, upload.content_type or "application/octet-stream")
        await conn.execute(
            """INSERT INTO report_files
               (report_id, role, original_name, storage_key, mime_type, size_bytes)
               VALUES ($1, $2, $3, $4, $5, $6)""",
            rid, role, upload.filename, key, upload.content_type, len(content)
        )

    await replace_file(main_file,  "main")
    await replace_file(extra_file, "extra")
    await replace_file(pdf_file,   "pdf")
    await replace_file(img_file,   "img")

    return await get_report(report_id, conn)

# ──────────────────────────────────────────
# REPORTS — DELETE
# ──────────────────────────────────────────
@app.delete("/api/reports/{report_id}")
async def delete_report(report_id: str, conn=Depends(get_db)):
    rid = uuid.UUID(report_id)
    files = await conn.fetch("SELECT storage_key FROM report_files WHERE report_id=$1", rid)
    for f in files:
        delete_from_minio(f["storage_key"])
    await conn.execute("DELETE FROM reports WHERE id=$1", rid)
    return {"deleted": report_id}

# ──────────────────────────────────────────
# FILE PROXY — serve arquivo do MinIO direto
# para o navegador (resolve acesso interno)
# ──────────────────────────────────────────
@app.get("/api/files/view/{file_id}")
async def view_file(file_id: int, conn=Depends(get_db)):
    """
    Busca o arquivo no MinIO e serve diretamente ao navegador.
    Usado para visualizar PDF e imagens sem expor URL interna do MinIO.
    """
    row = await conn.fetchrow(
        "SELECT storage_key, mime_type, original_name FROM report_files WHERE id=$1", file_id
    )
    if not row:
        raise HTTPException(404, "Arquivo não encontrado")

    try:
        obj = s3.get_object(Bucket=MINIO_BUCKET, Key=row["storage_key"])
        data = obj["Body"].read()
    except ClientError:
        raise HTTPException(404, "Arquivo não encontrado no storage")

    mime = row["mime_type"] or "application/octet-stream"

    # Para PDF e imagens: inline (abre no navegador)
    # Para outros: attachment (força download)
    is_viewable = mime.startswith("image/") or mime == "application/pdf"
    disposition = f'inline; filename="{row["original_name"]}"' if is_viewable \
                  else f'attachment; filename="{row["original_name"]}"'

    return StreamingResponse(
        iter([data]),
        media_type=mime,
        headers={"Content-Disposition": disposition}
    )


# ──────────────────────────────────────────
# BATCH CREATE
# ──────────────────────────────────────────
@app.post("/api/reports/batch")
async def create_batch(
    items_json: str = Form(...),  # JSON array com metadados
    conn=Depends(get_db),
):
    """
    Recebe JSON com array de itens já classificados.
    Cada item deve ter: name, category, description, author, file_key (chave MinIO já enviada).
    """
    items = json.loads(items_json)
    created = []
    for item in items:
        report_id = uuid.uuid4()
        await conn.execute(
            "INSERT INTO reports (id, name, description, author, category) VALUES ($1,$2,$3,$4,$5)",
            report_id, item["name"], item.get("description",""),
            item.get("author",""), item["category"]
        )
        created.append(str(report_id))
    return {"created": len(created), "ids": created}
