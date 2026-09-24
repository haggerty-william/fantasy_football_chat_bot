"""Discord presentation, independent of the shared plain-text reports."""

import re
from urllib.parse import urlsplit
from .team_labels import team_label
from gamedaybot.commentator_names import ANALYST_NAME, RESPONDER_NAME


class TeamReport(str):
    """Plain report text with optional presentation metadata from the same snapshot."""

    def __new__(cls, text, teams, matchups=None, final_scores=False, week=None):
        result = super().__new__(cls, text)
        result.teams = teams
        result.matchups = matchups
        result.final_scores = final_scores
        result.week = week
        return result


def matchup_rows(boxes):
    from gamedaybot.espn.functionality import get_projected_total
    rows = []
    for box in boxes:
        if not getattr(box, 'away_team', None) or not getattr(box, 'home_team', None):
            continue
        pair = []
        for side in ('home', 'away'):
            team = getattr(box, side + '_team')
            lineup = getattr(box, side + '_lineup', None)
            score = getattr(box, side + '_score', None)
            projection = get_projected_total(lineup) if lineup is not None else None
            pair.append({'name': team.team_name, 'logo': logo_url(team),
                         'record': f"{getattr(team, 'wins', 0)}-{getattr(team, 'losses', 0)}-{getattr(team, 'ties', 0)}",
                         'score': f'{score:.2f}' if isinstance(score, (int, float)) else '—',
                         'projection': f'{projection:.2f}' if isinstance(projection, (int, float)) else '—'})
        rows.append(pair)
    return rows


def team_matches(text, teams):
    """Find unambiguous team labels, preferring full names over abbreviations."""
    candidates = []
    for team in teams:
        for attr in ('team_name', 'team_abbrev'):
            label = getattr(team, attr, None)
            if not isinstance(label, str) or not label.strip():
                continue
            for match in re.finditer(r'(?<!\w)' + re.escape(label) + r'(?!\w)', text, re.I):
                candidates.append((match.start(), match.end(), team))
    selected = []
    for start, end, team in sorted(candidates, key=lambda x: (-(x[1] - x[0]), x[0])):
        if any(a == start and b == end and t is not team for a, b, t in candidates):
            continue
        if not any(start < b and end > a for a, b, _ in selected):
            selected.append((start, end, team))
    found = []
    for _, _, team in sorted(selected, key=lambda x: x[0]):
        if not any(team is t for t in found):
            found.append(team)
    return found


def logo_url(team):
    value = getattr(team, 'logo_url', '')
    if not isinstance(value, str):
        return None
    try:
        parsed = urlsplit(value)
        if parsed.scheme in ('https', 'http') and parsed.hostname and not parsed.username and not parsed.password:
            return value
    except ValueError:
        pass
    return None


def team_sections(title, table, lines, teams):
    # One card per subject, not one per team/award. Only decorate unambiguous
    # single-team sections; images already label teams in league-wide boards.
    matched = team_matches('\n'.join(lines), teams) if teams else []
    if title in (STYLES['AI Analysis'][0], STYLES['AI Hot Take'][0], STYLES['Current Standings'][0], STYLES['Matchups'][0]):
        matched = []
    yield lines, matched if len(matched) == 1 else []


# Source heading, display heading, accent color, preserve numeric columns.
STYLES = {
    'Data Highlights': ('Verified data highlights', 0x3498DB, False),
    'Rivalry preview': ('🔥 Rivalry of the week', 0xE67E22, False),
    'Monday night watch': ('🏈 Monday night watch', 0xE67E22, False),
    'Neighborhood awards': ('🏆 Neighborhood awards', 0xF1C40F, False),
    'Playoff picture': ('🎟️ Playoff picture', 0x3498DB, False),
    'Trade follow-up': ('🧾 Trade receipts', 0x1ABC9C, False),
    'Pick’em': ('🎯 Weekly pick’em', 0x9B59B6, False),
    'Team alerts': ('🔔 Team alerts', 0xE67E22, False),
    'Research Sources': ('📚 Research sources', 0x607D8B, False),
    'AI Analysis': (ANALYST_NAME, 0x8E44AD, False),
    'AI Hot Take': (RESPONDER_NAME, 0xE67E22, False),
    'Score Update': ('🏈 Scoreboard', 0x3498DB, True),
    'Final Score Update': ('🏁 Final scores', 0x2ECC71, True),
    'Approximate Projected Scores': ('🔮 Projected scores', 0x9B59B6, True),
    'Projected Close Scores': ('🔥 Monday night watch', 0xE67E22, True),
    'Current Standings': ('📊 League standings', 0x3498DB, True),
    'Power Rankings (Playoff %)': ('📈 Power rankings · Playoff %', 0x9B59B6, True),
    'Matchups': ('🏈 This week’s matchups', 0x3498DB, False),
    'Starting Players to Monitor': ('🚑 Lineup watch', 0xE67E22, False),
    'Trophies of the week:': ('🏆 Weekly awards', 0xF1C40F, False),
}


def style_for(line):
    if line in STYLES:
        return STYLES[line]
    for prefix, title, color in (
        ('Trade Report ', '🔁 Trade report', 0x1ABC9C),
        ('Waiver Report ', '📋 Waiver wire', 0x3498DB),
    ):
        if re.fullmatch(re.escape(prefix) + r'\d{4}-\d{2}-\d{2}:', line):
            return (f'{title} · {line[len(prefix):-1]}', color, False)
    return None


def escape(text):
    return re.sub(r'([\\`*_~|<>])', r'\\\1', text)


def label_team_names(text, teams):
    """Decorate deterministic report labels without rewriting AI prose."""
    labels = {}
    for team in teams:
        name = getattr(team, 'team_name', '')
        if name and sum(getattr(t, 'team_name', '').casefold() == name.casefold() for t in teams) == 1:
            labels[name] = team_label(team, teams)
    if not labels:
        return text
    pattern = re.compile(r'(?<!\w)(?:' + '|'.join(re.escape(n) for n in sorted(labels, key=len, reverse=True)) + r')(?!\w)')
    def replace(match):
        name = match.group()
        suffix = labels[name][len(name):]
        return name if suffix and text[match.end():].startswith(suffix) else labels[name]
    return pattern.sub(replace, text)


def format_line(line):
    clean = escape(line)
    if ': Power ' in line:
        team, details = line.split(': Power ', 1)
        return f'**{escape(team)}**\nPower {escape(details)}\n'
    if line.startswith('Trade completed at ') or line.endswith(':'):
        return f'**{clean}**'
    # Trophy labels begin and end with the same emoji.
    words = line.split()
    if len(words) > 2 and words[0] == words[-1] and not words[0].isascii():
        return f'\n**{clean}**'
    if ' received ' in line:
        team, player = line.split(' received ', 1)
        return f'↳ **{escape(team)}** received **{escape(player)}**'
    if ' sent ' in line:
        return f'↗ {clean}'
    if line.startswith('ADDED '):
        return f'＋ {clean[6:]}'
    if line.startswith('DROPPED '):
        return f'− {clean[8:]}'
    return clean


def format_analysis_line(line):
    # Preserve generated ESPN citations while escaping the surrounding prose.
    parts = re.split(r'(\[ESPN\]\(https://[^\s)]+\))', line)
    formatted = []
    for part in parts:
        match = re.fullmatch(r'\[ESPN\]\((https://[^\s)]+)\)', part)
        if match:
            parsed = urlsplit(match.group(1))
            if (parsed.hostname == 'espn.com' or (parsed.hostname or '').endswith('.espn.com')) and not parsed.username and not parsed.password:
                formatted.append(part)
                continue
        formatted.append(format_line(part))
    return ''.join(formatted)


def chunks(text, limit=3800):
    """Prefer paragraph/line boundaries and never discard report text."""
    while len(text) > limit:
        boundary = text.rfind('\n\n', 0, limit + 1)
        if boundary <= 0:
            boundary = text.rfind('\n', 0, limit + 1)
        if boundary <= 0:
            boundary = limit
        yield text[:boundary]
        text = text[boundary:]
    if text:
        yield text


def build_payloads(text, teams=None):
    """Yield webhook payloads within individual and aggregate embed limits."""
    teams = teams if teams is not None else getattr(text, 'teams', [])
    teams = list(teams or [])
    sections = []
    title, color, table = ('🏈 League update', 0x3498DB, False)
    lines = []
    for line in text.strip().splitlines():
        style = style_for(line.strip())
        if style:
            if lines:
                sections.append((title, color, table, lines))
            title, color, table = style
            lines = []
        else:
            lines.append(line)
    if lines or not sections:
        sections.append((title, color, table, lines))

    boxes = getattr(text, 'matchups', None)
    if boxes is not None:
        # Merge names, records, scores and projections from the same snapshot.
        final = getattr(text, 'final_scores', False)
        replaced = {STYLES[key][0] for key in ('Matchups', 'Score Update', 'Final Score Update', 'Approximate Projected Scores')}
        rows = matchup_rows(boxes)
        summary = []
        for home, away in rows:
            names = f"{home['name']} vs {away['name']}" if final else f"{home['name']} ({home['record']}) vs {away['name']} ({away['record']})"
            scores = f"Score: {home['score']} – {away['score']}"
            if not final:
                scores += f" · Projected: {home['projection']} – {away['projection']}"
            summary.extend([names, scores, ''])
        summary.extend(line for title, _, _, lines in sections if title in replaced
                       for line in lines if line.startswith('Fetched from ESPN '))
        board_title = STYLES['Final Score Update' if final else 'Matchups'][0]
        if getattr(text, 'week', None):
            board_title += f' · Week {text.week}'
        remaining = [s for s in sections if s[0] not in replaced]
        remaining = [s for s in remaining if not all(not line.strip() or re.fullmatch(r'Week \d+ · (Completed|In progress)', line) for line in s[3])]
        sections = [(board_title, 0x2ECC71 if final else 0x3498DB, False, summary)] + remaining

    embeds = []
    decorated = [(title, color, table, group, matched)
                 for title, color, table, lines in sections
                 for group, matched in team_sections(title, table, lines, teams)]
    for title, color, table, lines, matched in decorated:
        score_titles = {STYLES[key][0] for key in ('Score Update', 'Final Score Update', 'Approximate Projected Scores', 'Projected Close Scores')}
        if title in score_titles and teams:
            readable = []
            for line in lines:
                found = re.fullmatch(r'\s*(.+?)\s+(-?\d+(?:\.\d+)?)\s+-\s+(-?\d+(?:\.\d+)?)\s+(.+?)\s*', line)
                if found:
                    home, home_score, away_score, away = found.groups()
                    for abbreviation, score in ((home, home_score), (away, away_score)):
                        team = next((t for t in teams if t.team_abbrev == abbreviation), None)
                        readable.append(f'{getattr(team, "team_name", abbreviation)}: {score}')
                    readable.append('')
                else:
                    readable.append(line)
            lines, table = readable, False
        if title == STYLES['Power Rankings (Playoff %)'][0] and teams:
            readable = []
            for line in lines:
                found = re.fullmatch(r'\s*([\d.]+)(\[[^\]]+\])?\s*\(\s*([\d.]+)\) - (.+)', line)
                if found:
                    score, change, playoff, abbreviation = found.groups()
                    team = next((t for t in teams if t.team_abbrev == abbreviation), None)
                    readable.append(f"{getattr(team, 'team_name', abbreviation)}: Power {score} | Playoffs {playoff}%" + (f' | Change {change[1:-1]}' if change else ''))
                else:
                    readable.append(line)
            lines, table = readable, False
        if title not in (STYLES['AI Analysis'][0], STYLES['AI Hot Take'][0]):
            lines = [label_team_names(line, teams) for line in lines]
        if table:
            # Keep team names from closing a numeric table's code fence.
            body = '\n'.join(lines).strip().replace('`', 'ˋ')
        elif title in (STYLES['AI Analysis'][0], STYLES['AI Hot Take'][0]):
            body = '\n'.join(format_analysis_line(line) for line in lines).strip()
        else:
            names = {team_label(team, teams) for team in teams}
            rendered = []
            for line in lines:
                name = next((name for name in names if name and line.startswith(name + ': ') and ': Power ' not in line), None)
                if name:
                    rendered.append(f'**{escape(name)}** — {escape(line[len(name)+2:])}')
                elif ' vs ' in line and len(team_matches(line, teams)) == 2:
                    rendered.append(f'**{escape(line)}**')
                else:
                    rendered.append(f'**{escape(line.strip())}**' if line.strip() in names and line.strip() else format_line(line))
            body = '\n'.join(rendered).strip()
        parts = list(chunks(body)) or ['']
        for index, part in enumerate(parts):
            suffix = f' · {index + 1}/{len(parts)}' if len(parts) > 1 else ''
            embed = {
                'title': title + suffix,
                'color': color,
                'footer': {'text': 'GameDayBot • AI commentary' if title in (STYLES['AI Analysis'][0], STYLES['AI Hot Take'][0])
                           else 'GameDayBot • ESPN Fantasy'},
            }
            if part.strip():
                embed['description'] = f'```\n{part}\n```' if table else part
            with_logos = [team for team in matched if logo_url(team)]
            if with_logos:
                primary = with_logos[0]
                embed['author'] = {'name': str(primary.team_name)[:256], 'icon_url': logo_url(primary)}
                if len(with_logos) == 2:
                    embed['thumbnail'] = {'url': logo_url(with_logos[1])}
                    embed['footer']['text'] += ' • Also shown: ' + str(with_logos[1].team_name)[:256]
            speaker = title if title in (ANALYST_NAME, RESPONDER_NAME) else None
            embeds.append((embed, speaker))

    batch, length, previous_speaker = [], 0, None
    for embed, speaker in embeds:
        size = (len(embed['title']) + len(embed.get('description', '')) + len(embed['footer']['text'])
                + len(embed.get('author', {}).get('name', '')))
        # Each speaker gets a separate post, in report -> analyst -> response order.
        # Continuation cards for the same speaker can share a post within limits.
        if batch and (length + size > 5800 or len(batch) == 10 or speaker != previous_speaker):
            yield {'embeds': batch, 'allowed_mentions': {'parse': []}}
            batch, length = [], 0
        batch.append(embed)
        length += size
        previous_speaker = speaker
    if batch:
        yield {'embeds': batch, 'allowed_mentions': {'parse': []}}
