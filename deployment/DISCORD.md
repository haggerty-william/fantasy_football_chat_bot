# Discord slash commands

Keep the existing webhook: it continues sending scheduled reports. Commands use
a separate bot connection to Discord. No public HTTP endpoint, port forwarding,
or paid AI API is needed.

Store the scheduled-report webhook in the ignored `deployment/secrets.env` file
as described in [Minikube setup](README.md). The deployment script imports it into
`fantasy-football-private-env`; keep the tracked `k8.yaml` free of credentials.
The bot token below stays in the separate `fantasy-football-discord-app` Secret.

1. Open the [Discord Developer Portal](https://discord.com/developers/applications)
   and create a dedicated application for this league bot.
2. Open **Bot**, generate/reset its token, and keep it private. Leave privileged
   gateway intents disabled.
3. Under **Installation**, configure **Guild Install** with the `bot` and
   `applications.commands` scopes and **View Channels**, **Send Messages**, and
   **Embed Links**, and **Attach Files** permissions. Use the install link to add it to your server.
   You need permission to manage that server. Do not enter an Interactions Endpoint URL:
   this implementation receives interactions over the outbound Discord gateway.
4. In Discord, enable **User Settings → Advanced → Developer Mode**. Right-click
   your server and choose **Copy Server ID**. Optionally copy the league channel ID.
5. After deploying the updated bot, run from the repository root:

   ```powershell
   .\deployment\configure-discord.ps1 -GuildId YOUR_SERVER_ID -ChannelId YOUR_LEAGUE_CHANNEL_ID
   ```

   Enter the **bot token** at the hidden prompt. This stores it in the
   `fantasy-football-discord-app` Kubernetes Secret and restarts the bot. Omit
   `-ChannelId` to allow commands throughout the configured server. The script
   targets the `minikube` context and `default` namespace unless overridden.
6. Check logs for `Discord interactions ready`. Commands register to the configured
   server at startup. If registration fails, verify the token, server ID, install
   scopes, and channel permission overrides. Use a dedicated application because
   startup synchronization manages that application's command list in this server.

## Commands

| Command | Result |
| --- | --- |
| `/matchup` | Current matchups, scores, and projections |
| `/matchup team:ABC` | One team's matchup; accepts name or abbreviation |
| `/standings` | Fresh league standings |
| `/recap` | Last completed week's scores, awards, and local AI commentary |
| `/recap week:2` | A specified week; the current week is labeled in progress |
| `/trades` | Latest five completed trades in the last seven days, with AI verdicts |
| `/trades days:30` | Search up to 30 days of completed trades |

Replies are private by default. Add `share:true` to show the report in the channel.
Team cards use the current ESPN team logos in both commands and scheduled webhook
reports. Standings and matchup boards display one attached PNG without a duplicate
text table. If image rendering fails, or the bot lacks attachment permission, the
complete text report is used instead. Recaps use one final-score board, one grouped
awards section, and optional AI analysis. Waivers, trades and lineup alerts group
related information instead of making a separate card per team. Power rankings use
full team names with labeled values rather than an abbreviation-heavy table.
Public ESPN SVG logos are converted to PNG and uploaded to Discord;
custom ESPN logos are retrieved using the bot's existing ESPN login on the exact
ESPN image endpoint, then uploaded as PNG too. Unsupported or temporarily unavailable
logos use initials as a last-resort fallback. Standings include W-L-T, Win% (ties
count as half a win), and GB relative to the first team in ESPN's standings order.
Other cards omit inaccessible images instead of emitting broken links.
Matchup commands and scheduled matchup/score updates use one consolidated board,
with each team's logo directly beside its name, record, score and projection.
The duplicate name/abbreviation cards are removed; the board and optional analysis
fit in one message for the ten-team league. Team-specific awards, trades, waivers, and rankings
also use logos. Logo downloads are restricted to ESPN's public image CDN and custom-image
endpoint, bounded, and cached. ESPN cookies are sent only to the HTTPS custom-image endpoint,
never to the public CDN or redirect destinations.
Reports reuse the styled Discord embeds and suppress mentions. Commands fetch new
ESPN data; recap commentary uses LM Studio when available and falls back to the
ESPN report. Before local inference, the bot supplies a bounded selection of ESPN
player stats, recent weekly fantasy points, projections and current injury statuses.
Current-week reports also receive up to six matching news briefs from ESPN's public
NFL news feed, published within the last 72 hours. The feed is cached for ten minutes;
unavailable or expired data is never presented as fresh. This is focused ESPN retrieval,
not unrestricted web browsing, and does not require a paid search or AI API.

Analysis is plain commentary with no separate sources card or inline citations.
The local model misattributed citations during live validation, so visible sources
were removed in favor of the requested plain-analysis fallback. Source content,
URLs, and retrieval/publication dates still go to the model as context.
Historical recaps exclude today's statuses and news; historical lineup slots are only
supplied when historical box scores are available. Player context is a partial sample
(up to 40 detailed players for focused reports, or 24 for league-wide reports, plus compact rosters; a default 16,000-character budget for focused reports or 24,000 for league-wide reports), prioritizing starter injury risks and
scoring contributions across teams. A selected matchup uses only its two lineups;
transactions prioritize named players and include the surrounding rosters of involved teams. Missing players or stats stay
unknown. ESPN's feed may miss breaking news, and its injury status update times are
not supplied. A new request fetches a fresh league snapshot but does not guarantee the
upstream source has updated. Source failures fall back to available facts, and model
failures still deliver the original report.

Requests are rate limited and only
one command report is fetched at a time to protect ESPN and the local model.

The optional environment variables are `DISCORD_BOT_TOKEN`, `DISCORD_GUILD_ID`,
and `DISCORD_COMMAND_CHANNEL_ID`. Without a token, scheduled reports continue and
commands remain disabled. Updating the Secret requires a pod restart.

## Hourly league updates

With `LEAGUE_UPDATES=True` (default), one job checks at startup and the top of each
hour for rostered players' injury-designation changes, new visible trade proposals
and explicit proposal-status changes, and completed waiver/free-agent adds and drops.
Starters, bench and IR players are all included. Unknown statuses are skipped;
removing an injury designation does not establish full health or participation.
Proposals are limited to what the configured ESPN account can see. A disappearing
offer is never treated as declined, accepted or completed.

Each new source establishes a quiet baseline instead of announcing old activity.
Later successful checks queue changes durably; source failures preserve that source's
checkpoint without preventing other sources from reporting. No changes means no post.
Verified facts are grouped into readable cards, followed by Graham Ellis and then Rex
Callahan in separate messages with model-name footers. If analysis is unavailable,
the factual report still sends. Detection can take up to an hour plus ESPN's delay.

The same job retains completed-trade polling and its existing delivery ledger when
`TRADE_REPORT=True`. Set `LEAGUE_UPDATES=False` to keep only that hourly completed-trade
schedule. Its first activation starts a watch without reposting historical trades.
`/trades` still retrieves older completed deals without changing automatic delivery state.

| Setting | Default | Effect |
| --- | --- | --- |
| `LEAGUE_UPDATES` | `True` | Startup/hourly injury, visible proposal and completed roster-move changes |
| `TRADE_REPORT` | `True` | Completed-trade announcements within the hourly job, or alone if league updates are disabled |
| `TRADE_STATE_PATH` | `data/trades.sqlite3` | SQLite snapshots, queued updates and delivery ledgers; deployment points it at `/var/lib/gamedaybot/trades.sqlite3` |

`TRADE_STATE_PATH` points to SQLite state on the `fantasy-football-trade-state`
PVC mounted at `/var/lib/gamedaybot`. Keep this PVC across upgrades. Event identity
uses stable source/player IDs and status revisions; changing team names does not
cause reposts. Keep one replica and the Recreate rollout strategy.

The bot reserves each update batch or completed trade before sending to prevent
duplicate automatic announcements. A crash or ambiguous send timeout can therefore
leave a notice unannounced or partly delivered: `claimed`/`uncertain` entries are never
automatically retried, because Discord webhooks do not provide durable exactly-once
delivery. Errors are logged; completed trades can still be viewed with `/trades`.
Do not delete the ledger
to resolve a delivery issue. Its scope includes the league, season and destination.

Trade analysis now judges the deal, gives a tentative winner/loser and predicts
roster impact using traded-player stats, recent news and current positional depth.
It uses pointed league banter and roasts poor decisions. Forecasts are opinions,
not guarantees; missing evidence is not filled in with invented player facts.

See [Discord's application setup guide](https://docs.discord.com/developers/quick-start/getting-started).

## Neighborhood features

New slash commands (private unless `share:true` is selected):

| Command | What it does |
| --- | --- |
| `/rivalry` | Closest projected matchup, this-season meeting history, seed context and local AI forecast. |
| `/monday` | Both teams' remaining starters and the net points needed to take the lead. |
| `/awards` | Last completed week's eligible bench swap, narrowest escape, highest-scoring loser and projected upset. |
| `/playoffs` | Current seeds, bubble matchups and conservative record-based clinch/elimination bounds. |
| `/tradefollowup` | Latest three eligible trade reviews after two full completed scoring weeks. |
| `/pickem team:<name>` | Save/update one matchup winner; omit `team` to view your picks and the leaderboard. |
| `/alerts team:<name>` | Opt into private starting-player injury alerts. `/alerts enabled:false` unsubscribes. |

Rivalries accompany Thursday matchups, awards replace the old Tuesday trophy section,
and a compact playoff/bubble card accompanies standings. Monday's existing 6:30 p.m.
Eastern report now shows remaining starters instead of an abbreviated score table.
Trade follow-ups run Tuesday at 6:35 p.m. in the configured timezone (maximum three
reviews per run; each review is reserved once). The pick'em leaderboard posts at
6:40 p.m. Tuesday only when people have participated. No extra empty reports post.

Pick'em locks ALL selections at the week's FIRST NFL kickoff, using ESPN's NFL
schedule. Missing or mismatched kickoff data blocks new picks. Correct picks earn
one point, tied fantasy matchups half a point, unpicked games zero. Settlement waits
for ESPN's final matchup outcome. Picks are per Discord user, league and season.

Alerts scan every ten minutes and only send within two hours before the player's
kickoff. Only opted-in users receive DMs; enable server-member DMs in Discord.
Starting-player questionable/doubtful/out/IR/suspension statuses are checked, with
a direct ESPN link beside the status. Questionable is never presented as confirmed
inactive. No message is sent for an unchanged status already reported that week.
ESPN can publish late or incomplete updates; the bot does not guarantee every inactive.
Blocked DMs are logged and not repeatedly retried. Unsubscribe/resubscribe does not
replay old alerts. Alerts are supplemental to checking your fantasy lineup.

Bench awards show the best eligible single swap, not an optimal-lineup reconstruction
or proof that the player was available when a lineup locked. Historical projections
may change. Trade reviews measure received players' actual starting-lineup points
over two full weeks after the trade, not total trade value or causal wins gained.
Missing roster observations are labeled. Clinch bounds leave ties unresolved;
divisional, median-scoring and other unsupported formats explicitly show clinches
as unconfirmed rather than inventing playoff rules.

Preferences, picks and notification reservations persist in `community.sqlite3`
beside `TRADE_STATE_PATH` on the existing PVC. Override with `COMMUNITY_STATE_PATH`
if needed. Like trade announcements, notifications reserve before sending to avoid
duplicates after ambiguous delivery; a crash or failed send can suppress that notice.


## Richer local analyst evidence

The analyst receives exact ESPN scoring items (including overrides), verified lineup
slot IDs, compact relevant rosters, current NFL kickoff/game states, and calculated
recent performance trends. Rivalry reports focus on their two teams; trade reports
include both teams' depth. Larger reports disclose omitted details and roster rows.
`AI_CONTEXT_CHAR_LIMIT` defaults to 16000 for focused reports and 24000 for league-wide reports and is bounded to 16000?48000. The current
Gemma model has a 32768-token loaded context; character limits are not token limits.

ESPN news remains short: at most six recent matching headlines and descriptions
(maximum 280 characters per description), with source URLs and dates retained
internally. No full articles or video clips are downloaded. Current news and injury
statuses are excluded from historical recaps. NFL game states distinguish future,
in-progress and completed games, and lineup locks use kickoff times too.

Free nflverse player usage and PFR snap-count releases provide completed-game
carries, targets, target share, snaps, snap share and related stats. Joins use
ESPN-to-GSIS/PFR IDs from nflverse's player table, never approximate player-name
matching. Missing, ambiguous, future-week or unavailable data is not invented.
Data is cached on the existing PVC (usage one hour, player IDs one day) and expired
cache entries are not silently reused after fetch failures. Each supplied usage
row retains its week; this is supplemental data, not a live scoring source.
Official league fantasy points continue to come from ESPN with this league's rules.

Python calculates recent averages, range, variability, projection differences and
starter point contribution. Zero-score rows without participation evidence are
excluded from trend samples. League history includes completed records, streaks,
past meetings and recent relevant trades. A durable pregame ESPN-projection ledger
starts accumulating now; it does not reconstruct or invent past AI predictions.
Snapshots captured after kickoff are not stored as pregame forecasts. Multi-week
fantasy matchups do not create weekly forecast records.

Before publishing, deterministic checks look for unsupported numeric claims, new
person-like subjects, direct player-point/usage/status mismatches, clear ownership
and trade-direction errors, unsupported clinches and advice about locked players.
These checks are conservative safeguards, not a complete semantic fact checker.
A rejected draft gets one correction attempt (at most 45 seconds), with explicit
trade directions repeated at the end of the prompt. If it still fails, Discord
explains that the draft could not be verified and shows calculated highlights.
Subjective consistency opinions alone do not block delivery. The Discord reports remain concise and have no sources dump.
Logs record the configured model, context size, prompt token count (when returned),
elapsed generation time and check result, without logging credentials or responses.

Primary feed documentation:
- https://nflreadr.nflverse.com/articles/nflverse_data_schedule.html
- https://nflreadr.nflverse.com/articles/dictionary_snap_counts.html
- https://github.com/nflverse/nflverse-data

### Model-requested research

The bot now has 32 read-only tools. The [complete ESPN research catalog](../docs/ESPN_RESEARCH_TOOLS.md)
lists the league, schedule, draft, player discovery, NFL game and transaction tools,
with source references and visibility limits. Current reports receive compact upcoming
matchups, visible pending offers and recent completed trades automatically. Historical
reports exclude these current snapshots. Pending offers are limited to the configured
ESPN account's visibility; a trade acceptance event does not prove players transferred.

Commentary runs sequentially: **Graham Ellis** checks the evidence, then **Rex Callahan**
receives Graham's validated response and the same structured packet. Both are fictional
announcers; Rex adds pointed management banter and uncertain forecasts and can address
Graham by name. Both use `AI_MODEL`; they do not require two loaded models. After the
report, Discord sends Graham's card first and Rex's response in a separate message,
within Discord's message limits. Speaker headings use their names; the footer shows
the configured model name, for example `GameDayBot • Gemma 4 12B QAT`. If the second voice
fails validation, times out or repeats the analyst, the analyst still sends.
Set `AI_SECOND_COMMENTATOR=False` to use only the analyst.

The two voices share the existing 600-second maximum, four research rounds and 12
calls, with time reserved for the reaction. Individual tool results and their combined
evidence are bounded; large queries ask the model to narrow the team/week/page. No
Discord conversation or reaction listeners are enabled by this change.

`AI_RESEARCH_TOOLS=True` (default) enables `get_player_news`, `get_player_stats`,
and `get_player_status` through LM Studio's chat-completions function-calling API.
The model can request one to three exact player IDs from the report per tool call.
Four rounds and three executed calls per round are allowed (12 total), followed by synthesis.
Research shares the interaction time budget with generation and correction.
The bot reuses the ESPN snapshot, expands player details omitted from the initial
prompt, and retrieves cached dated news and completed-game nflverse usage. Missing
data stays unknown. Historical reports cannot fetch current news or statuses.
Results are merged into the validation evidence before publishing. Logs show tool
names, requested player IDs and result counts. Set the flag to `False` to disable.

These tools use fixed data sources; they do not accept arbitrary URLs, execute
model-generated code, or scrape full articles. Bionic's Allowed Websites setting
does not configure this bot workflow. No additional paid API is required.

### Calculated research and receipts

Five manager-focused tools are also available through the same optional tool-call
workflow and shared 12-call budget:

* `get_schedule_luck`: completed scores against every other team, with ties worth
  half a win, compared with actual opponent results. Excludes live weeks and byes.
* `get_lineup_efficiency(team_id)`: exact eligible-slot hindsight optimization of
  historical rosters, including FLEX; reports starter points, missed points and
  efficiency. Missing stats/lineups are omitted, IR excluded. Current slot rules
  and historical snapshots cannot reconstruct every game-time decision.
* `get_waiver_return(team_id)`: historical starter contributions after verified
  pickup dates and before subsequent drop/trade events. Examines the latest 100
  activity records; this is explicitly partial coverage, not guaranteed season totals.
* `get_draft_value(team_id)`: draft order versus scoring rank within the same
  position among drafted players with available stats. Uses original draft owner,
  includes later production elsewhere, and excludes live weeks. Auction drafts
  return unavailable. This proxy is not an ADP or keeper-cost valuation.
* `get_league_personality`: reads user-supplied nicknames, rivalries and running
  jokes. No automatic lore generation or model write access.

All team arguments are exact keys from `research_context.teams`. Historical lineup tools
require verified single-week matchup periods. Tool errors mean unavailable evidence.
ESPN manager names are supplied automatically with the initial context.

### Structured commentary input

All AI commentary uses a shared JSON packet (schema version 2). Each entry in
`research_context.teams`, keyed by ESPN fantasy team ID, contains the team's name,
managers, compact rosters, detailed player evidence, history, trades and highlights.
Names are declared once. Report text and cross-team relations use ID references.
Later research results join the same team record; shared evidence stays in
`shared_research`. Tool replies point to the results in the refreshed initial JSON
instead of duplicating large evidence blocks. Internal validation retains the original
ESPN objects, so serialization does not change recorded ownership or trade direction.

Manager identities are built independently of detailed player research, so a source
failure does not discard them. Prompts use manager names when judging management
decisions, while team names remain natural for scores and standings. Names are
current ESPN profiles, not verified historical management or Discord identities.

Discord tables and image boards display `Team Name (Manager)` independently of
the AI's wording. Shared first names use last initials, with full names for remaining
collisions. Wider standings, matchup/final-score and power-ranking boards keep
the team icons and numeric columns; text fallbacks use the same labels.

#### User-supplied personality notes

Start with `deployment/league-personality.example.json`. Each entry in `notes`
has exactly `kind` (`nickname`, `rivalry`, or `running_joke`), `team_ids` (one to
four exact string IDs), and `text` (1–400 characters). Maximum 30 entries / 32KB.
The file's league ID and season must match; current notes are excluded from
historical recaps. Notes are treated as untrusted descriptive data, never prompts.

`LEAGUE_PERSONALITY_PATH` may point to a fixed JSON file. By default the bot reads
`league-personality.json` alongside `COMMUNITY_STATE_PATH` or `TRADE_STATE_PATH`.
For the existing Minikube PVC this is `/var/lib/gamedaybot/league-personality.json`.
To install your completed file without restarting:

```powershell
$botPod = kubectl --context minikube -n default get pod -l app=fantasy-football-bot -o jsonpath='{.items[0].metadata.name}'
kubectl --context minikube -n default cp ./league-personality.json "${botPod}:/var/lib/gamedaybot/league-personality.json" -c bot-container
```

The persistent volume retains notes across deployments. If no file is present,
the tool explicitly returns no supplied lore. No example jokes are installed.

Six additional tools are available to the analyst:

* `simulate_trade_impact`: exact assignment of players to verified eligible lineup
  slots, comparing current rosters against a counterfactual that reverses the latest
  report trade. Includes depth counts and verified bye conflicts. Missing projections
  suppress gain estimates; subsequent roster moves can make reconstruction unavailable.
* `find_available_replacements`: up to 60 ESPN available/waiver players, comparing
  projected starting-lineup gains for leading candidates. Cached ten minutes. This
  does not make roster moves or assume waiver availability, a free roster spot, or
  that locked lineups can be changed.
* `get_workload_changes`: latest completed-game targets, carries and percentage-point
  share changes against available prior games. Missing metrics remain unknown.
* `get_schedule_outlook`: next four weeks of NFL opponents, verified byes and fantasy
  opponents. A bye is inferred only from a complete 17-game regular-season schedule.
* `simulate_playoff_odds`: 600 reproducible simulations using completed team scores,
  remaining schedules, division qualifiers, wins and points-scored seeding. Means are
  shrunk toward the league average; current-week live scores are not incorporated.
  Reports overall and conditional win/loss odds. Unsupported median scoring,
  multi-week formats or tiebreakers return unavailable rather than fabricated odds.
* `review_previous_predictions`: exact validated AI commentary stored in the existing
  community SQLite database, paired with later completed matchup results. Collection
  begins with this release. These are generated drafts, not proof of Discord delivery;
  no invented historical takes or automatic grading of subjective trade opinions.

All tools are read-only against ESPN. Calculated results enter the validation packet.
The shared four research rounds/12-call limit applies. The archive stores only
validated commentary and marks game states at generation, so in-progress reactions
are distinguishable from pregame forecasts.

### Longer generation windows

`AI_REQUEST_TIMEOUT_SECONDS` defaults to 600 (allowed 30-600). This is the read
timeout for one LM Studio request, capped by the remaining total budget.
`AI_ANALYSIS_TIMEOUT_SECONDS` defaults to 600
(allowed 120-600), covering both voices, research and correction. With two voices,
30% of that window (at most 180 seconds) is reserved for Rex. The analyst
keeps a 45-second correction reserve inside its own portion. A completed analyst
is retained if there is too little time left for the second voice.
The Discord command waits this budget plus 60 seconds: 660
seconds by default. Commands still defer immediately and keep the gateway responsive.
The extra time lets slower local generations finish instead of discarding them at
the previous 60-second request/120-second command limits. Timeouts remain finite.

Analysis allows up to 350 words in short paragraphs, 1,400 output tokens per
analyst generation/correction, and a 6,000-character hard cap. Rex uses at
most 900 output tokens and is prompted for 50-100 words. Repeated statistical
summaries and overlong reactions trigger a rewrite or omission. Discord splits longer
analysis into numbered embeds and groups them within message size limits.

### Keep the local model loaded

The bot now enforces readiness itself. `AI_KEEP_MODEL_LOADED=True` (default) checks
the configured `AI_MODEL` at startup, every 60 seconds, and before each analysis.
If the LM Studio API is available but that installed model is absent from memory,
the bot explicitly loads it via `/api/v1/models/load` and verifies readiness before
inference. `AI_MODEL_CONTEXT_LENGTH` defaults to 32768 (bounded 8192-65536).
Explicit loads do not specify an idle TTL. Existing loaded instances are left alone;
if an existing timed instance expires, the keeper reloads it on the next check.
This setting is part of bot configuration and survives bot/app restarts. It does
not start a closed LM Studio app, download missing models, unload other models,
or switch to a different model. LM Studio's API server must be enabled.
Set `AI_KEEP_MODEL_LOADED=False` to stop automatic reloading when intentionally
unloading the bot's model. Readiness loading has a 90-second budget inside the
overall analysis timeout and retries on the next background check if unavailable.

For manual loading without the keeper:

After opening LM Studio/Bionic, load the bot's configured model without `--ttl`:

```powershell
lms load google/gemma-4-12b-qat --context-length 32768 --identifier google/gemma-4-12b-qat --yes
```

CLI-loaded models have no idle timeout unless `--ttl` is supplied. To change an
existing timed instance, unload that specific instance first, then load it again.
For a timed instance, append `--ttl 14400` for four hours. TTL is idle time in
seconds, not context length. The API server's JIT/auto-unload settings control the
default for models loaded on demand; TTL does not itself load a model at app
startup. Run the load command again after restarting the app. Models manually
loaded through the CLI are not subject to JIT Auto-Evict.

Official documentation: https://lmstudio.ai/docs/developer/core/ttl-and-auto-evict
