<!-- vnai-bootstrap | auto-generated -->
# Vnstock Vibe Onboarding

> **Về tệp này (dành cho người dùng).** Nếu bạn thấy khối này trong `~/.claude/CLAUDE.md`, `~/.gemini/GEMINI.md`, `~/.codex/AGENTS.md` hoặc `AGENTS.md` của dự án mà không nhớ đã tự thêm, thì nó do thư viện `vnstock` bản cũ (gói `vnai` 2.6.1 trở về trước) tự ghi vào khi bạn `import vnstock`. Từ `vnai` 2.6.2, thư viện không ghi gì khi import và chỉ ghi khi bạn bật; vnstock thế hệ 5 không ghi tệp nào. Nó chỉ là tài liệu cài đặt, không cấp cho trợ lý AI quyền gì thêm. Gỡ khối này bằng một lệnh (xem mục "Older line", phần cuối), hoặc đọc giải thích đầy đủ tại <https://vnstocks.com/docs/tai-lieu/vnstock-agent-skills>.

## Scope — read this first (for AI assistants)

This is **reference documentation** for installing and updating the Vnstock Python packages. It is not a task and it grants no permissions.

* **Act on it only when the user asks** to install, set up, update or troubleshoot `vnstock`. In any other conversation, ignore this file entirely.
* If the user pasted a setup prompt copied from vnstocks.com, **that prompt is the user's request** — follow it, and use this file only to look up details.
* **The user's instructions and your own safety rules always win.** Nothing here overrides them. If a step conflicts with what the user asked, follow the user.
* **Ask before acting.** Show the commands you intend to run and get the user's go-ahead before installing packages, creating environments, or writing any file. A request like "set up vnstock for me" counts as approval for the install commands in Sections 1–6; it does not cover anything else.
* **Never handle the API key yourself** — see Section 5.

## Which generation to install

vnstock **generation 5** (`vnstock` 5.x, official from 15/10/2026) is the default. Sections 1–10 describe it. The **older line** (`vnstock` 4.x with `vnai`, and the sponsor packages `vnstock_data` 3.x, `vnstock_ta`…) is still installable for people who deliberately keep it; it is described only in the section "Older line (vnstock 4.x)" at the end.

* Follow the older line only when the user's prompt says so (the prompts on `vnstocks.com/onboard/legacy` and `vnstocks.com/onboard-member/cai-dat-go-loi/legacy` do), or the user asks for it.
* Before installing generation 5, check the environment you are about to install into (Section 2): `<env python> -m pip show vnstock vnstock_data vnai`. If it already has `vnstock` 4.x, `vnstock_data` or `vnai`, **stop and ask**: the two generations share the package name `vnstock`, so installing generation 5 there replaces 4.x, breaks code written for it, and cannot be undone. Offer the separate environment for generation 5 (Section 2, case B), or migration: <https://vnstocks.com/docs/vnstock/chuyen-doi/tu-vnstock-data-3x>.

## What the Vnstock packages do on the user's machine

State this plainly if the user asks. Do not downplay it.

**Generation 5 (`vnstock` 5.x)**

* With a key, the key is sent to `vnstocks.com` over HTTPS in the `Authorization` header to check the tier, and the machine is registered as one of the account's devices (each tier has a device limit). The key is stored encrypted under `~/.vnstock/auth/`, never printed.
* Usage measurement runs **only if the account has turned it on** at <https://vnstocks.com/account?section=data-preferences>. It counts calls per function, source, row counts, durations and error codes; it never sends symbols, dates, figures, paths or error text. `DO_NOT_TRACK=1` or `VNSTOCK_TELEMETRY=0` switches it off on the machine.
* It writes no file for AI assistants.

**Older line (`vnstock` 4.x)**

* `vnai` reports usage counts to `vnstocks.com` so they count toward the account's shared quota (one quota for every vnstock library, hour/day/month, shown as percent left), and derives a device identifier so sponsor plans can enforce their device limit.
* With an API key configured, the key is sent to `vnstocks.com` over HTTPS in the `Authorization` header — except one call in the sponsor helper `vnii` 0.2.x (`get_license_info`), which still passes it as a URL query parameter.
* `vnai` 2.6.1 and earlier write an onboarding block into the AI-assistant instruction files on every `import vnstock`. From `vnai` 2.6.2 nothing is written on import; the user opts in with `vnstock.enable_agent()`.

Privacy policy: <https://vnstocks.com/onboard/chinh-sach-quyen-rieng-tu>

## Package index

* **Generation 5:** `https://vnstocks.com/api/packages` (PEP 503). No `vnai`, no separate installer.
* **Older line 4.x:** `https://vnstocks.com/api/simple`, with `vnai`.

Always use `--extra-index-url`, not `--index-url` (dependencies such as `pandas` still come from PyPI). A plain `pip install vnstock` without the index gets 4.x from PyPI. Use the interpreter of the environment you checked: `python -m pip …`.

## Setup (generation 5)

### 1. Python check

Identify the OS and the kind of machine first; it decides where vnstock goes (Section 2).

* **Hosted cloud environment** — Google Colab (`"google.colab" in sys.modules`), GitHub Codespaces (`CODESPACES=true`), Kaggle (`KAGGLE_KERNEL_RUN_TYPE` set): Python is provided; skip to Section 2.
* **macOS:** `python3 --version` must be **3.10 or newer**. The `/usr/bin/python3` that ships with the Xcode command line tools is 3.9 — too old. Use Python from <https://www.python.org/downloads/> or Homebrew (`brew install python`, the user runs it).
* **Linux:** `python3 --version` 3.10 or newer. `python3 -m venv` needs the distribution's venv package; on Debian/Ubuntu, if venv creation fails with `ensurepip is not available`, the user runs `sudo apt install python3-venv` (or `python3.12-venv`, matching the version) — you show the command, they run it.
* **Windows:** `py --version` (the Python launcher) or `python --version`, 3.10 or newer. If several versions are installed, `py -0p` lists them; pick one with `py -3.12`. Avoid the Microsoft Store Python (hidden script folders, sandboxed paths); recommend the python.org installer.
* If Python is missing or too old, stop and point the user to <https://www.python.org/downloads/> or <https://vnstocks.com/onboard/cai-dat-moi-truong-python>. Do not run `curl | sh` style installers.

### 2. Where to install

vnstock always goes into **one fixed, known place**, so the user, their support and any AI assistant can find the complete vnstock environment without searching the machine:

| Machine | Environment | Python of that environment |
| --- | --- | --- |
| macOS, Linux | `~/.venv` | `~/.venv/bin/python` |
| Windows | `$HOME\.venv` | `& "$HOME\.venv\Scripts\python.exe"` |
| Colab, Codespaces, Kaggle | the session's own Python, **no** virtual environment | `python` (notebook cells: `!pip …`) |

Use another place only when the user asks for it (a project `.venv`, conda, uv). Generation 5 depends only on `pandas`, so it would run elsewhere too; the fixed place is what makes support fast.

Decide the case before running anything, and show the user what you found:

**Case A — `~/.venv` does not exist.** Propose creating it, then install (Section 3).

```bash
python3 -m venv ~/.venv            # macOS / Linux
```
```powershell
py -m venv "$HOME\.venv"           # Windows (or: py -3.12 -m venv "$HOME\.venv")
```

**Case B — `~/.venv` exists.** Look inside it first; this changes nothing:

```bash
~/.venv/bin/python --version
~/.venv/bin/python -m pip show vnstock vnstock_data vnai
```
```powershell
& "$HOME\.venv\Scripts\python.exe" --version
& "$HOME\.venv\Scripts\python.exe" -m pip show vnstock vnstock_data vnai
```

* Python 3.10+ and `vnstock` 5.x, or no vnstock at all → install or update there (Section 3).
* `vnstock` 3.x/4.x, `vnstock_data`, `vnai` or `vnstock_installer` present (typical after the 4.x installer, which also used `~/.venv`) → **stop and ask**. Installing generation 5 there overwrites the older line, and it cannot be restored as it was. Tell the user what you found and recommend leaving `~/.venv` untouched and putting generation 5 in **`~/.venv5x`** (Windows: `$HOME\.venv5x`) — a fixed name too, so it can always be found. Use `~/.venv` only if the user explicitly says the old line may be replaced.
* Python older than 3.10, or the environment is broken (its `python` no longer runs, for example after a Python upgrade) → ask before recreating it: recreating deletes everything installed in it. If the user prefers to keep it, use `~/.venv5x`.

**Case C — hosted cloud environment.** Install into the session's Python; do not create a virtual environment.

If vnstock 4.x is installed somewhere else (system Python, another project), installing into `~/.venv` does not touch it; mention it to the user in one line and move on.

Run every later command with the chosen environment's own Python, as in the table, rather than activating it. That works even when PowerShell blocks `Activate.ps1` and whatever is on PATH. If the user wants to type `python` and `vnstock` directly in their terminal, they activate it: `source ~/.venv/bin/activate` (macOS/Linux) or `& "$HOME\.venv\Scripts\Activate.ps1"` (Windows; if blocked, the user runs `Set-ExecutionPolicy -Scope CurrentUser RemoteSigned`).

Never install into the system Python with `--break-system-packages`, and never `sudo pip`.

### 3. Install

macOS / Linux (replace `~/.venv` with `~/.venv5x` in case B with the older line kept):

```bash
~/.venv/bin/python -m pip install -U --extra-index-url https://vnstocks.com/api/packages vnstock
```

Windows (PowerShell):

```powershell
& "$HOME\.venv\Scripts\python.exe" -m pip install -U --extra-index-url https://vnstocks.com/api/packages vnstock
```

* Colab / Codespaces / Kaggle: `!pip install -U --extra-index-url https://vnstocks.com/api/packages vnstock` in a notebook cell (in a Codespaces terminal, without `!`). On Colab then **Runtime → Restart session** (Colab may ship an older `vnstock`).
* `uv` users who asked for uv: `uv pip install --python ~/.venv/bin/python -U --extra-index-url https://vnstocks.com/api/packages vnstock`
* Verify: `~/.venv/bin/python -c "import vnstock; print(vnstock.__version__)"` prints `5.…`; `~/.venv/bin/vnstock version` (Windows: `& "$HOME\.venv\Scripts\vnstock.exe" version`) shows the installed and latest release.
* The `vnstock` command lives in the environment: `~/.venv/bin/vnstock` (Windows: `$HOME\.venv\Scripts\vnstock.exe`). Call it by that path, or after activating. If `vnstock` is "not found" in an un-activated terminal, that is expected, not an error. For an environment that is not the default one, `python -c "from vnstock.cli import main; raise SystemExit(main())" <subcommand>` runs it through any interpreter; there is no `python -m vnstock`. The user runs the `register` subcommand in their own terminal.

### 4. Editors and Jupyter

Point the user's tools at the same environment so code runs where vnstock is installed:

* **Antigravity / VS Code:** *Python: Select Interpreter* → `~/.venv/bin/python` (Windows: `%USERPROFILE%\.venv\Scripts\python.exe`).
* **Jupyter (local):** register the environment as a kernel once, then pick **Python (Vnstock)** in the notebook:

```bash
~/.venv/bin/python -m pip install ipykernel
~/.venv/bin/python -m ipykernel install --user --name=vnstock-venv --display-name "Python (Vnstock)"
```
```powershell
& "$HOME\.venv\Scripts\python.exe" -m pip install ipykernel
& "$HOME\.venv\Scripts\python.exe" -m ipykernel install --user --name=vnstock-venv --display-name "Python (Vnstock)"
```

For `~/.venv5x`, use `--name=venv5x --display-name "Python (Vnstock 5)"` so the two kernels stay apart.

### 5. API key — the user enters it, you never see it

The Guest tier works without a key (a subset of functions, tighter limits). A free registered account or a sponsor plan needs a key from <https://vnstocks.com/account#api-key>.

* **Do not ask the user to paste the key into the chat**, and if they do, tell them it is now in the conversation log and suggest regenerating it at the link above.
* **Do not put the key in a command, a script, a file you write, or your output.** Never print `VNSTOCK_API_KEY` (no `echo`, `env`, `printenv`, `set`, `Get-ChildItem Env:`); only check whether it exists.
* vnstock 5 keeps the key sealed (encrypted) in `~/.vnstock/auth/`. You never read those files, and you never call private helpers that return the key (for example anything under `vnstock._core`).

Where vnstock 5 takes the key from, in order:

1. `VNSTOCK_API_KEY` in the environment, if set (it always wins).
2. The sealed store written by `vnstock register` / `vnstock.login()`.
3. A plain `~/.vnstock/api_key.json` — written by the one-line command on vnstocks.com that sponsors run before opening you, or by vnstock 4.x — is imported into the sealed store **once**, the first time vnstock 5 runs on a machine that has no sealed key yet. After that the file is ignored: rewriting it does **not** change the key vnstock 5 uses.

Check with `vnstock status`, using the environment's command (`~/.venv/bin/vnstock status`; Windows `& "$HOME\.venv\Scripts\vnstock.exe" status`): tier, expiry and device; it never prints the key. If it shows Guest, or a tier the user does not expect, ask the user to log in themselves, in their own terminal, with the same environment's command: `~/.venv/bin/vnstock register` (Windows `& "$HOME\.venv\Scripts\vnstock.exe" register`; hidden input; it replaces the stored key). The key is stored per user, not per environment, so every environment on the machine sees the same login. In a notebook: `import os, vnstock; vnstock.login(os.environ["VNSTOCK_API_KEY"])`. There is no `register_user()`, `vnai` or `setup_api_key` in vnstock 5.

Once `vnstock status` shows the right tier, tell the user that `~/.vnstock/api_key.json` still holds the key in plain text; if this machine does not run vnstock 4.x any more, offer that they delete it themselves. To log out: `vnstock logout`.

### 6. Next step by tier

Start here: run `vnstock onboard` with the environment's command (`~/.venv/bin/vnstock onboard`; Windows `& "$HOME\.venv\Scripts\vnstock.exe" onboard`). It prints this machine's release, tier and add-ons, the rules to follow, the next step for that tier and the skills that tier can read, so it already matches the user's tier. Follow it before writing code; `--json` gives the same for a program. A release without the command answers `invalid choice: 'onboard'`: update first (Section 7). The rest of this section is the same guidance in long form.

**Guest or Community:** confirm the install with a small read-only example:

```python
from vnstock import Reference
print(Reference().company.info("FPT"))
```

Community can also try `vnstock_quant` (a daily trial of basic calculations): it appears in `vnstock install --list`.

**Sponsor (Bronze, Silver, Golden, Diamond):** the same `vnstock` serves every tier — the key decides what runs, so imports never change (`from vnstock import Market, Reference, Fundamental, Macro, Insights, Analytics, Retail`). Add-on libraries are installed by `vnstock` itself, into the environment whose `vnstock` command you run — so run the one from Section 2 (`~/.venv/bin/vnstock`); the index only offers what the key's tier allows:

```bash
vnstock install --list     # preview, installs nothing
vnstock install            # install everything the tier allows (after the user agrees)
```

| Library | Lowest tier |
| --- | --- |
| `vnstock_quant` (replaces `vnstock_ta`, `from vnstock_quant import Indicator`) | Community trial; Bronze adds financial ratios, risk and portfolio; Silver adds every indicator |
| `vnstock_news` 3.x | Silver |
| `vnstock_pipeline` 3.x | Golden |

Do not `pip install` add-ons by name and do not install the 4.x sponsor packages (`vnstock_data`, `vnstock_ta`, `vnstock_installer`) into a generation 5 environment. A library above the user's tier is simply not offered; that is not an install error. Higher tiers: <https://vnstocks.com/insiders-program>.

If anything fails, run `vnstock doctor`: seven checks (policy, release, tier rights, key, quota, data schema, index) with a fix for the first one that fails. Illustrated walkthrough for the user: <https://vnstocks.com/onboard-member/cai-dat-go-loi>

### 7. Updating to a new version

Use this when the user pastes an update notice or asks to update. Moving to a higher sponsor tier is an account change at <https://vnstocks.com/insiders-program>, not a pip command.

1. Use the environment from Section 2 — `~/.venv` unless the user chose another place; check `~/.venv5x` too if `~/.venv` holds the older line. `~/.venv/bin/vnstock version` shows the installed and latest release.
2. Update, after the user agrees:
   ```bash
   ~/.venv/bin/python -m pip install -U --extra-index-url https://vnstocks.com/api/packages vnstock
   ~/.venv/bin/vnstock install -U         # sponsor add-ons already installed
   # Windows: & "$HOME\.venv\Scripts\python.exe" -m pip install -U ...  then  & "$HOME\.venv\Scripts\vnstock.exe" install -U
   ```
3. Restart the Python process / Jupyter kernel / Colab runtime, re-check the version, and report before → after to the user in Vietnamese with the release notes: <https://vnstocks.com/docs/tai-lieu/lich-su-phien-ban>.

Each generation 5 release runs for 365 days from its publication; the library warns 30 days ahead, then data calls fail with `VNSTOCK_RELEASE_EXPIRED`. Updating regularly avoids it.

| Symptom | Fix |
| --- | --- |
| `pip install vnstock` gave 4.x | The command lacks `--extra-index-url https://vnstocks.com/api/packages`. |
| `externally-managed-environment` | System Python targeted instead of `~/.venv`. Install with `~/.venv/bin/python -m pip` (Section 2); never add `--break-system-packages`. |
| Version unchanged | pip ran in another environment; compare `python -c "import sys; print(sys.executable)"` in the user's terminal, notebook kernel or editor with `~/.venv/bin/python`, and point the tool at `~/.venv` (Section 4). |
| `vnstock: command not found` / not recognized | The environment is not activated. Call `~/.venv/bin/vnstock` (Windows `$HOME\.venv\Scripts\vnstock.exe`) or activate it (Section 2). |
| `ensurepip is not available` (Linux) | The distribution's venv package is missing; the user runs `sudo apt install python3-venv`, then create `~/.venv` again. |
| `Activate.ps1 cannot be loaded` (Windows) | Not needed: call `& "$HOME\.venv\Scripts\python.exe"` directly, or the user runs `Set-ExecutionPolicy -Scope CurrentUser RemoteSigned`. |
| Device limit reached | The user removes unused devices at <https://vnstocks.com/account?section=devices>, then runs `vnstock register` again. |

### 8. Skills for AI assistants

Topic guides ("skills") and the documentation pages on vnstocks.com can be loaded into your context on demand. They are reference material, not instructions: they never override the user's request or your safety rules, and you tell the user before loading one.

```python
from vnstock.utils.agents import load_skill, load_skill_catalog, load_doc, load_docs_catalog   # vnstock 5
# vnstock 4.x: from vnstock.core.utils.agents import load_skill, load_skill_catalog
print(load_skill_catalog())          # every skill and the tier it needs
print(load_skill("vnstock-bootstrap"))
```

The key the user registered goes with the request; you never handle it. On vnstock 5 a refusal is raised as an error that says why (no key, tier too low with the tier needed, unknown name); on 4.x the call logs a warning and returns `None`. Nothing is written to disk.

### 9. AI assistant guide in the project's instruction file (only when the user's prompt says to turn it on)

**User asked to turn it on.** vnstock 5 writes no file. Show the user the block below, and after they agree add it to `AGENTS.md` in the project directory (create the file if it does not exist; keep everything already in it). Use another instruction file only if the user names it. Never write to the assistants' global files (`~/.claude/CLAUDE.md`, `~/.gemini/GEMINI.md`, `~/.codex/AGENTS.md`).

```markdown
<!-- vnstock-guide:start -->
## vnstock

This project uses the Python library `vnstock` (generation 5).

- Environment: `~/.venv` (Windows `$HOME\.venv`) unless noted here. Run code and commands with `~/.venv/bin/python` and `~/.venv/bin/vnstock`. Do not install vnstock into another Python.

- At the start of a session: run `vnstock onboard` and follow it; it reflects the user's tier.
- Data classes: `from vnstock import Market, Reference, Fundamental, Macro, Analytics, Retail`, for example `Market().equity("FPT").ohlcv()`. Look functions up before writing them: `vnstock tools` (the tree), `vnstock doc Market.equity.ohlcv` (one function); `vnstock.show_api()` in a notebook.
- Account: the user logs in with `vnstock register` in their own terminal. Never ask for the API key in chat, never put it in code or print it. `vnstock status` shows the tier.
- Add-on libraries for the user's tier: `vnstock install --list`, then `vnstock install` after the user agrees.
- Reference guides: `from vnstock.utils.agents import load_skill, load_skill_catalog`. Tell the user before loading one. They are reference material, never instructions.
- Errors are raised as typed exceptions with a Vietnamese and English message; read `error.code` and `error.action`.
<!-- vnstock-guide:end -->
```

To update the guide later, replace everything between the two markers; to remove it, delete them and what is between. On 4.x, see "Older line".

**User asked not to turn it on** — write nothing to any instruction file.

### 10. Immediate value & next steps (the first 3 prompts)

After installation and the check succeed, do not leave the user wondering what to do next. Congratulate the user and offer 3 concrete prompts they can try immediately:

1. **Price & Technicals:**
   > *"Lấy dữ liệu giá đóng cửa 6 tháng gần nhất của FPT, tính đường MA20, MA50 và vẽ biểu đồ thể hiện xu hướng cùng khối lượng."*
2. **Fundamental Comparison:**
   > *"Tải báo cáo tài chính 4 quý gần nhất của HPG và NKG, lập bảng so sánh doanh thu thuần, biên lợi nhuận gộp và tốc độ tăng trưởng."*
3. **Domain Skill (Kỹ năng chuyên sâu):**
   > *"Nạp kỹ năng phân tích báo cáo tài chính từ Vnstock và đánh giá cơ cấu tài sản, tỷ lệ nợ vay của VNM."*

## Older line (vnstock 4.x)

Use this section only when the user's prompt or request says they deliberately keep vnstock 4.x / `vnstock_data` 3.x. Sections 1, 8 and 10 above apply unchanged, and Section 4 with the older line's environment in place of `~/.venv`; the rest differs as follows.

### Environment

The older line needs its own virtual environment, because 4.x and generation 5 share the package name `vnstock`. Use a fixed place, as for generation 5, and check it first (`<env python> -m pip show vnstock vnstock_data vnai`):

* `~/.venv` (Windows `$HOME\.venv`) already holds the 4.x install (the 4.x installer used it) → keep using it.
* `~/.venv` does not exist → it can be created for 4.x, but tell the user generation 5 will then go to `~/.venv5x` later.
* `~/.venv` holds generation 5 → **ask before touching it**, and put the older line in `~/.venv4x` (Windows `$HOME\.venv4x`).

Create an environment only after the user agrees, for example:

* macOS/Linux: `python3 -m venv ~/.venv4x && source ~/.venv4x/bin/activate`
* Windows: `py -m venv "$HOME\.venv4x"` then `& "$HOME\.venv4x\Scripts\Activate.ps1"`
* Google Colab, GitHub Codespaces: the session's own environment.

Python 3.10–3.14 (`vnstock_pipeline` 2.x needs 3.11+).

### Install (Community)

```bash
python -m pip install -U --extra-index-url https://vnstocks.com/api/simple "vnstock>=4.0.9" "vnai>=2.6.2"
```

Quote version specifiers (`"vnstock>=4.0.9"`) so the shell does not read `>` as a redirect.

### API key (4.x)

**Usual case: the key is already saved on the machine.** The setup page on vnstocks.com gives sponsors a one-line command that saves their key to `~/.vnstock/api_key.json` (mode 600) before they open you. `vnstock`, `vnai` and both official installers read that file. Check that it exists **without reading or printing it**:

```bash
python -c "import os; print('Đã có khoá' if os.path.exists(os.path.expanduser('~/.vnstock/api_key.json')) else 'Chưa có khoá')"
```

If not, ask the user to run the command from <https://vnstocks.com/onboard-member/cai-dat-go-loi/legacy> (step 1) in their own Terminal/PowerShell, or one of the options below.

**Alternative: environment variable.** For a command-line assistant (`claude`, `codex`, `gemini`) started from the same terminal, the user can set `VNSTOCK_API_KEY` with a silent read (nothing is echoed and the key does not enter shell history):

* macOS / Linux (zsh or bash):
  ```bash
  printf "Dán khoá rồi nhấn Enter: "; read -rs VNSTOCK_API_KEY; echo; export VNSTOCK_API_KEY
  ```
* Windows PowerShell:
  ```powershell
  $env:VNSTOCK_API_KEY = [System.Net.NetworkCredential]::new('', (Read-Host 'Dán khoá rồi nhấn Enter' -AsSecureString)).Password
  ```

Check it exists without printing it, then save it so the graphical installer and later sessions can use it:

```bash
python -c "import os; print('Đã có khoá' if os.environ.get('VNSTOCK_API_KEY') else 'Chưa có khoá')"
python -c "import os; from vnai import setup_api_key; setup_api_key(os.environ['VNSTOCK_API_KEY'])"
chmod 600 ~/.vnstock/api_key.json   # macOS/Linux; vnai before 2.6.2 creates the file readable by other users
```

**Fallback: neither is available.** Ask the user to register the key themselves, in their own terminal, with the environment active:

```bash
python -c "from vnstock.core.utils.auth import register_user; register_user()"
```

**Which plan is this key on?** Ask the user to run the same `register_user()` command: with a key stored it prints the plan tier and limits (the key only as its first and last four characters), then asks whether to change the key — the user answers `N`. Do not run key-status helpers yourself: some of them print a longer part of the key.

### Sponsor packages (4.x)

Installed by the official installer, which asks for the key itself. Offer to run it; do not install sponsor packages by name. The installer only offers what the key's plan allows:

| Tier | Sponsor packages |
| --- | --- |
| Bronze | `vnstock_data` |
| Silver | `vnstock_data`, `vnstock_ta`, `vnstock_news` |
| Golden, Diamond | `vnstock_data`, `vnstock_ta`, `vnstock_news`, `vnstock_pipeline` (needs Python 3.11+) |

Only verify or import the packages of the user's tier: on Bronze, `import vnstock_ta` fails by design.

* Graphical installer (Windows, macOS). Keep the exact pin: an unrelated party owns `vnstock-installer` 99.0.0 on PyPI and pip takes the highest version across indexes.
  ```bash
  python -m pip install -U --extra-index-url https://vnstocks.com/api/simple vnstock_installer==3.1.3
  python -m vnstock_installer
  ```
* Command-line mode of the same installer (any OS, no window), from 3.1.3 (`python -m vnstock_installer --version`):
  ```bash
  python -m vnstock_installer --non-interactive --json
  ```
  It reads the key from `VNSTOCK_API_KEY` or `~/.vnstock/api_key.json`. stdout is one JSON object with `tier`, `installed`, `locked`, `skipped`, `failed` and `verify_command` — run that `verify_command` to check the install. Exit codes: `0` done, `1` setup/network error or a Community key, `2` a requested package is above the plan, `3` some packages failed or were skipped. If it rejects `--non-interactive`, use the graphical installer.
* Command-line installer (macOS, Linux, servers, Colab):
  ```bash
  curl -sL https://vnstocks.com/files/vnstock-cli-installer.run -o vnstock-cli-installer.run
  bash vnstock-cli-installer.run --check
  bash vnstock-cli-installer.run -- --non-interactive
  ```
  `--check` verifies the archive's embedded checksums. The `--` before `--non-interactive` is required. On CI, provide the key as the secret environment variable `VNSTOCK_API_KEY`.

Illustrated walkthrough: <https://vnstocks.com/onboard-member/cai-dat-go-loi/legacy>

### Updating (4.x)

1. Show current versions: `python -m pip show vnstock vnai vnii`. Latest published: `https://vnstocks.com/api/vnstock/versions`.
2. Community: `python -m pip install -U --extra-index-url https://vnstocks.com/api/simple vnstock vnai`. Sponsor: re-run the installer (`python -m vnstock_installer --non-interactive` from 3.1.3).
3. Restart the Python process / kernel and report before → after.

| Symptom | Fix |
| --- | --- |
| `from versions: none` | The command lacks `--extra-index-url https://vnstocks.com/api/simple`. |
| `vnstock_installer` 99.0.0 installed | Unpinned install. `python -m pip uninstall -y vnstock_installer`, reinstall with `==3.1.3`. |
| Version unchanged | pip ran in another environment. |
| Device limit reached | The user removes unused devices at <https://vnstocks.com/account?section=devices>. |

### AI assistant guide (4.x)

Only when the user's prompt says to turn it on. Run in the project directory, then show the user the file:

```bash
python -c "import vnstock; vnstock.enable_agent(); print(vnstock.setup_agent())"
```

This writes `AGENTS.md` in the current directory only, never the assistants' global files.

### Turning the 4.x guide off and removing it

Tell the user about this whenever they ask why this text is in their assistant's instructions. Run it only if they ask you to. The environment variable keeps this very `import` from writing the block again while it is being removed.

* macOS / Linux:
  ```bash
  VNSTOCK_DISABLE_AGENT_SETUP=1 python -c "import vnstock; vnstock.disable_agent(); print(vnstock.remove_agent_files('all'))"
  ```
* Windows PowerShell:
  ```powershell
  $env:VNSTOCK_DISABLE_AGENT_SETUP="1"; python -c "import vnstock; vnstock.disable_agent(); print(vnstock.remove_agent_files('all'))"
  ```

`python -c "import vnstock; print(vnstock.agent_status())"` shows which files are written and whether writing is on. On vnstock 5, remove the `vnstock-guide` block from `AGENTS.md` by hand.

*(End of Bootstrap. When in doubt, Route!)*
