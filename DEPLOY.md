# Guia de Deploy no Coolify — Portal de Relatórios WMI (passo a passo)

Guia da **primeira publicação** na infra WMI (Coolify). Vamos subir os 4 componentes
(PostgreSQL + MinIO + backend + frontend) de uma vez, usando o build pack
**Docker Compose** — o arquivo [`docker-compose.coolify.yml`](docker-compose.coolify.yml)
já está pronto para isso.

> **Como vai ficar:** só o **frontend** fica público (via domínio); banco, MinIO e
> backend ficam na rede interna (não expostos). No começo, a ferramenta fica protegida
> pelo **login Microsoft** da WMI (a equipe usa e publica relatórios). Quando a Infra WMI
> liberar **acesso anônimo**, os clientes passam a **baixar sem login automaticamente** —
> sem mexer no código.

---

## Antes de começar (checklist)
- [ ] Acesso ao painel do Coolify: **https://admin.apps.wmi.solutions/**
- [ ] Acesso de admin ao repositório no GitHub (`fernandatavares2493/wmi-relatorios`)
- [ ] Os **secrets de produção** (gerados pelo Claude — ficam no chat, **não** no repositório)
- [ ] O código publicado no GitHub na branch certa (Etapa 0)

---

## Etapa 0 — Código no GitHub  *(o Claude faz por você)*
O Coolify constrói a partir do repositório no GitHub. Recomendado: **mesclar na `main`** e
publicar da `main`. Confirme com o Claude e ele faz o `merge` + `push`. Ao final, o repositório
terá os arquivos `docker-compose.coolify.yml` e este `DEPLOY.md`.

---

## Etapa 1 — Conectar o repositório (privado) ao Coolify
O repositório é privado, então o Coolify precisa de acesso. Vamos usar **Deploy Key** (mais simples).

1. No Coolify: **Keys & Tokens → Private Keys → + Add → Generate new ED25519 SSH Key**. Dê um nome (ex.: `wmi-relatorios`).
2. **Copie a chave pública** gerada.
3. No GitHub: repositório `wmi-relatorios` → **Settings → Deploy keys → Add deploy key** → cole a chave, deixe **"Allow write access" DESMARCADO** → **Add key**.
4. (Na Etapa 2, ao criar o recurso, você escolhe **"Private Repository (with Deploy Key)"**, seleciona essa chave e usa a URL SSH `git@github.com:fernandatavares2493/wmi-relatorios.git`.)

> Com Deploy Key não há auto-deploy a cada push (a chave é só leitura). Para atualizar depois de um novo commit, clique em **Deploy** no painel. Se um dia quiser auto-deploy (push-to-deploy), dá pra trocar por GitHub App.

---

## Etapa 2 — Criar o recurso (Docker Compose)
1. No Coolify, entre no seu **Project** → **Environment** `production` → **+ New**.
2. Escolha **Private Repository (with GitHub App)** (ou Deploy Key, conforme a Etapa 1).
3. Selecione o repositório e a **Branch** (`main`, se seguiu a Etapa 0).
4. Em **Build Pack**, troque para **Docker Compose**.
5. Preencha os dois campos do arquivo:
   - **Base Directory:** `/`
   - **Docker Compose Location:** `/docker-compose.coolify.yml`
6. Salve. O Coolify lê o arquivo e mostra os 4 serviços.

---

## Etapa 3 — Variáveis de ambiente (colar os valores)
Abra a aba **Environment Variables** do recurso e cole as chaves abaixo (use o
**Developer View** para colar de uma vez, formato `CHAVE=valor`).

> ⚠️ **Os valores reais dos secrets estão no chat** (gerados pelo Claude). Aqui ficam só
> os nomes — **nunca** comite secrets no repositório.

```
POSTGRES_DB=relatorios
POSTGRES_USER=wmi
POSTGRES_PASSWORD=<senha forte gerada>
MINIO_ROOT_USER=<usuario gerado>
MINIO_ROOT_PASSWORD=<senha forte gerada>
FRONTEND_URL=http://relatorios.apps.wmi.solutions
APP_ENV=production
ADMIN_EMAILS=fernandaferreira@wmi.solutions
MAX_UPLOAD_MB=50
INTERNAL_PROXY_SECRET=<segredo forte gerado>
```

- `ADMIN_EMAILS`: e-mails (separados por vírgula) que podem **publicar/editar/excluir**. Os demais só leem/baixam.
- Marque os valores sensíveis como **secret** quando o Coolify oferecer a opção.

---

## Etapa 4 — Domínio só no `frontend`
Após o parse, o Coolify lista os serviços. No serviço **frontend**:
1. No campo **Domains**, coloque: **`http://relatorios.apps.wmi.solutions`**
   - ⚠️ **Use `http://` (sem o "s")** — convenção WMI: a camada Apache da plataforma faz o
     HTTPS e o SSO por fora. Pôr `https://` aqui causa loop de redirect.
2. **Não** coloque domínio em `postgres`, `minio` nem `backend` — eles ficam internos.

---

## Etapa 5 — Deploy
1. Clique em **Deploy** (canto superior direito).
2. Acompanhe os logs de build (ele builda backend e frontend, e sobe postgres + minio).
3. Aguarde os serviços ficarem **healthy** (a ordem é automática: postgres/minio → backend → frontend).

---

## Etapa 6 — Validar
1. Abra **https://relatorios.apps.wmi.solutions** → vai pedir **login Microsoft** (SSO WMI).
2. Logada com um e-mail de `ADMIN_EMAILS`, você verá os botões de **+ Individual**, **Importar em Lote** e **⚙ Categorias**.
3. Teste: publique um relatório (solte um `.zip` → RTM/LAB extraídos; anexe PDF/imagem se quiser) e baixe.

---

## Etapa 7 — Liberar acesso público (pedido à Infra WMI)
Hoje a plataforma exige login Microsoft para **todos**. Para os **clientes baixarem sem login**:
- Peça à **Infraestrutura WMI** para configurar este app com **acesso anônimo**, mantendo a
  injeção do header de identidade para quem está logado (assim a equipe continua publicando).
- **Nada muda no código** — o backend já trata leitura/download como público e escrita como admin.

---

## Se algo der errado (problemas comuns)
| Sintoma | Causa provável | Solução |
|---|---|---|
| **502 Bad Gateway** no domínio | frontend não achou o backend | confirmar `API_UPSTREAM=http://backend:8000` e que tudo está no mesmo compose |
| Loop de redirect / 403 ao abrir | domínio com `https://` | trocar para `http://` no campo Domains |
| Deploy falha "variável vazia" | faltou preencher um secret | conferir a aba Environment Variables (Etapa 3) |
| `postgres`/`minio` expostos na internet | algum `ports:` sobrando | usar o `docker-compose.coolify.yml` (já sem `ports:`) |
| backend `unhealthy` | banco/MinIO ainda subindo | aguardar; ver logs do backend no Coolify |
| Build "no Dockerfile" | Base Directory errado | Base Directory = `/`, Compose Location = `/docker-compose.coolify.yml` |

> Dúvida ou erro no deploy: o Claude pode acompanhar ao vivo e diagnosticar (skill `wmi-coolifydebug`).
