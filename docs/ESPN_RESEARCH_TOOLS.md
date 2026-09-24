# ESPN research available to the commentators

The analyst can choose from **32 read-only research tools**. The second commentator receives the analyst's grounded conclusions and the same structured evidence. The bot fetches data on their behalf; enabling websites in LM Studio is not required.

This is a practical catalog of league, roster, player, NFL game, draft, schedule and transaction evidence. It is not an unrestricted ESPN API proxy. ESPN does not publish a complete, supported fantasy API specification, and access to a view does not guarantee all records or fields will be returned. The implementation follows ESPN responses and maintained client source, with explicit unknown/unavailable states when data is missing.

## Data supplied before either commentator writes

The initial report packet already includes the report's scores or standings, relevant rosters and player details, scoring rules, completed performance history, manager identities, and available dated news/usage evidence. Team and manager names appear in the canonical team directory; references elsewhere use team IDs. Additional tool evidence is merged into that structured packet.

Every **current, full report** also receives:

- **Upcoming fantasy matchups:** up to the next three scoring periods, from the league's existing schedule snapshot. Automatic mapping requires verified consecutive single-week matchup periods and reciprocal opponent assignments. Future playoff pairings are marked provisional. Missing or unsupported mappings remain unknown; the model can request the full schedule tool.
- **Trade awareness:** up to three visible pending offers and three completed trades from the last seven days. This initial lookup shares an eight-second budget. Each summary retains at most six player legs and explicitly marks omitted legs. The optional tools can request more detail.

Current offers, current ownership and current news must not be used to explain a historical report. Player-only follow-up fetches do not repeat the automatic schedule/trade lookup. The automatic trade snapshot does not mark any trade as reported: the separate trade notification delivery flow retains its own deduplication ledger.

When the initial packet is too large, completed team-performance comparisons, their metric definitions, identities and trade directions take priority over optional detail. Omission counts identify trimmed roster entries; tools can recover those players and their statistics. Repeated cached tool calls reuse existing evidence instead of adding duplicate copies to the model context.

## League, schedule and draft tools

| Tool | Useful inputs | Evidence and interpretation |
| --- | --- | --- |
| `get_fantasy_schedule` | Optional `team_id` | Remaining scheduled fantasy matchups beyond the short initial preview, returned as compact rows with an explicit column schema to preserve context space. Playoff pairings can change; they do not establish qualification. |
| `get_week_matchups` | `matchup_period`; optional `team_id` | One fantasy matchup period, its covered scoring weeks, participants, scores and completion state. Future placeholder scores are not results. |
| `get_week_box_scores` | `week`, `team_id` | Both lineups in a current or historical matchup, with player actuals, projections and scoring contributions. Does not invent future starters. |
| `get_league_rules` | None | Scoring categories and position overrides, lineup slots, waiver/FAAB rules, trade deadline/review rules, keeper and playoff settings. Unsupported/missing rules remain unknown. |
| `get_team_profile` | `team_id` | Current roster, division, record splits, waiver priority, available FAAB data and acquisition/drop/trade counters. Private account identifiers are excluded. |
| `get_scoring_splits` | Optional `team_id` | Completed matchup scoring averages, medians, spread, recent form and opponent scoring. Coverage is explicit when matchup periods span multiple weeks. |
| `get_draft_board` | Optional `team_id`, `offset`, `limit` | Draft picks, original drafting team, keeper flags and auction costs. Pages contain at most 60 picks. Draft ownership is distinct from current ownership. |
| `get_head_to_head` | `team_id`, `opponent_team_id` | This season's completed and upcoming meetings, scores and official outcomes. No invented multi-season rivalry history. |

## Player and NFL tools

| Tool | Useful inputs | Evidence and interpretation |
| --- | --- | --- |
| `search_players` | `query`; optional `limit` | Name search beyond the supplied rosters; up to ten matching ESPN IDs. Other player tools use those discovered IDs. The active-player index can omit retired or inactive players. |
| `get_player_season_details` | `player_ids` | Up to three players' observed weekly/season stats, projections, eligibility and ESPN-wide ownership. Historical requests exclude later weeks, current ownership/status and current season totals. Projections fetched today are not archived forecasts. |
| `get_player_news` | `player_ids` | Dated ESPN headline/summary briefs for up to three known players. No full articles. A missing brief does not prove there is no news. |
| `get_player_stats` | `player_ids` | Additional report-week statistics, recent trends, targets, carries and available snap usage for up to three known players. Missing data is not zero. |
| `get_player_status` | `player_ids` | ESPN injury designation and NFL game/lineup-lock state for up to three known players. `ACTIVE` does not certify full health. |
| `get_free_agent_pool` | Optional `position`, `offset`, `limit`, ownership/projection thresholds | A page of up to 20 current free agents or waiver players. Additional thresholds filter that page, not the entire player pool. Availability does not guarantee a claim will clear. |
| `get_player_nfl_schedule` | `player_ids`; optional `start_week`, `weeks` | Up to six upcoming NFL weeks for up to three known players, using current NFL affiliation and verified byes. Does not infer opponent difficulty from names. |
| `get_nfl_scoreboard` | Optional `week` | The league season's NFL regular-season games, game states and event IDs. Historical requests cannot inspect later weeks. NFL scores are distinct from fantasy scores. |
| `get_nfl_game_summary` | `event_id` from the scoreboard | Concise NFL team statistics, leaders and recent scoring plays. It fetches neither full articles nor highlight videos. |

## Trade and transaction tools

| Tool | Useful inputs | Evidence and interpretation |
| --- | --- | --- |
| `get_recent_trades` | Optional `team_id`, `days`, `limit` | Completed player transfers from ESPN activity, with sending/receiving team IDs. Up to 20 returned trades over 1–60 days; scans at most 100 activity topics. Coverage/truncation is explicit. |
| `get_pending_trades` | Optional `team_id`, `limit` | Visible proposed trades and accepted offers still awaiting processing. Up to 20 records. Pending data never becomes a completed trade simply because an acceptance timestamp exists. |
| `get_transaction_history` | Optional `week`, `team_id`, `types`, `limit` | Adds, drops, completed waiver acquisitions, roster moves and trade lifecycle events for one scoring week. Up to 20 returned records. Pending/failed waiver claims and bid amounts are excluded. |
| `evaluate_trade_proposal` | Two team IDs, each team's outgoing player IDs; optional `week` | A hypothetical exchange of 1–5 currently owned players per side. Compares optimal projected lineups and roster depth before/after without changing rosters. Supports the current week and up to three later weeks when projections exist. |
| `simulate_trade_impact` | None | Counterfactual before/after analysis of the latest completed trade in the report. Uses current rosters and current report-week projections; later roster moves can make the reconstruction unavailable. |

### Trade status and visibility are part of the evidence

The bot reads three distinct ESPN sources:

1. **`mPendingTransactions`** contains offers visible to the configured ESPN account, including accepted trades awaiting review or processing. It may also contain private waiver information, which the bot discards. ESPN can omit the collection altogether. That is reported as `not_exposed`, not as proof that nobody has an outstanding offer.
2. **`mTransactions2`** records lifecycle events. A `TRADE_ACCEPT` event with `EXECUTED` status means the acceptance event executed; that alone does not prove the players changed teams. ESPN can omit player legs for trades involving other managers. The bot preserves the missing-data limitation instead of reconstructing a trade from today's rosters.
3. **`kona_league_communication`**, filtered to `TRADED` messages (`messageTypeId` 244), supplies completed player transfers. The sending and receiving teams come from the message's `from` and `to` fields.

The model must distinguish **“if this offer goes through”**, **“accepted and awaiting processing”**, and **“completed”**. A hypothetical evaluation is not evidence that anyone proposed, accepted or completed the exchange. If a stale pending snapshot overlaps later completed activity, the completed source establishes the transfer; the earlier offer retains its original provenance.

The configured ESPN account may not see every manager's private proposals. Neither a successful read nor an empty result proves league-wide visibility. The bot does not request another manager's credentials or submit/accept/veto trades. No account/member IDs, cookies, private bid amounts, private transaction comments or pending waiver claims reach model output.

## Calculated analysis and league memory

| Tool | Useful inputs | Evidence and interpretation |
| --- | --- | --- |
| `find_available_replacements` | `team_id` | Available candidates and projected lineup improvements for one team. This is analysis, not an add/drop or waiver claim. |
| `get_workload_changes` | `player_ids` | Latest versus prior completed-game targets, carries and available snap-share changes. Does not turn missing participation data into zero. |
| `get_schedule_outlook` | `team_id` | A team's next four NFL/fantasy opponents and verified bye conflicts. Complements the longer full fantasy schedule and player-specific NFL schedule. |
| `simulate_playoff_odds` | None | Reproducible estimates based on completed scores and the remaining schedule, with supported-rule checks. A high probability is not a clinch. |
| `get_schedule_luck` | None | Completed weekly scores versus all other teams and actual opponents. Positive luck means more score-derived wins than all-play expected wins; it is not proof of managerial skill. |
| `get_lineup_efficiency` | `team_id` | Actual starters versus eligible hindsight-optimal lineups in completed historical weeks. It measures points left on the bench after outcomes are known, not what managers should have known beforehand. |
| `get_waiver_return` | `team_id` | Points actually started after dated waiver/free-agent pickups, using available transactions and historical lineups. Incomplete history limits conclusions. |
| `get_draft_value` | `team_id` | Draft order compared with completed scoring rank at the same position. Uses original drafting teams; this is a value proxy, not a causal grade. |
| `review_previous_predictions` | None | Saved commentary alongside subsequent completed matchup results. Quotes come from saved records, not invented memories. Subjective claims are not automatically graded. |
| `get_league_personality` | None | User-supplied nicknames, rivalries and running jokes. An empty file means no lore was supplied. Relationships and quotes must not be invented. |

## Request boundaries and deployment verification

The new ESPN adapters use fixed HTTPS hosts and application-selected views, strict argument schemas, finite page/record sizes, request deadlines, response byte limits and request-scoped caches. Model-provided URLs, arbitrary views, request headers and cookies are not accepted. Data is projected onto allowed output fields rather than forwarding raw ESPN payloads. The central research budget also limits tool rounds and total calls.

Unit tests cover pending/completed separation, transfer direction, unavailable/empty responses, private-field exclusion, hypothetical roster immutability, historical cutoffs, truncation, bounded requests and default packet integration. These fixtures do **not** establish that a particular private ESPN account exposes outstanding offers. Live private-view availability must be checked in the running environment; an unavailable source remains explicitly unavailable.

## Primary implementation references

- The maintained [`espn-api` football client](https://github.com/cwendt94/espn-api/blob/master/espn_api/football/league.py) shows league views, player-card/free-agent filters, the activity feed and `mTransactions2` requests. Its current source also documents that other owners' `TRADE_ACCEPT` records can omit player legs.
- The SDK's [`Activity` parser](https://github.com/cwendt94/espn-api/blob/master/espn_api/football/activity.py) maps message 244 to outgoing and incoming transfers. Its [`Transaction` parser](https://github.com/cwendt94/espn-api/blob/master/espn_api/football/transaction.py) keeps lifecycle status, dates and item directions separate.
- The SDK's [`Player` parser](https://github.com/cwendt94/espn-api/blob/master/espn_api/football/player.py) defines the player statistics, eligibility and ownership mappings, and its [request client](https://github.com/cwendt94/espn-api/blob/master/espn_api/requests/espn_requests.py) defines player-card filters and professional schedules. The new adapters retain bounded HTTP behavior rather than inheriting unbounded SDK calls.
- The SDK maintainers' [pending trade discussion](https://github.com/cwendt94/espn-api/issues/500) includes an observed `mPendingTransactions` response and confirms that accepted pending trades are separate from completed activity. That observation originated in basketball; the bot handles absent/unsupported football responses conservatively.
- The independent [`ffscrapr` ESPN transaction implementation](https://github.com/ffverse/ffscrapr/blob/main/R/espn_transactions.R) uses the same fixed fantasy reads host, communication view and trade direction fields.
- ESPN's [NFL scoreboard endpoint](https://site.api.espn.com/apis/site/v2/sports/football/nfl/scoreboard?dates=2025&seasontype=2&week=1&limit=100) supplies game IDs and state for subsequent [game-summary requests](https://site.api.espn.com/apis/site/v2/sports/football/nfl/summary?event=401772510). These are ESPN-owned data endpoints, not published API stability guarantees. Summary event IDs must first be discovered by the scoreboard tool in the same request.

ESPN can change undocumented views and field visibility. Extend these adapters only with a clear question to answer, bounded read behavior, explicit provenance, and tests for missing or contradictory data.
