"""Fresh Discord reports and user-requested preferences/picks; no broadcasts."""

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from espn_api.football import League

from gamedaybot.espn import functionality as espn
from gamedaybot.espn.analysis import generate_analysis
from gamedaybot.chat.discord_format import TeamReport
from gamedaybot.espn.env_vars import get_env_vars
from gamedaybot.utils.util import has_sendable_content, NO_MATCHUP_DATA
from gamedaybot.espn.trades import completed_trades, format_trade


class ReportInputError(ValueError):
    """A safe, user-facing explanation for an invalid command argument."""


def command_report(command, team=None, week=None, days=7, user=None, name=None, enabled=True):
    data = get_env_vars(require_chat=False)
    if command == 'alerts' and not enabled and user is not None:
        # Unsubscribing must work even while ESPN is unavailable.
        from types import SimpleNamespace
        from gamedaybot.espn.team_alerts import subscribe
        league = SimpleNamespace(league_id=data['league_id'], year=data['year'])
        return TeamReport(subscribe(league, user, enabled=False), [])
    auth = {}
    if data['swid'] != '{1}' and data['espn_s2'] != '1':
        auth = {'swid': data['swid'], 'espn_s2': data['espn_s2']}
    league = League(league_id=data['league_id'], year=data['year'], **auth)
    stamp = datetime.now(ZoneInfo(data['my_timezone'])).strftime('%b %d, %Y · %I:%M %p %Z')
    if command in ('rivalry', 'monday', 'awards', 'playoffs', 'tradefollowup', 'pickem', 'alerts'):
        from gamedaybot.espn import community
        boxes = espn.fetch_box_scores(league) if command in ('rivalry', 'monday', 'pickem') else []
        selected = None
        if team:
            query = team.strip().casefold()
            matches = [t for t in league.teams if query in (t.team_name.casefold(), t.team_abbrev.casefold())]
            matches = matches or [t for t in league.teams if query in t.team_name.casefold()]
            if len(matches) != 1:
                raise ReportInputError('Use one exact team name or abbreviation.')
            selected = matches[0]
        if command == 'rivalry':
            text = community.preview(league, boxes)
        elif command == 'monday':
            text = community.monday_watch(league, boxes, community.nfl_games(league)) or 'Monday night watch\nNo matchups with starters still to play.'
        elif command == 'awards':
            week = min(league.scoringPeriodId-1, league.finalScoringPeriod) if week is None else week
            if not 1 <= week < league.scoringPeriodId or week > league.finalScoringPeriod:
                raise ReportInputError('Choose a completed scoring week.')
            text = community.awards(league, espn.fetch_box_scores(league, week=week), week) or 'No completed matchup data.'
        elif command == 'playoffs':
            text = community.playoff_picture(league)
        elif command == 'tradefollowup':
            from gamedaybot.espn.trade_followups import followups
            reports = followups(league)
            text = '\n\n'.join(t for _,t in reports[-3:]) or 'Trade follow-up\nNo trades with two full completed weeks in the last six weeks.'
        elif command == 'pickem':
            if user is None:
                raise ReportInputError('Use /pickem in Discord to save or view your picks.')
            try:
                games = community.nfl_games(league) if selected else None
            except Exception:
                games = None
            text = community.pickem(league, boxes, user, name, selected, games)
        else:
            from gamedaybot.espn.team_alerts import subscribe
            if user is None or (enabled and selected is None):
                raise ReportInputError('Choose your team, or use enabled:false to unsubscribe.')
            text = subscribe(league, user, selected, enabled)
        if command == 'rivalry' and 'Forecast:' in text:
            analysis = generate_analysis(text, 'get_rivalry', timezone=data['my_timezone'], week=league.scoringPeriodId,
                                         league=league, box_scores=boxes)
            if analysis: text += '\n\n' + analysis
    elif command == 'trades':
        if not isinstance(days, int) or not 1 <= days <= 30:
            raise ReportInputError('Choose between 1 and 30 days.')
        since = int((datetime.now(ZoneInfo(data['my_timezone'])) - timedelta(days=days)).timestamp()*1000)
        trades = completed_trades(league, since)
        if not trades:
            return f'No completed trades in the last {days} days.'
        recent = trades[-5:]
        text = '\n\n'.join(format_trade(trade, data['my_timezone']) for trade in recent)
        analysis = generate_analysis(text, 'get_trade_report', timezone=data['my_timezone'],
                                     week=league.scoringPeriodId, league=league,
                                     trade_actions=[(*a[:4], datetime.fromtimestamp(trade['date']/1000,
                                                                                   ZoneInfo(data['my_timezone'])).isoformat())
                                                    for trade in recent for a in trade['actions']])
        if len(trades) > 5:
            text += f'\n\nShowing the latest 5 of {len(trades)} trades in this window.'
        if analysis:
            text += '\n\n' + analysis
    elif command == 'standings':
        from gamedaybot.espn.community import playoff_picture
        text = espn.get_standings(league) + '\n\n' + playoff_picture(league, compact=True)
        analysis = generate_analysis(text, 'get_standings', timezone=data['my_timezone'],
                                     week=league.scoringPeriodId, league=league)
        if analysis:
            text += '\n\n' + analysis
    elif command == 'matchup':
        boxes = espn.fetch_box_scores(league)
        if team:
            query = team.strip().casefold()
            exact = [t for t in league.teams if query in (t.team_name.casefold(), t.team_abbrev.casefold())]
            matches = exact or [t for t in league.teams if query in t.team_name.casefold()]
            if not query or not matches:
                raise ReportInputError('No matching team. Use its ESPN team name or abbreviation.')
            if len(matches) != 1:
                raise ReportInputError('That matches multiple teams. Use the full team name or abbreviation.')
            selected = matches[0].team_id
            boxes = [b for b in boxes if selected in (
                getattr(b.home_team, 'team_id', None), getattr(b.away_team, 'team_id', None))]
        text = espn.get_matchups(league, box_scores=boxes)
        if text == NO_MATCHUP_DATA:
            return 'No matchup data is available for that team or week (it may be a bye).'
        text += '\n\n' + espn.get_scoreboard_short(league, box_scores=boxes)
        text += '\n\n' + espn.get_projected_scoreboard(league, box_scores=boxes)
        from gamedaybot.espn.community import preview
        text += '\n\n' + preview(league, boxes)
        analysis = generate_analysis(text, 'get_matchups', timezone=data['my_timezone'],
                                     week=league.scoringPeriodId, league=league, box_scores=boxes)
        if analysis:
            text += '\n\n' + analysis
    elif command == 'recap':
        period = league.scoringPeriodId
        final_period = league.finalScoringPeriod
        if week is None:
            week = min(period - 1, final_period)
        if week < 1:
            return 'No completed week yet. Use /matchup for current scores, or /recap week:1 for a live recap.'
        if week > min(period, final_period):
            raise ReportInputError('That week has not started or is outside this season.')
        boxes = espn.fetch_box_scores(league, week=week)
        text = espn.get_scoreboard_short(league, week=week, box_scores=boxes)
        if text == NO_MATCHUP_DATA:
            return 'No matchup data is available for that week.'
        finished = week < period
        if finished:
            text = 'Final ' + text
            from gamedaybot.espn.community import awards
            trophies = awards(league, boxes, week)
            if has_sendable_content(trophies):
                text += '\n\n' + trophies
        else:
            text += '\n\n' + espn.get_projected_scoreboard(league, week=week, box_scores=boxes)
        text = f'Week {week} · {"Completed" if finished else "In progress"}\n\n' + text
        analysis = generate_analysis(text, 'get_final' if finished else 'get_scoreboard_short',
                                     timezone=data['my_timezone'], week=week, league=league, box_scores=boxes)
        if analysis:
            text += '\n\n' + analysis
    else:
        raise ReportInputError('Unknown report command.')
    return TeamReport(text + '\n\n' + 'Fetched from ESPN ' + stamp, league.teams,
                      matchups=boxes if command in ('matchup', 'recap') else None,
                      final_scores=command == 'recap' and week < league.scoringPeriodId,
                      week=week if command == 'recap' else league.scoringPeriodId)
