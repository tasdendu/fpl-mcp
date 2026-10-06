# Tashi FPL MCP

A deployable, read-only Model Context Protocol server that gives ChatGPT live Fantasy Premier League data and mini-league analysis. It defaults to Tashi's FPL entry ID `354978`.

The server never logs in to FPL, changes a squad, makes a transfer, or selects a captain. All recommendations remain decisions for the manager to confirm manually.

## What ChatGPT can use

The MCP exposes these tools:

| Tool | Purpose |
| --- | --- |
| `get_fpl_overview` | Current/next gameweek, deadline, teams, and data limitations |
| `get_my_team` | Latest published squad for entry 354978, with private state marked unknown |
| `get_price_changes` | Official net price changes this gameweek, without predictions |
| `search_players` | Resolve names to IDs and inspect price, form, status, and ownership |
| `get_player` | Detailed player stats and upcoming fixtures |
| `get_fixtures` | Fixtures and official FDR by gameweek or club |
| `get_manager` | Team name, rank, points, and classic league memberships |
| `get_manager_history` | Gameweek history, chips, ranks, and transfer costs |
| `get_manager_transfers` | Completed public transfer history |
| `get_manager_gameweek` | Squad, captain, bench, chip, and gameweek result |
| `get_mini_league_standings` | Mini-league table and rival manager IDs |
| `compare_managers` | Common picks, differentials, and captain comparison |
| `analyze_mini_league_ownership` | Mini-league ownership, EO, and captain counts |
| `analyze_captains` | Transparent captain shortlist with optional mini-league EO |
| `rank_transfer_targets` | Position/price/fixture/form transfer shortlist |
| `get_live_mini_league` | Estimated live ranks using official live points |

## Important FPL limitation

The public FPL API reveals a manager's gameweek picks only after the deadline. It does not provide a reliable current private squad, bank balance, selling prices, or free-transfer state. Historical bank values can appear in public gameweek history; they are historical snapshots. This server intentionally does not accept FPL login credentials.

For a recommendation, ChatGPT should therefore check completed transfers and the latest available public squad, and ask you to confirm any private state that cannot yet be observed.

## Quick start with Docker

```bash
cp .env.example .env
# Edit MCP_ALLOWED_HOSTS in .env to include your actual public hostname.
docker compose up -d --build
docker compose ps
```

The Streamable HTTP endpoint is then available locally at:

```text
http://127.0.0.1:8000/mcp
```

Test tool discovery with the MCP Inspector:

```bash
npx @modelcontextprotocol/inspector@latest
```

Enter `http://127.0.0.1:8000/mcp` and select Streamable HTTP.

## Run without Docker

Python 3.11 or newer is required.

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.lock
pip install -e '.[dev]'
cp .env.example .env
fpl-mcp
```

Run quality checks:

```bash
ruff check .
pytest -q
```

## Publish over HTTPS

ChatGPT needs either a public HTTPS MCP endpoint or an OpenAI Secure MCP Tunnel. For the straightforward VPS deployment:

1. Point a DNS name such as `fpl-ai.your-domain.com` to the server.
2. Start this project with Docker Compose.
3. Adapt `deploy/nginx-fpl-mcp.conf` to your domain and existing certificate paths, and add that same hostname to `MCP_ALLOWED_HOSTS` in `.env`. If a certificate is not available yet, obtain one through your existing certificate manager before enabling this HTTPS server block.
4. Test `https://fpl-ai.your-domain.com/mcp` with MCP Inspector.

The Nginx example binds the container only to localhost, disables response buffering for MCP streaming, and applies a modest request limit. If Cloudflare proxies the hostname, disable caching for `/mcp` and ensure streaming responses are not buffered.

## Connect it to ChatGPT

Current OpenAI setup flow:

1. Open **ChatGPT → Settings → Security and login**.
2. Enable **Developer mode** (availability can depend on account/workspace policy).
3. Open **ChatGPT Plugins**, select **+**, and create a connection.
4. Enter a clear name such as `Tashi FPL Analyst`.
5. Enter the full public URL, including `/mcp`:

   ```text
   https://fpl-ai.your-domain.com/mcp
   ```

6. Select no authentication if asked; this version exposes only public FPL data. Review the 16 discovered tools. Every tool should be marked read-only.
7. In a new chat, add the connection from the tools menu.

Useful first prompts:

```text
Use Tashi FPL Analyst to find my classic mini leagues and show their IDs.
```

```text
Analyze my team (entry 354978) and my nearest mini-league rivals before the next deadline. Check my latest public squad, completed transfers, player status, fixtures, and mini-league effective ownership. Separate official facts from your judgment.
```

```text
Track this gameweek's live mini-league table and explain which captains and differentials are changing my position.
```

## Configuration

| Variable | Default | Meaning |
| --- | --- | --- |
| `FPL_ENTRY_ID` | `354978` | Default manager entry |
| `FPL_BASE_URL` | Official FPL API | Override mainly for tests |
| `FPL_USER_AGENT` | `Tashi-FPL-MCP/1.0` | HTTP user agent |
| `REQUEST_TIMEOUT_SECONDS` | `15` | Upstream timeout |
| `MAX_LEAGUE_PAGES` | `5` | Safety cap for league pagination |
| `MCP_HOST` | `0.0.0.0` | Bind address in the container |
| `MCP_PORT` | `8000` | MCP service port |
| `MCP_ALLOWED_HOSTS` | Localhost only in code | JSON list; add the exact public hostname |
| `MCP_ALLOWED_ORIGINS` | Local development origins | JSON list of allowed browser origins; requests without Origin are accepted |

## Data and analysis behavior

- Official data is fetched from `fantasy.premierleague.com/api`.
- High-volume bootstrap and fixture data is cached for five minutes.
- Live points are cached for 30 seconds; entry and league data for 60 seconds.
- Each tool includes source URLs, actual upstream fetch timestamps, cache TTLs, and a separate response timestamp. A new response timestamp does not imply newly fetched data.
- Only four upstream requests run concurrently; the in-process cache is bounded to 1,024 entries.
- Mini-league effective ownership sums each pick's official multiplier, so captain and triple-captain effects are represented.
- Ownership includes all squad members; EO uses scoring multipliers, so an unused bench player contributes zero. Results report pagination and failed squads; percentages refer only to the successfully loaded sample.
- Captain and transfer rankings are explicitly labelled as heuristics. They combine current official expected points, form, points per game, availability, minutes, and fixture difficulty. They are not promises or betting models.
- Captain recommendations target only the next gameweek and exclude teams without fixtures. Any mini-league EO alongside them is labelled with the previous published gameweek; rival pre-deadline captain choices are unknown. Transfer shortlists do not establish affordability, club limits, hit cost, or whether you already own a player.
- Captain score: `(0.45 * ep_next + 0.30 * form + 0.15 * points_per_game + 0.10 * mean(6-FDR)) * availability_fraction`. Transfer score: `(0.35 * ep_next + 0.25 * form + 0.20 * points_per_game + 0.15 * (6-mean_FDR) + 0.05 * minutes_reliability) * availability_fraction`. Missing availability means no additional discount. These weights are unvalidated and should only support a shortlist; the fixture count and raw metrics are returned for judgment.
- Live ranks remain estimates until FPL finalizes bonus, autosubs, and the gameweek.
- Live scoring uses published multipliers. It does not independently predict autosubs, captain fallback, or pending bonus. Prior cumulative totals preserve all historical transfer hits; current hits are deducted once. Provisional ranks compare season totals only within loaded pages, with equal points sharing rank; official tie-breaks and custom league starting gameweeks are not applied.

## Security scope

Version 1 is anonymous and read-only because it only exposes data already public through FPL's public endpoints. Anyone who knows the endpoint can use its tools, including the configured default entry. Other public manager/league IDs can be queried too. Do not add FPL credentials to `.env`. Keep the Nginx rate limit. Basic-auth or interactive access-gateway login pages will not work with this anonymous connector setup; use standards-compliant MCP OAuth when access control is needed.

If private account data or write actions are added later, implement OAuth 2.1 according to the MCP authorization specification before connecting that version to ChatGPT.

## Troubleshooting and scope

- A `421 Invalid Host header` means your public hostname is missing from `MCP_ALLOWED_HOSTS`. Edit `.env` and run `docker compose up -d --force-recreate`.
- Opening `/mcp` in a browser can return 405 or an Accept-header error; this is an MCP endpoint, not a webpage. Use MCP Inspector or the included smoke client.
- A picks 404 can mean the deadline has not passed or the entry had not joined that gameweek. Do not substitute a different squad without labelling the gameweek.
- An upstream 403/429 or non-JSON response is reported as a tool error. FPL's web API is an unofficial integration surface and can change or rate-limit. The client is isolated in `src/fpl_mcp/client.py` for maintenance.
- This release supports classic leagues. Head-to-head standings, predicted lineups, private account login, scheduled alerts, third-party price forecasts, and automatic transfers are not implemented.
- On-demand tools do not run background monitoring or create any ChatGPT scheduled tasks.
- The current workspace has no access to your VPS, DNS, or ChatGPT account settings, so no deployment or account connection was performed.

## Verify after deployment

With this project installed locally, run:

```bash
python deploy/smoke_test.py https://fpl-ai.your-domain.com/mcp
```

This initializes a real MCP client, lists the tools, and calls `get_fpl_overview` through the deployed server. A failure is reported rather than treated as a successful upstream check.

## References

- [OpenAI: connect and test an MCP server](https://developers.openai.com/plugins/deploy/connect-chatgpt)
- [OpenAI: MCP authentication](https://developers.openai.com/plugins/build/auth)
- [Official MCP Python SDK](https://github.com/modelcontextprotocol/python-sdk)
- [FPL official bootstrap data](https://fantasy.premierleague.com/api/bootstrap-static/)

Setup documentation checked September 2026. The setup labels shown in your account can vary with rollout and workspace policy.
