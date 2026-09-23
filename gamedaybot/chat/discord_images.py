"""Upload verified PNGs rather than asking Discord to load ESPN SVG/private URLs."""

from concurrent.futures import ThreadPoolExecutor
from io import BytesIO
import os
import re
from threading import Lock
import time
from urllib.parse import urlsplit
from xml.etree import ElementTree

from PIL import Image, ImageDraw, ImageFont
import requests
import resvg_py

from .discord_format import build_payloads, STYLES, team_matches, logo_url, matchup_rows
from .team_labels import team_label

_cache = {}
_lock = Lock()
BOARD_WIDTH = 1320


def _draw_label(draw, label, position, width, size=29):
    font = ImageFont.load_default(size=size)
    while draw.textlength(label, font=font) > width and size > 24:
        size -= 1
        font = ImageFont.load_default(size=size)
    shown = label
    while draw.textlength(shown, font=font) > width:
        shown = shown[:-2].rstrip() + '…'
    draw.text(position, shown, font=font, fill='#f5f7fa')


def _draw_logo(canvas, draw, name, data, position, size=46):
    x, y = position
    if data:
        icon = Image.open(BytesIO(data)).convert('RGBA')
        icon.thumbnail((size, size))
        canvas.paste(icon, (x, y), icon)
    else:
        draw.rounded_rectangle((x, y, x + size, y + size), radius=10, fill='#456286')
        initials = ''.join(word[0] for word in name.split()[:2]).upper()
        draw.text((x + 6, y + 12), initials, font=ImageFont.load_default(size=18), fill='white')


def fetch_logo(url):
    if not url:
        return None
    try:
        parsed = urlsplit(url)
        safe = parsed.scheme == 'https' and not parsed.username and not parsed.password and parsed.port in (None, 443)
    except ValueError:
        return None
    if not safe:
        return None
    cookies = {}
    if parsed.hostname == 'mystique-api.fantasy.espn.com':
        # Use the existing ESPN login only for this exact ESPN image resource.
        if not re.fullmatch(r'/apis/v1/domains/lm/images/[0-9a-fA-F-]{36}', parsed.path) or parsed.query:
            return None
        cookies = {key: os.environ[env] for key, env in (('espn_s2', 'ESPN_S2'), ('SWID', 'SWID'))
                   if os.environ.get(env)}
    elif parsed.hostname != 'g.espncdn.com':
        return None
    with _lock:
        cached = _cache.get(url)
        if cached and time.monotonic() < cached[0]:
            return cached[1]
    result = None
    try:
        started = time.monotonic()
        with requests.get(url, cookies=cookies, timeout=(2, 3), stream=True, allow_redirects=False) as response:
            if response.status_code != 200:
                raise ValueError('Logo unavailable')
            body = bytearray()
            for chunk in response.iter_content(16384):
                body.extend(chunk)
                if len(body) > 500_000 or time.monotonic() - started > 5:
                    raise ValueError('Logo limit')
            if 'svg' in response.headers.get('Content-Type', ''):
                # Reject SVG external resources before passing it to the renderer.
                if b'<!DOCTYPE' in body.upper() or b'<!ENTITY' in body.upper():
                    raise ValueError('Unsupported XML declarations')
                root = ElementTree.fromstring(body)
                for node in root.iter():
                    if node.tag.split('}')[-1] in ('image', 'use', 'script', 'foreignObject', 'style'):
                        raise ValueError('Unsupported SVG resource')
                    if any('href' in key or 'url(' in str(value).lower() and not re.search(r'url\(\s*#', str(value))
                           for key, value in node.attrib.items()):
                        raise ValueError('External SVG resource')
                body = resvg_py.svg_to_bytes(svg_string=bytes(body).decode('utf-8'), width=96, height=96,
                                             skip_system_fonts=True)
            with Image.open(BytesIO(body)) as image:
                if image.width * image.height > 4_000_000:
                    raise ValueError('Oversized logo')
                image = image.convert('RGBA')
                image.thumbnail((96, 96))
                output = BytesIO()
                image.save(output, format='PNG')
                result = output.getvalue()
    except Exception:
        # A logo is decoration and must never prevent delivering a report.
        result = cached[1] if cached else None
    with _lock:
        if len(_cache) >= 256:
            _cache.pop(next(iter(_cache)))
        _cache[url] = (time.monotonic() + (3600 if result else 300), result)
    return result


def standings_image(description, teams, images):
    rows = re.findall(r'^\s*(\d+):\s+\(([^)]+)\)\s+(.+?)\s*$', description, re.M)
    if not rows or len(rows) > 40:
        return None
    canvas = Image.new('RGB', (BOARD_WIDTH, 110 + len(rows) * 72), '#18212f')
    draw = ImageDraw.Draw(canvas)
    font = ImageFont.load_default(size=29)
    small = ImageFont.load_default(size=26)
    draw.text((24, 22), 'LEAGUE STANDINGS', font=font, fill='#f5f7fa')
    for x, label in ((1010, 'W-L-T'), (1116, 'Win%'), (1230, 'GB')):
        draw.text((x, 28), label, font=small, fill='#a9bdd4')
    for index, (rank, record, name) in enumerate(rows):
        metrics = re.search(r' \| Win% ([\d.]+%) \| GB (-?[\d.]+)$', name)
        win_pct, games_back = (metrics.group(1), metrics.group(2)) if metrics else ('—', '—')
        if metrics:
            name = name[:metrics.start()]
        y = 76 + index * 72
        draw.rounded_rectangle((14, y, BOARD_WIDTH - 14, y + 66), radius=8, fill='#243247' if index % 2 == 0 else '#1d293a')
        draw.text((27, y + 20), rank, font=small, fill='#9bc9ff')
        matches = team_matches(name, teams)
        data = images.get(logo_url(matches[0])) if len(matches) == 1 else None
        _draw_logo(canvas, draw, name, data, (73, y + 10))
        shown = team_label(matches[0], teams) if len(matches) == 1 else name
        _draw_label(draw, shown, (135, y + 18), 845)
        for x, value in ((1010, record), (1116, win_pct), (1230, games_back)):
            draw.text((x, y + 20), value, font=small, fill='#f5f7fa')
    output = BytesIO()
    canvas.save(output, format='PNG')
    return output.getvalue()


def matchups_image(boxes, images, final=False, week=None, teams=None):
    boxes = list(boxes)
    teams = list(teams or [getattr(box, side + '_team') for box in boxes
                           for side in ('home', 'away') if getattr(box, side + '_team', None)])
    rows = matchup_rows(boxes)
    if not rows or len(rows) > 20:
        return None
    canvas = Image.new('RGB', (BOARD_WIDTH, 104 + len(rows) * 168), '#18212f')
    draw = ImageDraw.Draw(canvas)
    font = ImageFont.load_default(size=29)
    small = ImageFont.load_default(size=25)
    heading = 'FINAL SCORES' if final else 'LEAGUE MATCHUPS'
    if week:
        heading += f' - WEEK {week}'
    draw.text((24, 20), heading, font=font, fill='#f5f7fa')
    draw.text((24, 57), 'Completed week' if final else 'Live scores | Projections are estimates', font=small, fill='#a9bdd4')
    for index, pair in enumerate(rows):
        y = 92 + index * 168
        draw.rounded_rectangle((14, y, BOARD_WIDTH - 14, y + 152), radius=8, fill='#243247')
        if not final:
            draw.text((915, y + 8), 'W-L-T', font=small, fill='#a9bdd4')
            draw.text((1150, y + 8), 'Projected', font=small, fill='#a9bdd4')
        draw.text((1030, y + 8), 'Final' if final else 'Score', font=small, fill='#a9bdd4')
        for row, team in enumerate(pair):
            top = y + 38 + row * 54
            data = images.get(team['logo'])
            _draw_logo(canvas, draw, team['name'], data, (28, top))
            matched = next((candidate for candidate in teams if candidate.team_name == team['name']), None)
            shown = team_label(matched, teams) if matched is not None else team['name']
            _draw_label(draw, shown, (90, top + 8), 800)
            if not final:
                draw.text((915, top + 9), team['record'], font=small, fill='#a9bdd4')
            draw.text((1030, top + 9), team['score'], font=small, fill='#f5f7fa')
            if not final:
                draw.text((1150, top + 9), team['projection'], font=small, fill='#9bc9ff')
    output = BytesIO()
    canvas.save(output, format='PNG')
    return output.getvalue()


def power_rankings_image(report, teams, images):
    rows = re.findall(r'^\s*([\d.]+)(\[[^\]]+\])?\s*\(\s*([\d.]+)\)\s*-\s*(.+?)\s*$', report, re.M)
    if not rows or len(rows) > 40:
        return None
    canvas = Image.new('RGB', (BOARD_WIDTH, 110 + len(rows) * 72), '#18212f')
    draw = ImageDraw.Draw(canvas)
    font = ImageFont.load_default(size=29)
    small = ImageFont.load_default(size=25)
    draw.text((24, 22), 'POWER RANKINGS', font=font, fill='#f5f7fa')
    for x, label in ((970, 'Power'), (1080, 'Playoffs'), (1200, 'Change')):
        draw.text((x, 28), label, font=small, fill='#a9bdd4')
    for index, (power, change, playoffs, abbreviation) in enumerate(rows):
        y = 76 + index * 72
        draw.rounded_rectangle((14, y, BOARD_WIDTH - 14, y + 66), radius=8,
                               fill='#243247' if index % 2 == 0 else '#1d293a')
        draw.text((27, y + 20), str(index + 1), font=small, fill='#9bc9ff')
        matches = [team for team in teams if getattr(team, 'team_abbrev', None) == abbreviation]
        team = matches[0] if len(matches) == 1 else None
        name = getattr(team, 'team_name', abbreviation)
        _draw_logo(canvas, draw, name, images.get(logo_url(team)), (73, y + 10))
        _draw_label(draw, team_label(team, teams) if team is not None else name, (135, y + 18), 810)
        draw.text((970, y + 20), power, font=small, fill='#f5f7fa')
        draw.text((1080, y + 20), playoffs + '%', font=small, fill='#f5f7fa')
        amount = re.search(r'([\d.]+)%', change or '')
        movement = amount.group(1) + '%' if amount else '—'
        color = '#a9bdd4'
        if change and '🟢' in change:
            movement, color = '+' + movement, '#80d9ae'
        elif change and '🔻' in change:
            movement, color = '-' + movement, '#ffacaa'
        draw.text((1200, y + 20), movement, font=small, fill=color)
    output = BytesIO()
    canvas.save(output, format='PNG')
    return output.getvalue()


def prepare_payloads(text, teams=None, include_images=True):
    teams = list(teams if teams is not None else getattr(text, 'teams', []))
    payloads = list(build_payloads(text, teams))
    if not include_images:
        for payload in payloads:
            for embed in payload['embeds']:
                embed.pop('thumbnail', None)
                if 'author' in embed:
                    embed['author'].pop('icon_url', None)
        return [(payload, []) for payload in payloads]
    urls = {value for team in teams if (value := logo_url(team))}
    # Small league assets are fetched concurrently once and cached across reports.
    with ThreadPoolExecutor(max_workers=4) as pool:
        images = dict(zip(sorted(urls), pool.map(fetch_logo, sorted(urls))))
    prepared = []
    for payload in payloads:
        files = []
        uploaded = {}
        def attach(data):
            name = f'image-{len(files)}.png'
            files.append((name, data))
            return 'attachment://' + name
        for embed in payload['embeds']:
            if (embed['title'].startswith((STYLES['Matchups'][0], STYLES['Final Score Update'][0]))
                    and getattr(text, 'matchups', None) is not None):
                try:
                    data = matchups_image(text.matchups, images, getattr(text, 'final_scores', False), getattr(text, 'week', None), teams)
                    if data and len(files) < 10:
                        embed['image'] = {'url': attach(data)}
                except Exception:
                    pass
            if embed['title'].startswith(STYLES['Current Standings'][0]):
                try:
                    data = standings_image(embed.get('description', ''), teams, images)
                    if data and len(files) < 10:
                        embed['image'] = {'url': attach(data)}
                except Exception:
                    pass
            if embed['title'] == STYLES['Power Rankings (Playoff %)'][0]:
                try:
                    data = power_rankings_image(text, teams, images)
                    if data and len(files) < 10:
                        embed['image'] = {'url': attach(data)}
                except Exception:
                    pass
            for field, key in (('author', 'icon_url'), ('thumbnail', 'url')):
                if field not in embed or key not in embed[field]:
                    continue
                url = embed[field][key]
                data = images.get(url)
                if data and (url in uploaded or len(files) < 10):
                    if url not in uploaded:
                        uploaded[url] = attach(data)
                    embed[field][key] = uploaded[url]
                else:
                    embed[field].pop(key)
                    if field == 'thumbnail':
                        embed.pop(field)
            if 'image' in embed:
                # Keep a timestamp, not a second copy of the pictured table.
                description = embed.pop('description', '')
                fetched = next((line for line in description.splitlines() if line.startswith('Fetched from ESPN ')), None)
                if fetched:
                    embed['footer']['text'] += ' | ' + fetched
        prepared.append((payload, files))
    return prepared
