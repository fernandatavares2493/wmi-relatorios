# Portal de Relatórios WMI — Opção A (MinIO + PostgreSQL + FastAPI)

## Arquitetura

```
Usuários → Frontend (nginx:alpine, porta 80)
                │
                ▼
           Backend (FastAPI, porta 8000)
                │
        ┌───────┴────────┐
        ▼                ▼
  PostgreSQL 16      MinIO (S3)
  (metadados)      (arquivos binários)
```

## Estrutura do projeto

```
wmi-relatorios/
├── backend/
│   ├── main.py            ← API FastAPI completa
│   ├── requirements.txt
│   ├── Dockerfile         ← python:3.12-slim, multi-stage (~200 MB)
│   └── .dockerignore
├── frontend/
│   ├── index.html         ← Portal completo (conectado à API)
│   ├── nginx.conf
│   ├── Dockerfile         ← nginx:alpine (~15 MB)
│   └── .dockerignore
├── docker-compose.yml     ← Stack completa para dev local
└── README.md
```

---

## 1. Testar localmente (antes do deploy)

### Pré-requisito: Docker + Docker Compose instalados

```bash
# Na raiz do projeto:
docker compose up -d

# Acompanhar logs:
docker compose logs -f

# Verificar que todos subiram:
docker compose ps
```

Aguarde todos os serviços ficarem `healthy`, depois acesse:
- **Portal:** http://localhost:3000
- **API docs:** http://localhost:8000/docs
- **MinIO console:** http://localhost:9001 (login: minioadmin / minioadmin123)

### Testar healthchecks:
```bash
curl http://localhost:8000/health   # {"status":"ok"}
curl http://localhost:3000/health   # ok
```

---

## 2. Deploy no Coolify (produção)

São **3 serviços separados** no Coolify + **2 Resources** (banco e MinIO).

### Passo 1 — PostgreSQL (Resource)
No Coolify: **New Resource → Database → PostgreSQL 16**
- Name: `wmi-relatorios-db`
- Database: `relatorios`
- User: `wmi`
- Password: *(gere uma senha forte)*
- Anote a Internal Connection String: `postgres://wmi:<SENHA>@<HOST>:5432/relatorios`

### Passo 2 — MinIO (Resource)
No Coolify: **New Resource → Service → MinIO**
- Name: `wmi-relatorios-minio`
- Root user: `minioadmin`
- Root password: *(gere uma senha forte)*
- Anote: URL interna será `http://wmi-relatorios-minio:9000`

### Passo 3 — Backend (Application)
No Coolify: **New Resource → Application**
- Repo: seu repositório Git → pasta `backend/`
- Build pack: **Dockerfile**
- Porta: `8000`
- FQDN: `http://relatorios-api.apps.wmi.solutions`
- Memory: `256M` | CPU: `0.25`
- Healthcheck: path `/health`, interval 30s

**Env vars obrigatórias:**
```
DATABASE_URL      = postgres://wmi:<SENHA>@<HOST_POSTGRES>:5432/relatorios
MINIO_URL         = http://<HOST_MINIO>:9000
MINIO_ACCESS_KEY  = minioadmin
MINIO_SECRET_KEY  = <SENHA_MINIO>    ← marcar como Secret
MINIO_BUCKET      = relatorios
FRONTEND_URL      = https://relatorios.apps.wmi.solutions
```

### Passo 4 — Frontend (Application)
No Coolify: **New Resource → Application**
- Repo: seu repositório Git → pasta `frontend/`
- Build pack: **Dockerfile**
- Porta: `80`
- FQDN: `http://relatorios.apps.wmi.solutions`
- Memory: `64M` | CPU: `0.10`
- Healthcheck: path `/health`, interval 30s

**⚠️ Importante:** antes de fazer o deploy do frontend, edite `frontend/index.html` e troque a linha:

```javascript
const API = window.ENV_API_URL || 'http://localhost:8000';
```

Por:

```javascript
const API = 'https://relatorios-api.apps.wmi.solutions';
```

Ou passe via env var no Coolify + nginx (veja seção abaixo).

### Passo 5 — Deploy na ordem certa
1. Deploy PostgreSQL → aguardar `running:healthy`
2. Deploy MinIO → aguardar `running:healthy`
3. Deploy Backend → aguardar `running:healthy`
4. Deploy Frontend → aguardar `running:healthy`

---

## 3. Apontar a URL da API no frontend sem recompilar

Para não precisar rebuildar o frontend toda vez que a URL da API mudar, adicione no `nginx.conf` um endpoint que injeta a configuração:

```nginx
location /config.js {
    add_header Content-Type application/javascript;
    return 200 'window.ENV_API_URL = "$API_URL";';
}
```

E no `index.html`, antes do script principal:
```html
<script src="/config.js"></script>
```

---

## 4. Segurança para produção

- [ ] Troque todas as senhas padrão (postgres, minio)
- [ ] Marque `MINIO_SECRET_KEY` e `POSTGRES_PASSWORD` como **Secret** no Coolify
- [ ] Restrinja `FRONTEND_URL` no backend (não use `*`)
- [ ] Configure HTTPS (Coolify gerencia via Traefik + Let's Encrypt automaticamente)
- [ ] Configure backups do volume PostgreSQL (Coolify → Database → Backup)
- [ ] Crie um bucket policy no MinIO para bloquear acesso público direto

---

## 5. Endpoints da API

| Método | Endpoint | Descrição |
|--------|----------|-----------|
| GET | `/health` | Healthcheck |
| GET | `/api/categories` | Listar categorias |
| POST | `/api/categories` | Criar categoria |
| DELETE | `/api/categories/{name}` | Remover categoria |
| GET | `/api/reports` | Listar relatórios (filtra por `q` e `category`) |
| GET | `/api/reports/{id}` | Detalhes + URLs de download |
| POST | `/api/reports` | Criar relatório (multipart/form-data) |
| PATCH | `/api/reports/{id}` | Editar relatório |
| DELETE | `/api/reports/{id}` | Excluir relatório + arquivos |

Documentação interativa disponível em `/docs` (Swagger UI).

---

## 6. Estimativa de recursos no Coolify

| Serviço | RAM | CPU | Disco |
|---------|-----|-----|-------|
| PostgreSQL | 128M | 0.10 | ~50 MB + dados |
| MinIO | 128M | 0.10 | tamanho dos arquivos |
| Backend | 256M | 0.25 | ~200 MB (imagem) |
| Frontend | 64M | 0.05 | ~15 MB (imagem) |
| **Total** | **~576M** | **~0.5** | — |
