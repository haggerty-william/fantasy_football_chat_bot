"""Daily reports from ESPN's completed-trade activity feed."""

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
import hashlib
import json


def completed_trades(league, since_ms):
    """Stable IDs use player/team IDs, not mutable display names or action order."""
    found = {}
    for page in range(100):
        activities = league.recent_activity(size=25, msg_type='TRADED', offset=page * 25)
        older = False
        for activity in activities:
            timestamp = getattr(activity, 'date', 0)
            if timestamp < since_ms:
                older = True
                continue
            actions = [a for a in activity.actions if a[1] in ('TRADE_SENT', 'TRADE_RECEIVED')]
            if not actions:
                continue
            identity = sorted((str(getattr(t, 'team_id', getattr(t, 'team_name', 'unknown'))), action,
                               str(getattr(p, 'playerId', getattr(p, 'name', p)))) for t, action, p, *_ in actions)
            key = hashlib.sha256(json.dumps([timestamp, identity]).encode()).hexdigest()
            found[key] = {'id': key, 'date': timestamp, 'actions': actions}
        if older or len(activities) < 25:
            return sorted(found.values(), key=lambda trade: (trade['date'], trade['id']))
    raise RuntimeError('Trade feed exceeded pagination limit; scan not complete')


def format_trade(trade, timezone='America/New_York'):
    when = datetime.fromtimestamp(trade['date'] / 1000, ZoneInfo(timezone))
    lines = [f'Trade Report {when:%Y-%m-%d}:', f'Completed {when:%I:%M %p %Z}']
    received = [action for action in trade['actions'] if action[1] == 'TRADE_RECEIVED']
    if received:
        recipients = {}
        for team, _, player, *_ in received:
            recipients.setdefault(getattr(team, 'team_name', 'Unknown team'), []).append(
                getattr(player, 'name', 'Player #' + str(player)))
        for team, players in recipients.items():
            lines.append(f"{team} received {', '.join(players)}")
        return '\n'.join(lines)
    for team, action, player, *_ in trade['actions']:
        verb = 'sent' if action == 'TRADE_SENT' else 'received'
        lines.append(f"{getattr(team, 'team_name', 'Unknown team')} {verb} {getattr(player, 'name', 'Player #' + str(player))}")
    return '\n'.join(lines)


def get_trade_report(league, timezone='America/New_York', report_date=None):
    """Report a complete local calendar day (yesterday by default).

    Page through trade activity, rather than the default 25 recent league
    moves. The activity feed supplies both sides of player trades, which
    the released transaction API does not consistently expose.
    """
    zone = ZoneInfo(timezone)
    if report_date is None:
        report_date = datetime.now(zone).date() - timedelta(days=1)

    blocks = []
    seen = set()
    offset = 0
    page_size = 25
    while True:
        activities = league.recent_activity(
            size=page_size, msg_type='TRADED', offset=offset)
        if not activities:
            break
        reached_older_day = False
        for activity in activities:
            timestamp = getattr(activity, 'date', None)
            if timestamp is None:
                continue
            local_time = datetime.fromtimestamp(timestamp / 1000, zone)
            if local_time.date() < report_date:
                reached_older_day = True
                continue
            if local_time.date() != report_date:
                continue

            moves = []
            for team, action, player, *_ in activity.actions:
                if action not in ('TRADE_SENT', 'TRADE_RECEIVED'):
                    continue
                team_name = getattr(team, 'team_name', None) or 'Unknown team'
                player_name = getattr(player, 'name', None)
                if not player_name:
                    player_name = f'Player #{player}'
                verb = 'sent' if action == 'TRADE_SENT' else 'received'
                moves.append(f'{team_name} {verb} {player_name}')
            if not moves:
                continue
            key = (timestamp, tuple(moves))
            if key in seen:
                continue
            seen.add(key)
            blocks.append((timestamp, '\n'.join([
                f'Trade completed at {local_time:%I:%M %p %Z}', *moves])))

        # ESPN sorts the feed newest first. Finish the whole boundary page
        # before stopping so trades sharing a timestamp are retained.
        if reached_older_day or len(activities) < page_size:
            break
        offset += len(activities)

    if not blocks:
        return ''
    blocks.sort(key=lambda entry: entry[0])
    return f'Trade Report {report_date}:\n\n' + '\n\n'.join(
        block for _, block in blocks)
