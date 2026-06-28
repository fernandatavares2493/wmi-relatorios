# Portal de Relatórios WMI — Opção A (MinIO + PostgreSQL + FastAPI)

## Arquitetura

```
Usuários → SSO Microsoft (proxy da plataforma WMI)
                │  injeta header X-Auth-Request-Email
                ▼
        Frontend (nginx:alpine, porta 80)
                │  reverse-proxy de /api/  → mesma origem (sem CORS)
                │  repassa o header SSO ao backend
                ▼
           Backend (FastAPI, porta 8000)  ← interno, sem FQDN público
                │
        ┌───────┴────────┐
        ▼                ▼
  PostgreSQL 16      MinIO (S3)
  (metadados)      (arquivos binários)
```

**Acesso:**
- **Leitura/Download — PÚBLICO (sem login):** navegar, ver detalhes e baixar relatórios
  não exigem identidade. Clientes não precisam de conta/cadastro.
- **Escrita — ADMIN (com login):** publicar/editar/excluir exige o SSO Microsoft e papel
  `admin` (tabela `user_roles`, e-mail → `user`/`admin`). O app lê a identidade do header
  `X-Auth-Request-Email` injetado pelo proxy; sem ela, o usuário é anônimo (só leitura).

> ⚠️ **Importante (infra):** a plataforma WMI Coolify coloca login Microsoft **na frente de
> todo app** automaticamente. Para que clientes externos acessem **sem login**, a Infra WMI
> precisa configurar este app para **permitir acesso anônimo** (mantendo a injeção do header
> para quem está logado, para os admins continuarem publicando). Isso é uma configuração de
> infraestrutura — o código já está pronto, mas o acesso público depende desse ajuste no proxy.

## Estrutura do projeto

```
wmi-relatorios/
├── backend/
│   ├── main.py                 ← API FastAPI (SSO, roles, proxy de arquivos)
│   ├── requirements.txt
│   ├── Dockerfile              ← python:3.12-slim, multi-stage, usuário não-root
│   └── .dockerignore
├── frontend/
│   ├── index.html              ← Portal completo (mesma origem /api)
│   ├── default.conf.template   ← nginx + reverse-proxy + CSP (envsubst no start)
│   ├── Dockerfile              ← nginx:alpine
│   └── .dockerignore
├── docker-compose.yml          ← Stack completa para dev local
├── .env.example                ← Modelo de variáveis (copie para .env)
├── .gitignore
└── README-opcao-A.md
```

> **Segurança:** nenhum secret fica em código/compose. Tudo vem de variáveis de
> ambiente. Copie `.env.example` para `.env` (já ignorado pelo Git) e preencha.

---

## 1. Testar localmente (antes do deploy)

### Pré-requisito: Docker + Docker Compose instalados

```bash
# Na raiz do projeto:
cp .env.example .env          # preencha senhas e seu e-mail em ADMIN_EMAILS/DEV_USER_EMAIL
docker compose up -d --build

# Acompanhar logs:
docker compose logs -f

# Verificar que todos subiram:
docker compose ps
```

Aguarde os serviços ficarem `healthy`, depois acesse:
- **Portal:** http://localhost:3000
- **API docs:** http://localhost:8000/docs
- **MinIO console:** http://localhost:9001 (login: valores de `MINIO_ROOT_USER` / `MINIO_ROOT_PASSWORD` do seu `.env`)

> Em dev (`APP_ENV=development`), como não há o proxy SSO na frente, o backend usa
> `DEV_USER_EMAIL` como identidade. Coloque o mesmo e-mail em `ADMIN_EMAILS` para
> testar as funções de admin localmente.

### Testar healthchecks:
```bash
curl http://localhost:8000/health   # {"status":"ok"} (checa Postgres + MinIO)
curl http://localhost:3000/health   # ok (nginx)
```

---

## 2. Deploy no Coolify (produção)

São **2 Applications** (frontend e backend) + **2 Resources** (Postgres e MinIO).
O **backend não precisa de FQDN público** — o frontend faz proxy para ele pela rede
interna do Coolify. Apenas o **frontend** recebe o domínio público (e o SSO).

### Passo 1 — PostgreSQL (Resource)
**New Resource → Database → PostgreSQL 16**
- Name: `wmi-relatorios-db` · Database: `relatorios` · User: `wmi` · Password: *(forte)*
- Anote a Internal Connection String: `postgres://wmi:<SENHA>@<HOST>:5432/relatorios`

### Passo 2 — MinIO (Resource)
**New Resource → Service → MinIO**
- Name: `wmi-relatorios-minio` · Root user/password: *(fortes, marcar como Secret)*
- URL interna: `http://wmi-relatorios-minio:9000`

### Passo 3 — Backend (Application, interno)
**New Resource → Application** → repo Git, pasta `backend/`, build **Dockerfile**, porta `8000`.
- **Sem FQDN público** (ou restrito) — o acesso é interno pelo frontend.
- Memory: `512M` | CPU: `0.5`  ·  Healthcheck: `/health`

**Env vars (marcar segredos como Secret):**
```
DATABASE_URL     = postgres://wmi:<SENHA>@<HOST_POSTGRES>:5432/relatorios
MINIO_URL        = http://<HOST_MINIO>:9000
MINIO_ACCESS_KEY = <ROOT_USER_MINIO>
MINIO_SECRET_KEY = <ROOT_PASSWORD_MINIO>     ← Secret
MINIO_BUCKET     = relatorios
FRONTEND_URL     = http://relatorios.apps.wmi.solutions
APP_ENV          = production
ADMIN_EMAILS     = fulano@wmi.solutions,beltrano@wmi.solutions
MAX_UPLOAD_MB    = 50
INTERNAL_PROXY_SECRET = <segredo forte: openssl rand -hex 32>   ← Secret
```

### Passo 4 — Frontend (Application, público)
**New Resource → Application** → repo Git, pasta `frontend/`, build **Dockerfile**, porta `80`.
- FQDN: `http://relatorios.apps.wmi.solutions` (use `http://` — Apache faz o TLS externamente)
- Memory: `128M` | CPU: `0.25`  ·  Healthcheck: `/health`

**Env vars:**
```
API_UPSTREAM          = http://<HOST_INTERNO_DO_BACKEND>:8000
INTERNAL_PROXY_SECRET = <mesmo valor configurado no backend>   ← Secret
```
O nginx do frontend faz proxy de `/api/` para `API_UPSTREAM`, repassa o header SSO e
injeta o `INTERNAL_PROXY_SECRET` (que o backend exige). Nada precisa ser editado no
`index.html` — ele chama a API na mesma origem.

### Passo 5 — Deploy na ordem certa
1. PostgreSQL → `running:healthy`
2. MinIO → `running:healthy`
3. Backend → `running:healthy`
4. Frontend → `running:healthy`

> **Promover admins:** no primeiro acesso, cada usuário é criado com papel `user`.
> Os e-mails em `ADMIN_EMAILS` viram `admin` automaticamente. Para promover outros
> depois: `UPDATE user_roles SET role='admin' WHERE email='...';`

---

## 2.1 Acesso público de download (pedido para a Infra WMI)

**Objetivo:** clientes externos precisam **baixar relatórios sem fazer login** (sem conta
Microsoft / sem cadastro). A publicação (criar/editar/excluir) continua restrita à equipe WMI.

**O que o app já faz (pronto no código):**
- Leitura/listagem/download são **públicos** (não exigem identidade).
- Escrita exige `X-Auth-Request-Email` + papel `admin`. Sem identidade → o usuário é anônimo
  (só leitura). Logo, o app funciona tanto para clientes anônimos quanto para admins logados.

**O que falta (configuração de infraestrutura — não é código):**
A plataforma WMI Coolify protege **todo** app com login Microsoft por padrão. É preciso pedir à
Infra WMI para liberar o **acesso anônimo** a este app (frontend `relatorios.apps.wmi.solutions`),
no modo:

> **"Permitir acesso anônimo, mas continuar injetando os headers de identidade
> (`X-Auth-Request-Email`) quando o usuário estiver logado."**

No OAuth2-Proxy isso costuma ser feito com algo como `--skip-auth-route` / `skip_provider_button`
mantendo `set_xauthrequest=true`, ou marcando o app como público no painel. Resultado:
- **Cliente sem login** → passa como anônimo → navega e baixa.
- **Equipe WMI logada** → header injetado → publica/edita normalmente.

> ⚠️ **Atenção:** com isso, **qualquer pessoa com o link do portal pode baixar qualquer
> relatório** (não há separação por cliente). É o comportamento desejado para distribuição
> aberta. Restrição por cliente seria uma evolução futura (exigiria identificação do cliente).

---

## 3. Segurança (já aplicada / checklist de produção)

- [x] Sem secrets em código/compose — tudo via env vars + `.env` (gitignored)
- [x] Leitura/download **públicos** (por decisão de produto); escrita exige SSO + papel admin
- [x] Download/preview via proxy (`/api/files/view/{id}`) — MinIO nunca exposto diretamente
- [x] Upload sanitizado (sem path traversal) e com limite (`MAX_UPLOAD_MB`)
- [x] `Content-Disposition` seguro; tipos não-visualizáveis forçados a download
- [x] CORS restrito ao `FRONTEND_URL` (nunca `*`); tráfego de produção é mesma-origem
- [x] CSP + headers de segurança no nginx; escape de saída no frontend (anti-XSS)
- [x] `INTERNAL_PROXY_SECRET`: backend só aceita requests vindos do frontend (defesa em profundidade)
- [ ] Trocar todas as senhas padrão e marcá-las como **Secret** no Coolify
- [ ] Definir um `INTERNAL_PROXY_SECRET` forte e igual no backend e no frontend
- [ ] Backups do volume PostgreSQL (Coolify → Database → Backup)
- [ ] Confirmar que o proxy SSO **descarta** headers `X-Auth-Request-*` vindos do cliente
      (o app confia no e-mail injetado; o proxy é a fronteira de confiança)

---

## 4. Endpoints da API

| Método | Endpoint | Acesso | Descrição |
|--------|----------|--------|-----------|
| GET | `/health` | público | Healthcheck (checa Postgres + MinIO) |
| GET | `/api/me` | público | Identidade e papel (anônimo se sem login) |
| GET | `/api/categories` | público | Listar categorias |
| POST | `/api/categories` | admin | Criar categoria |
| PATCH | `/api/categories/{name}` | admin | Renomear categoria |
| DELETE | `/api/categories/{name}` | admin | Remover categoria |
| GET | `/api/reports` | público | Listar (filtros `q`, `category`, `application`) |
| GET | `/api/reports/{id}` | público | Detalhes + URLs de view/download |
| POST | `/api/reports` | admin | Criar relatório (multipart/form-data) |
| POST | `/api/reports/zip` | admin | Criar a partir de .zip (RTM/LAB extraídos) |
| PATCH | `/api/reports/{id}` | admin | Editar relatório |
| DELETE | `/api/reports/{id}` | admin | Excluir relatório + arquivos |
| GET | `/api/files/view/{file_id}` | **público** | Baixar/visualizar arquivo (`?download=1` força download) |

Documentação interativa em `/docs` (Swagger UI).

---

## 5. Estimativa de recursos no Coolify

| Serviço | RAM | CPU | Disco |
|---------|-----|-----|-------|
| PostgreSQL | 256M | 0.25 | ~50 MB + dados |
| MinIO | 256M | 0.25 | tamanho dos arquivos |
| Backend | 512M | 0.5 | ~200 MB (imagem) |
| Frontend | 128M | 0.25 | ~15 MB (imagem) |
