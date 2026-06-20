# KAI interfaces — setup (Web UI · Webex · Slack · Teams)

One shared "brain" (the KAI API + your knowledge base), **four ways to reach it** —
all hit the same `/ask` endpoint:

- **Web UI** — a standalone browser app (`frontend/`), no tokens (see §Web UI).
- **Webex · Slack · Teams** — a thin bot process each, selected by **`CHAT_PLATFORM`**
  and started with `python run/setup.py bot`.

> **One platform per process — run more than one at the same time by starting more
> processes** (all pointing at the same KAI API). See [Running several at once](#running-several-platforms-at-once).
> The selection is validated: an unknown `CHAT_PLATFORM` fails fast.

## Quick reference

| Platform | Console | Tokens → `.env` | Public URL? | Start |
| --- | --- | --- | --- | --- |
| **Web UI** | — | none (browser) | **No** | `python run/setup.py ui` (serves `frontend/` on :3000) |
| **Webex** | developer.webex.com | `WEBEX_BOT_TOKEN` | **No** (outbound websocket) | `CHAT_PLATFORM=webex python run/setup.py bot` |
| **Slack** | api.slack.com/apps | `SLACK_BOT_TOKEN` (xoxb-) + `SLACK_APP_TOKEN` (xapp-) | **No** (Socket Mode) | `pip install -e '.[slack]'` then `CHAT_PLATFORM=slack python run/setup.py bot` |
| **Teams** | portal.azure.com | `TEAMS_APP_ID` + `TEAMS_APP_PASSWORD` | **Yes** (inbound HTTPS) | `pip install '.[teams]'` then `CHAT_PLATFORM=teams python run/setup.py bot` |

Common prereqs for all: the **KAI API running** (`python run/setup.py start`) and
reachable at `KAI_API_URL` (default `http://127.0.0.1:8100`); a `.env` (`cp .env.example .env`).

> **Access control (read before exposing a bot).** Today only **Webex** has a
> per-user allowlist (`WEBEX_APPROVED_USERS` / `WEBEX_APPROVED_DOMAINS`). **Slack
> and Teams answer anyone who can reach the bot** in the workspace/tenant — gate
> them at the platform level (restrict the Slack app to a private channel; scope
> the Teams app to specific users/teams in the Admin Center). The `KAI_API_KEY`
> guards the HTTP API, not who may DM the bot. A shared per-user allowlist for
> Slack/Teams is on the roadmap.

---

## Web UI (shipped, zero tokens)

The simplest surface — a standalone browser chat app in `frontend/` that calls the
API over HTTP. No bot, no tokens.

1. **Start the API:** `python run/setup.py start`.
2. **Allow the UI's origin (CORS):** in `.env`, set `CORS_ORIGINS` to the exact origin
   you serve from. The shipped `.env.example` already allows both common dev ports:
   `CORS_ORIGINS=http://localhost:3000,http://localhost:5173`.
3. **Serve the UI:** `python run/setup.py ui` (serves `frontend/` on **:3000**; override
   with `KAI_UI_PORT`). Equivalent: `python -m http.server 3000 -d frontend`.
4. **Open** <http://localhost:3000>. Use the header fields to set the **API base URL**
   (if not the default `:8100`) and the **API key** (only if `KAI_API_KEY` is set).

**Gotchas:** CORS is **deny-by-default** — the UI's origin must be in `CORS_ORIGINS`
or the browser blocks the call; under Docker the UI is the `frontend` service on `:3000`.

---

## Webex (shipped, no public URL)

Webex bots use an **outbound websocket**, so there's nothing to host and **no public URL** — the bot dials out to Webex and waits for messages. You'll create the bot, copy its one token, drop it in `.env`, add the bot to a space, and run it.

> **There are no scopes or permissions to choose.** Unlike Slack/Teams, a Webex bot gets a **fixed capability set** automatically (read @mentions in group spaces, see every message in 1:1 spaces, post/edit/delete messages, download files, show feedback cards). If you go looking for a "scopes" or "permissions" screen, you won't find one — that's expected, not a missing step.

1. **Sign in** at <https://developer.webex.com> using the Webex account for the org you'll test in (the same login you use for the Webex app).
2. Click your **avatar** (top-right) → **My Webex Apps**.
3. Click **Create a New App**, then choose **Create a Bot**.
4. **Fill the form:**
   - **Bot Name** — use a **single word** (e.g. `KAI`). Only the **first word** is shown when someone @mentions the bot, so a multi-word name gets truncated on screen.
   - **Bot Username** — becomes a permanent address like `yourname@webex.bot`. It is **permanent and globally unique** (you can't change it later and nobody else can reuse it), so pick carefully.
   - **Icon** — upload one, or pick a default.
   - **Description** — a short line describing the bot.
5. Click **Add Bot** at the bottom.
6. **Copy the Bot Access Token now — it is shown ONCE.** The next screen displays a long token string. Copy it immediately and keep it safe. If you navigate away without copying it, you must click **Regenerate Access Token**, which **invalidates the old token** (anything using it stops working).
7. **Put the token in `.env`** (run `cp .env.example .env` first if you don't have one):
   ```dotenv
   CHAT_PLATFORM=webex
   WEBEX_BOT_TOKEN=<the token>     # raw token, no "Bearer " prefix, no quotes
   ```
8. **Add the bot to a space** in the Webex app (desktop or web):
   - **1:1 chat:** start a new direct message and search for the bot by its `yourname@webex.bot` address. In a 1:1 the bot **sees every message** — no @mention needed.
   - **Group space:** open the space → **Add people** (or **People** → add) → type the bot's `yourname@webex.bot` address. In a **group space you MUST @mention the bot** (`@KAI your question`) — without the mention the bot stays **silent** by design.
9. **Run the bot:** `CHAT_PLATFORM=webex python run/setup.py bot` (the KAI API must already be running via `python run/setup.py start`).

> **Token = full access.** `WEBEX_BOT_TOKEN` is a long-lived token that grants every bot capability. Never commit it or share it; if it leaks, **Regenerate** it on developer.webex.com (which invalidates the leaked one) and update `.env`.

**Optional access control** (Webex only — both default to "anyone in the space"): set `WEBEX_APPROVED_DOMAINS` to a comma-separated list of email domains (e.g. `example.com,acme.com`) to answer only those users, and/or `WEBEX_APPROVED_USERS` to specific addresses (e.g. `alice@example.com,bob@example.com`). Leave blank to answer everyone.

**Gotchas:** the bot token is shown **once** (lose it → **Regenerate**, which kills the old one); **no scopes or permissions** to pick (fixed capability set — don't go hunting for that screen); **group spaces require an @mention** (silent otherwise), 1:1 spaces don't; **no public URL** needed (outbound websocket); the **first word** of the bot name is all that shows on @mention.

---

## Slack (shipped, no public URL — Socket Mode)

First: `pip install -e '.[slack]'` (installs `slack_bolt`).

Slack uses **two different tokens, created in two different places** — keep them straight from the start:

- **App-Level Token** (`xapp-`) → `SLACK_APP_TOKEN` — authorizes the **Socket Mode** websocket (how KAI receives events without a public URL). One scope: `connections:write`. Created on **Basic Information → App-Level Tokens** (or in the dialog when you enable **Socket Mode**).
- **Bot User OAuth Token** (`xoxb-`) → `SLACK_BOT_TOKEN` — authorizes the **bot itself** to read mentions/DMs and post replies. Found on **OAuth & Permissions**, and **blank until you install the app**.

> ⚠️ **The App-Level Token is NOT in the *OAuth Tokens* box** on the OAuth & Permissions page. That box (*"OAuth Tokens will be automatically generated when you finish installing your app…"*) is the **`xoxb-` bot token** — it fills in only after **Install to Workspace → Allow** (step 5). The **`xapp-`** token lives separately under **Basic Information → App-Level Tokens** (step 3).
>
> The two are **not interchangeable**; KAI exits immediately if either is missing — or if you've **swapped them** (it checks the `xoxb-`/`xapp-` prefixes and tells you which goes where).

1. **Create the app:** go to <https://api.slack.com/apps> and sign in → click **Create New App** → in the dialog choose **From scratch** → type a value in **App Name**, pick your workspace from **Pick a workspace to develop your app in:**, then click **Create App**. You land on **Basic Information**.

2. **Add the bot scopes (this is the step people miss):** in the left sidebar under *Features* click **OAuth & Permissions**, scroll to **Scopes**, and find the **Bot Token Scopes** sub-section (NOT *User Token Scopes* — that's a different box on the same page). For **each** scope below: click **Add an OAuth Scope**, then **type the scope name** into the searchable dropdown and **select it**. You add them **one at a time** — repeat the button per scope. These **save automatically**.

   | Scope | Why KAI needs it |
   | --- | --- |
   | `app_mentions:read` | Subscribe to `app_mention` so the bot **hears @mentions** in channels. |
   | `chat:write` | **Post replies** (and feedback acknowledgements) into channels and threads. |
   | `im:history` | Receive the `message.im` event so the bot **hears DMs** sent directly to it. |

   > Earlier docs also listed `commands` and `im:read` — **skip both.** KAI has no slash-command handler, and the DM event (`message.im`) requires **`im:history`** (in the table above), **not** `im:read`.

3. **Generate the App-Level Token (`xapp-`) — don't miss the name field:** in the left sidebar under *Settings* click **Socket Mode** and flip **Enable Socket Mode** to On. If you don't already have an app-level token, Slack prompts you to create one: **enter a value in the *Token Name* field** (any name, e.g. `socket`), make sure the **`connections:write`** scope is present (click **Add Scope** → `connections:write` if it isn't), **then** click **Generate**. Copy the token — it starts with **`xapp-`** → `SLACK_APP_TOKEN` — and click **Done**.

   > If no dialog appears, generate it manually at **Basic Information → App-Level Tokens → Generate Token and Scopes** (enter a *Token Name*, **Add Scope** `connections:write`, **Generate**). Already have one with `connections:write`? Reuse it.

4. **Subscribe to events:** in the left sidebar under *Features* click **Event Subscriptions** and flip **Enable Events** to On. (With Socket Mode on, the **Request URL** is **not required** — skip it; there's no public URL to find.) Expand **Subscribe to bot events**, then click **Add Bot User Event** and pick each of `app_mention` and `message.im` from the searchable list. **Now click Save Changes** at the bottom — unlike scopes, **events are NOT auto-saved** and are lost without this click.

5. **Install + get the Bot Token (`xoxb-`):** back on **OAuth & Permissions** (or the **Install App** sidebar item) click **Install to Workspace**, then on the consent screen click **Allow**. You return to **OAuth & Permissions** — the **OAuth Tokens** box now shows the **Bot User OAuth Token**, which starts with **`xoxb-`** → `SLACK_BOT_TOKEN`.

   > If you ever **add or change scopes after installing**, Slack requires you to **reinstall** the app (repeat this step) before the new scopes take effect.

6. **`.env`:**
   ```dotenv
   CHAT_PLATFORM=slack
   SLACK_BOT_TOKEN=xoxb-...                 # Bot User OAuth Token (from step 5)
   SLACK_APP_TOKEN=xapp-...                 # App-Level Token (from step 3)
   # SLACK_FEEDBACK_BUTTONS=true            # optional: 👍/👎/escalate buttons under answers (default true)
   ```

7. **Invite + run:** in a channel type `/invite @<your app name>` (the bot's display name), then `CHAT_PLATFORM=slack python run/setup.py bot`. @mention the bot in the channel, or DM it directly.

**Gotchas:** two tokens in two places — **App-Level** (`xapp-`, *Basic Information* / *Socket Mode*) vs **Bot User OAuth** (`xoxb-`, *OAuth & Permissions*, after install); they're **not interchangeable** and KAI exits with a clear message if either is missing or swapped; the `xapp-` token **needs a *Token Name*** and `connections:write` before you click **Generate**; add scopes under **Bot Token Scopes** (not User), **one at a time** via **Add an OAuth Scope**; **scopes auto-save but events need Save Changes**; the `xoxb-` token is **blank until you Install + Allow**; **reinstall** after any scope change; under Socket Mode **no Request URL** is needed.

**Test it:** with the bot running, @mention it in a channel with an **in-scope**
question (expect a **cited answer**) and an **out-of-scope** one (expect
**escalation**, no guess); DM it; tap 👍/👎. No workspace handy?
`python eval/simulate_bot.py` drives real questions through the live `/ask` backend and
the exact render code each adapter uses (Webex **and** Slack) — feedback taps included —
printing what each would send, without any platform tokens.

---

## Teams (shipped — needs an Azure Bot + a public HTTPS URL)

Teams **inverts** the transport: Webex/Slack are outbound websockets (no public URL);
Teams is **inbound** — Azure Bot Service POSTs each message to an HTTPS webhook KAI
hosts at `/api/messages`. KAI ships a working `TeamsAdapter`
([`kai/chat/teams.py`](../kai/chat/teams.py)) — the **same** `ChatService` + Adaptive
Card as the other platforms.

> ⚠️ **This is the only platform that cannot be exercised without a live Azure tenant
> and a public HTTPS endpoint.** Budget for an Azure account and either a dev tunnel
> (ngrok / Azure Dev Tunnels) or a real domain + TLS cert before you start. The parsing
> and routing are unit-tested in this repo, but the live Connector + Auth round-trip is
> verified only in *your* tenant.

First: `pip install '.[teams]'` (adds `pyjwt`, used to validate the inbound bot token).

### Step 1 — Create the Azure Bot

1. **Sign in** at <https://portal.azure.com> with an account that can create resources.
2. Click **Create a resource** (top-left, the **+** tile), search for **`Azure Bot`**, select it, then click **Create**.
3. Fill the **Create an Azure Bot** form:
   - **Bot handle** — a display name, e.g. `KAI` (just a label).
   - **Subscription** and **Resource group** — pick existing ones or click **Create new**.
   - **Pricing tier** — **F0** (free) is enough to start.
   - **Type of App** — choose **Multi Tenant** (simplest; leave `TEAMS_APP_TENANT_ID` blank later). Pick **Single Tenant** only if your org requires it — then you'll also copy the tenant ID in Step 2.
   - **Creation type** — leave **Create new Microsoft App ID** selected.
4. Click **Review + create**, then **Create**. Wait for **Your deployment is complete**, then click **Go to resource**.

### Step 2 — Copy the App ID and create a client secret

You now collect two secrets from the bot's **Configuration** blade.

5. In the bot resource's left menu, under **Settings**, click **Configuration**.
6. Copy **Microsoft App ID** (a GUID) → this is `TEAMS_APP_ID`.
7. **Single-tenant bots only:** also copy **App Tenant ID** → `TEAMS_APP_TENANT_ID`. (Multi-tenant bots leave this blank — it defaults to `botframework.com`.)
8. Still on the Configuration blade, next to **Microsoft App ID** click **Manage** (this opens the app registration in **Microsoft Entra ID** / Azure AD).
9. In the left menu click **Certificates & secrets** → tab **Client secrets** → **New client secret**.
10. Give it a description (e.g. `kai`), pick an expiry, click **Add**.

> **Copy the Value, NOT the Secret ID.** The table shows two columns — **Value** is the actual password, **Secret ID** is just a GUID reference and is useless to KAI. The **Value** is shown **once**; if you navigate away it is permanently masked and you must create a new secret. Also **note the expiry** — when the secret expires, outbound replies silently fail and you must create a fresh one.

11. Copy the **Value** → this is `TEAMS_APP_PASSWORD`.

### Step 3 — Configure `.env` and run the bot

12. **`.env`** (the Teams vars are not in `.env.example` — add them):
    ```dotenv
    CHAT_PLATFORM=teams                       # MUST be 'teams' (default is 'webex') or the adapter won't start
    TEAMS_APP_ID=<Microsoft App ID GUID>      # required; the webhook refuses ALL requests (401) until this is set
    TEAMS_APP_PASSWORD=<the secret Value>     # required; used to fetch tokens for outbound replies
    TEAMS_APP_TENANT_ID=                      # single-tenant only; blank = multi-tenant (botframework.com)
    KAI_API_URL=http://127.0.0.1:8100         # where the bot reaches the KAI API
    # TEAMS_PORT=3978                          # (default) inbound webhook port; TEAMS_HOST=0.0.0.0 to change bind
    ```
13. **Run the bot:** start the KAI API first (`python run/setup.py start`), then `CHAT_PLATFORM=teams python run/setup.py bot` — the bot won't launch until the API is reachable. It serves `POST /api/messages` on `:3978` (override with `TEAMS_PORT`; bind address with `TEAMS_HOST`).

### Step 4 — Expose a public HTTPS URL and set the Messaging endpoint

14. Point a **public HTTPS** URL at the port from Step 13:
    - **Dev:** start a tunnel, e.g. `ngrok http 3978`, and copy the `https://…` forwarding URL. (A fresh ngrok install first needs `ngrok config add-authtoken <token>` once, using the token from your ngrok dashboard.)
    - **Prod:** use a real domain with a valid TLS certificate (Azure requires HTTPS — plain HTTP is rejected).
15. Back in the Azure bot's **Configuration** blade, set **Messaging endpoint** to your public URL **plus the path**: `https://<public-host>/api/messages`. Click **Apply**.

### Step 5 — Enable the Teams channel

16. In the bot resource's left menu, under **Settings**, click **Channels**.
17. In **Available Channels**, click **Microsoft Teams**, accept the **Terms of Service**, then **Apply**. The channel should now show as running.

### Step 6 — Build and sideload the Teams app package

A bot in Azure can't be opened in Teams until you wrap it in an app package and install it.

18. Go to the **Developer Portal for Teams**: <https://dev.teams.microsoft.com> → **Apps** → **New app**.
19. Under **Configure → Basic information**, fill the required fields (app name, descriptions, developer name, website, and the privacy/terms URLs — these must be reachable HTTPS pages).
20. Under **Configure → App features**, click **Bot** → **Select an existing bot** → paste your `TEAMS_APP_ID` (the manifest's bot ID **must equal** your `TEAMS_APP_ID` or messages won't route to your webhook) → tick the scopes where it should work (**Personal**, **Team**, **Group chat**) → **Save**.
21. Provide the **two required icons** (a color icon and a transparent outline icon) under **Branding** if prompted.
22. **Publish → Download the app package** to get the `.zip`.
23. **Sideload it into Teams:** open Teams → **Apps** → **Manage your apps** → **Upload an app** → **Upload a custom app** → select the `.zip`.

> **Custom app upload must be allowed by your tenant.** If you don't see **Upload a custom app**, a Teams admin must enable custom app uploads (Teams Admin Center → **Teams apps → Setup policies**). That's also where an admin scopes the app to specific users/teams — **Teams answers anyone who can reach the bot**, so gate it here.

### Test it

- **Parsing/routing (no Azure):** the pure helpers — Activity classification (message vs feedback vs ignore), mention stripping, reply-activity shape — are unit-tested in `tests/test_teams.py`; run `pytest -k teams` to verify the logic with no tenant. There is **no unauthenticated local smoke**: the webhook refuses inbound requests until `TEAMS_APP_ID` is set (see Gotchas).
- **End-to-end (in Teams):** with the Azure bot configured + the public HTTPS endpoint live + the app sideloaded, @mention the bot with an **in-scope** question → **cited answer**; an **out-of-scope** one → **escalation** (no guess); tap 👍/👎 on the card.

**Gotchas:** a live Azure tenant **and** a public HTTPS URL are mandatory (budget the tunnel/cert) — plain HTTP is rejected. Set `CHAT_PLATFORM=teams` or the adapter never activates. With `TEAMS_APP_ID` unset the webhook **refuses every inbound request** (returns `401`) — it can't verify the Bot Framework JWT, and its `serviceurl` claim is bound to the activity, so it won't process unauthenticated activities or leak Connector tokens. Copy the secret's **Value**, not the **Secret ID**, and copy it **immediately** (shown once); note its **expiry**. The **Messaging endpoint** must end in `/api/messages`. Single-tenant bots also need `TEAMS_APP_TENANT_ID`; multi-tenant leave it blank. Replies over **~25 KB** are split into multiple messages (the feedback card rides only on the last piece). Teams renders the **same Adaptive Card** as Webex.

---

## Running several platforms at once

`CHAT_PLATFORM` selects **one** platform per process. To serve Webex **and** Slack
simultaneously, run **two bot processes** against the **same** KAI API:

```bash
python run/setup.py start                         # the shared brain (once)
CHAT_PLATFORM=webex python run/setup.py bot &     # Webex bot process
CHAT_PLATFORM=slack python run/setup.py bot &     # Slack bot process
```

This is deliberate: each platform process fails, restarts, and is supervised
independently, and they all share one knowledge base + one set of guards. (A future
convenience launcher could fan these out from one command — not needed today.)

## Optional bot copy (any platform)

```dotenv
BOT_ACK_MESSAGE=🔎 _Searching the knowledge base…_   # the instant "thinking" message
BOT_ANSWER_PREFIX=Here's what I found:               # bolded above the answer (blank = none)
WEBEX_EDIT_IN_PLACE=true                             # edit the ack into the answer (no repost)
WEBEX_FEEDBACK_CARD=false                            # 👍/👎/escalate card under answers
```
