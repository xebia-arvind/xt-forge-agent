# XT-Forge on Windows Server — Installation Guide

This walk-through installs the XT-Forge Django backend (`web` + `qcluster`
worker + PostgreSQL + Memurai/Redis + Node) on a Windows Server box.
Follow it top-to-bottom; every step is a copy-paste unit.

> **Deployment model**: two admin PowerShell windows running Python
> processes in the foreground. No NSSM/IIS/Windows-Service registration.
> A reboot requires manually re-launching both windows — this is the
> demo-grade trade-off. If you later want auto-start-on-boot, add NSSM
> per the appendix at the end of this file.

---

## 0. What you'll end up with

- **PostgreSQL 16** on `localhost:5432` — Django's application DB.
- **Memurai** on `localhost:6379` — Redis-compatible broker for django-q2.
- **Node.js 20** on PATH — powers Cucumber-JS, ts-morph AST sidecar,
  Playwright MCP, and the ui_knowledge crawler.
- **Python 3.11** with a venv holding all requirements.
- **Two PowerShell windows** you keep open:
  - Waitress serving Django on `0.0.0.0:8000`.
  - `python manage.py qcluster` running the background worker.
- **Firewall** open for inbound TCP 8000.
- **XT-Forge desktop app** on your Mac or Windows workstation pointed at
  `http://<server-ip>:8000`.

---

## 1. Prerequisites

| Item | Requirement |
| ---- | ----------- |
| OS | Windows Server 2016, 2019, or 2022 |
| Privileges | Administrator on the target machine |
| Disk | ~5 GB free (Python + Node + Postgres + Memurai + Playwright browsers) |
| RAM | 4 GB minimum, 8 GB recommended for the LLM stages |
| Network | Outbound HTTPS to `api.openai.com`, Jira, and (optionally) the client-under-test website |
| Inbound | TCP 8000 reachable from the desktop app's network |

**Enable long-path support** (Windows caps paths at 260 chars by
default; some npm dep trees exceed that). Run this in an admin
PowerShell **once**:

```powershell
New-ItemProperty -Path "HKLM:\SYSTEM\CurrentControlSet\Control\FileSystem" `
  -Name "LongPathsEnabled" -Value 1 -PropertyType DWORD -Force
```

Reboot after this if the value didn't previously exist.

**Loosen PowerShell script execution** (only for your session — safe):

```powershell
Set-ExecutionPolicy -Scope Process -ExecutionPolicy RemoteSigned
```

This lets the venv activation script run without prompting.

---

## 2. Install Python 3.11

1. Download the 64-bit Windows installer from
   <https://www.python.org/downloads/windows/> — pick **Python 3.11.x
   (Windows installer, 64-bit)**.
2. Run the installer. On the first screen:
   - ☑ **Add python.exe to PATH**
   - ☑ **Install for all users**
   - Click **Customize installation** if you want to place it in
     `C:\Python311\`; otherwise the default is fine.
3. Verify in a **fresh** PowerShell window (close the installer first):
   ```powershell
   python --version
   pip --version
   ```
   Expected: `Python 3.11.x` and `pip 24.x` (or newer).

---

## 3. Install PostgreSQL 16

1. Download the Windows installer from
   <https://www.postgresql.org/download/windows/> — the "EnterpriseDB
   installer" is the standard.
2. Run it. Defaults are fine except:
   - **Password**: set a strong one for the `postgres` superuser. Write
     it down.
   - **Port**: leave as `5432`.
3. **Stack Builder** at the end: skip it (uncheck "Launch Stack Builder").
4. Create the app database + user. Open **pgAdmin** (installed
   alongside) OR run this in a PowerShell:
   ```powershell
   & "C:\Program Files\PostgreSQL\16\bin\psql.exe" -U postgres -c "CREATE DATABASE xt_forge;"
   & "C:\Program Files\PostgreSQL\16\bin\psql.exe" -U postgres -c "CREATE USER xt_forge_user WITH PASSWORD 'ChooseAStrongPasswordHere';"
   & "C:\Program Files\PostgreSQL\16\bin\psql.exe" -U postgres -c "GRANT ALL PRIVILEGES ON DATABASE xt_forge TO xt_forge_user;"
   & "C:\Program Files\PostgreSQL\16\bin\psql.exe" -U postgres -d xt_forge -c "GRANT ALL ON SCHEMA public TO xt_forge_user;"
   ```
   The last command grants schema privileges (required on Postgres 15+).
   You'll be prompted for the `postgres` password each time.

---

## 4. Install Memurai (Redis for Windows)

Microsoft dropped their official Windows Redis in 2016. **Memurai** is a
maintained, byte-compatible drop-in — django-q2's `redis-py` client can't
tell the difference.

1. Download **Memurai Developer Edition** (free) from
   <https://www.memurai.com/get-memurai>.
2. Run the MSI. Defaults are fine — it registers as a Windows service
   that auto-starts on `127.0.0.1:6379`.
3. Verify:
   ```powershell
   Get-Service Memurai*
   & "C:\Program Files\Memurai\memurai-cli.exe" ping
   ```
   Expected: service `Running`, ping returns `PONG`.

---

## 5. Install Node.js 20 LTS

Required for Cucumber-JS (Execute stage), the crawler
(`ui_knowledge_capture`), the ts-morph AST sidecar, and Playwright MCP.

1. Download the LTS x64 MSI from <https://nodejs.org/>.
2. Run it. Default install path (`C:\Program Files\nodejs\`) is fine.
   Leave the "install tools for native modules" checkbox as-is (won't
   hurt; may help if any dep needs a native build).
3. Verify in a **fresh** PowerShell:
   ```powershell
   node --version
   npm --version
   ```
   Expected: `v20.x` and `10.x`.

---

## 6. Clone the repository

```powershell
git clone https://github.com/xebia-arvind/xt-forge-agent.git C:\xt-forge
cd C:\xt-forge
```

If git isn't installed: download from <https://git-scm.com/download/win>
(defaults are fine).

---

## 7. Python venv + Django dependencies

```powershell
cd C:\xt-forge
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip setuptools wheel
pip install -r ai-healer-django\requirements.txt
```

The install takes ~2-3 min. All packages are prebuilt Windows x64 wheels
(no compilation needed).

If `psycopg2-binary` fails: run `python -m pip install --upgrade pip
setuptools wheel` first, then retry.

---

## 8. Node dependencies + Playwright browsers

Two npm installs (repo root + the ts-normalizer sub-package) + the
Playwright Chromium download:

```powershell
cd C:\xt-forge
npm install
cd C:\xt-forge\ai-healer-django\flaky_healer\test_generation\ts_normalizer
npm install
cd C:\xt-forge
npx playwright install chromium
```

Playwright downloads ~150 MB and installs it under
`%USERPROFILE%\AppData\Local\ms-playwright\`.

If Chromium refuses to launch later during a Cucumber run, install its
OS-level dependencies:

```powershell
npx playwright install-deps
```

---

## 9. Configure the `.env`

Create `C:\xt-forge\ai-healer-django\flaky_healer\.env` with the
following. **Do not check this file into git** — it holds secrets.

```
DJANGO_SECRET_KEY=<paste the output of the command below>
DEBUG=false
ALLOWED_HOSTS=<server-hostname-or-IP>,localhost,127.0.0.1
DATABASE_URL=postgres://xt_forge_user:ChooseAStrongPasswordHere@localhost:5432/xt_forge
REDIS_URL=redis://127.0.0.1:6379/0

OPENAI_API_KEY=sk-proj-...paste-your-key...
FERNET_KEY=<paste the output of the command below>

OPENAI_TOOL_USE=on
OPENAI_MODEL_FEATURE=gpt-4o-mini
OPENAI_MODEL_MANUAL_TESTS=gpt-4o-mini
OPENAI_MODEL_PLAN=gpt-4o
OPENAI_MODEL_ARTIFACTS=gpt-4o
OPENAI_MODEL_FIXER=gpt-4o

XT_FORGE_REQUIRE_UI_KNOWLEDGE=true
SELECTOR_VERIFY_ENABLED=on
```

Generate `DJANGO_SECRET_KEY`:

```powershell
python -c "import secrets; print(secrets.token_urlsafe(50))"
```

Generate `FERNET_KEY`:

```powershell
python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
```

Replace `ChooseAStrongPasswordHere` with the Postgres password you set
in step 3.

---

## 10. Initial database setup

```powershell
cd C:\xt-forge\ai-healer-django\flaky_healer
..\..\.venv\Scripts\Activate.ps1
python manage.py migrate
python manage.py createsuperuser
python manage.py collectstatic --noinput
```

- `migrate` creates all Django tables in `xt_forge`.
- `createsuperuser` prompts for username, email, password — save these;
  they're your admin login.
- `collectstatic` writes CSS/JS into `flaky_healer\static\` so Whitenoise
  can serve them without a CDN.

---

## 11. Run the two processes

Open **two** admin PowerShell windows. Leave both running while you use
the system.

### Window 1 — Django web server (Waitress)

```powershell
cd C:\xt-forge\ai-healer-django\flaky_healer
..\..\.venv\Scripts\Activate.ps1
python -m waitress --host=0.0.0.0 --port=8000 flaky_healer.wsgi:application
```

You'll see:
```
Serving on http://0.0.0.0:8000
```

### Window 2 — Background worker (django-q2)

```powershell
cd C:\xt-forge\ai-healer-django\flaky_healer
..\..\.venv\Scripts\Activate.ps1
python manage.py qcluster
```

You'll see the Q Cluster banner and periodic heartbeat messages.

Both windows must stay open. Closing either kills that process.

---

## 12. Firewall & reachability

Open TCP 8000 inbound in Windows Defender Firewall. In an admin
PowerShell:

```powershell
New-NetFirewallRule -DisplayName "XT-Forge Web (8000)" `
  -Direction Inbound -Protocol TCP -LocalPort 8000 -Action Allow
```

Test from another machine on the LAN:

```
curl http://<server-ip>:8000/admin/login/
```

Expected: HTML of Django's admin login page.

Also test the health endpoint:

```
curl http://<server-ip>:8000/healthz/
```

Expected: `{"status": "ok"}`.

---

## 13. Point the desktop app at the Windows Server

Launch the XT-Forge desktop app on a workstation with network access to
the server.

1. On the **Setup** screen (first launch) or via the **Change backend
   URL** link on the Login screen (subsequent launches), paste
   `http://<server-ip>:8000`.
2. Log in with the superuser credentials from step 10.
3. Head to **Worklist** — Jira issues should populate (assuming the Jira
   connection is configured on the tenant via Django admin).

---

## 14. Backup notes

- **PostgreSQL** — daily backup:
  ```powershell
  & "C:\Program Files\PostgreSQL\16\bin\pg_dump.exe" -U xt_forge_user xt_forge > C:\backups\xt_forge_$(Get-Date -Format 'yyyy-MM-dd').sql
  ```
- **Memurai / Redis** — disposable. The queue rebuilds on the next task
  enqueue; nothing to back up.
- **`.env`** — copy off-box (encrypted USB, password manager). Holds the
  OpenAI key and the Fernet key that encrypts Jira tokens at rest. If
  lost, Jira tokens become unreadable and would need to be re-entered
  via Django admin.
- **Generated artifacts** (`tests/pages/generated/`, `features/<tenant>/`)
  — these are recreated by the pipeline; back up only if you've hand-
  edited any.

---

## 15. Troubleshooting

| Symptom | Fix |
| --- | --- |
| `pip install psycopg2-binary` fails with "Microsoft Visual C++ 14.0 required" | Upgrade pip first: `python -m pip install --upgrade pip setuptools wheel`. That switches pip to prefer prebuilt wheels. |
| `qcluster` says "Cannot connect to Redis" | `Get-Service Memurai*` — must be **Running**. If Stopped: `Start-Service Memurai`. Also confirm `REDIS_URL=redis://127.0.0.1:6379/0` in `.env`. |
| Cucumber execute fails: `Cannot find module 'ts-node/register'` | In repo root: `npm ci` to rebuild `node_modules/.bin/cucumber-js`. |
| Playwright launch: "browser executable not found" | `npx playwright install chromium` in the repo root. If it still fails: `npx playwright install-deps` for the OS libs. |
| `waitress` starts but `curl http://<ip>:8000/` from another machine times out | Firewall rule not applied → re-run the `New-NetFirewallRule` in step 12. Also confirm waitress bound `--host=0.0.0.0` (not `127.0.0.1`). |
| Django admin login page shows "Bad Request (400) DisallowedHost" | Add the server's hostname/IP to `ALLOWED_HOSTS` in `.env`, then restart the waitress window. |
| Windows-defender or antivirus deletes `chromium.exe` | Add an exclusion for `%USERPROFILE%\AppData\Local\ms-playwright\`. |
| Long-path error during `npm install` (like `ENAMETOOLONG`) | Confirm `LongPathsEnabled` was set (step 1) and REBOOT if you set it in this session. |

---

## Appendix — Auto-start on reboot (optional NSSM setup)

If you later want the two processes to auto-start after a Windows reboot,
install **NSSM** (Non-Sucking Service Manager) — a free single-exe
tool that wraps any command as a Windows service.

1. Download NSSM from <https://nssm.cc/download> → extract `nssm.exe`
   to `C:\Windows\System32\` (or add its folder to PATH).
2. Register the two services:
   ```powershell
   nssm install XTForgeWeb "C:\xt-forge\.venv\Scripts\python.exe" "-m" "waitress" "--host=0.0.0.0" "--port=8000" "flaky_healer.wsgi:application"
   nssm set XTForgeWeb AppDirectory "C:\xt-forge\ai-healer-django\flaky_healer"
   nssm set XTForgeWeb AppEnvironmentExtra ":PYTHONUNBUFFERED=1"
   nssm start XTForgeWeb

   nssm install XTForgeWorker "C:\xt-forge\.venv\Scripts\python.exe" "manage.py" "qcluster"
   nssm set XTForgeWorker AppDirectory "C:\xt-forge\ai-healer-django\flaky_healer"
   nssm start XTForgeWorker
   ```
3. Confirm both are running:
   ```powershell
   Get-Service XTForge*
   ```
4. Uninstall (if you change your mind):
   ```powershell
   nssm remove XTForgeWeb confirm
   nssm remove XTForgeWorker confirm
   ```

Logs for each service land under
`C:\xt-forge\ai-healer-django\flaky_healer\logs\runners\` (if the runner
config is unchanged) plus NSSM's own event log entries (viewable in
Event Viewer → Windows Logs → Application).

---

**You're done.** The Windows Server instance is now a peer of the Render
deployment — same code, same features, different infra. Point additional
desktop clients at `http://<server-ip>:8000` and they'll all share the
same Postgres + Memurai backing store.
