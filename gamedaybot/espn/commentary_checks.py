"""Conservative deterministic checks on commentary; not a semantic truth oracle."""
import json
import re
from decimal import Decimal, InvalidOperation


def _numbers(text):
    values=set()
    for token in re.findall(r'(?<![A-Za-z])[-+]?\d+(?:\.\d+)?',str(text)):
        try: values.add(abs(Decimal(token)))
        except InvalidOperation: pass
    return values


def _entries(context):
    entries={e['name']:dict(e) for e in context.get('players',[])}
    for roster in context.get('rosters',[]):
        for row in roster['players']:
            pid,name,position,slot,points,projection,status,nfl_team=row
            entries.setdefault(name,{'name':name,'fantasy_team':roster['team'],'points':points,
                                     'projected_points':projection,'current_status':status,'slot':slot,
                                     'game':context.get('nfl_games',{}).get(nfl_team,{})})
    return entries


def _trade_subjects(context):
    """Resolve manager aliases only when they identify one supplied manager/team."""
    owners={}
    subjects={side['team']:{side['team'].replace('\u2019', "'")}
              for side in context.get('trade_sides',[])}
    for team in context.get('fantasy_teams',[]):
        for manager in team.get('managers',[]):
            if not isinstance(manager,str) or not manager.strip(): continue
            manager=' '.join(manager.replace('\u2019', "'").split())
            parts=manager.split()
            aliases={manager,parts[0]}
            if len(parts)>1:
                initial=f'{parts[0]} {parts[-1][0]}'
                aliases.update((initial,initial+'.'))
            for alias in aliases:
                owners.setdefault(alias.casefold(),set()).add((team['name'],manager.casefold()))
    for alias,matches in owners.items():
        if len(matches)!=1: continue
        team,_=next(iter(matches))
        # A manager alias that also names another team is ambiguous as a subject.
        if any(t['name'].casefold()==alias and t['name']!=team
               for t in context.get('fantasy_teams',[])):
            continue
        if team in subjects: subjects[team].add(alias)
    return {team:r'(?<!\w)(?:'+'|'.join(re.escape(s) for s in sorted(aliases,key=len,reverse=True))+r')'
            +r'(?:\s*\('+re.escape(team.replace('\u2019', "'"))+r'\))?'
            for team,aliases in subjects.items()}


def check_commentary(text, report, context):
    if not context: return []  # Legacy report-only calls retain their existing behavior.
    problems=[]
    corpus=report+'\n'+json.dumps(context,ensure_ascii=False)
    clean=re.sub(r'\[(?:F|N)\d+\]','',text).replace('\u2019', "'").replace('**','')
    if _numbers(clean)-_numbers(corpus): problems.append('unsupported_number')
    entries=_entries(context)
    names=list(entries)
    # Detect new person-like subjects, while permitting supplied fantasy-team names.
    for match in re.finditer(r"\b([A-Z][a-z'-]+(?: [A-Z][a-z'-]+){1,2})\s+(?:is|was|has|scored|posted|remains|will|could|might)\b",clean):
        subject=match.group(1)
        if set(subject.casefold().split()) & {'match','matchup','league','rivalry','showdown','grudge','bowl','battle','night','bench','playoff'}:
            continue
        if subject.split()[0] not in ('The','This','That','Your','Their','Our','Both','If','Meanwhile') and subject.casefold() not in corpus.casefold():
            problems.append('unsupported_named_subject')
    sentences=re.split(r'(?<=[.!?])\s+|\n+',clean)
    for sentence in sentences:
        lower=sentence.casefold()
        mentioned=[]
        for name,e in entries.items():
            alias=name.split()[-1]
            full=re.search(r'(?<!\w)'+re.escape(name)+r'(?!\w)',sentence,re.I)
            short=(len(alias)>3 and sum(n.split()[-1]==alias for n in names)==1
                   and re.search(r'(?<!\w)'+re.escape(alias)+r'(?!\w)',sentence,re.I))
            if full or short: mentioned.append(e)
        if context.get('historical') and re.search(r'\b(questionable|doubtful|inactive|healthy|injured|injury|injuries|cleared)\b',lower):
            problems.append('current_status_in_historical_recap')
        if mentioned and re.search(r'\b(healthy|fully fit|full strength|cleared to play|guaranteed to play)\b',lower):
            problems.append('unsupported_health_inference')
        # Football opinions are not objectively disproved by sample size. The
        # prompt requires uncertainty; only concrete contradictions block delivery.
        for e in mentioned:
            if (context.get('matchups') and e.get('slot') in ('BE','BN','IR')
                    and e.get('game',{}).get('state') in ('in','post')
                    and re.search(r'\b(boost|carrying|carried|fueled|fueling|benefits?|contributing|contribution|leaning on|bridge the gap|powered|powering)\b',lower)
                    and not re.search(r'\b(bench|benched|reserve|missed|unused|could|might|would|next week|future)\b',lower)):
                problems.append('bench_points_do_not_contribute')
            status=str(e.get('current_status','UNKNOWN')).upper()
            subject=re.escape(e['name'])
            # Direct status assertions only; mentioning an opponent's status does not change this player's status.
            assertion=re.search(subject+r"(?:\s*\([^)]*\))?\s+(?:is|remains|was listed|is listed|has been ruled)\s+(?:officially\s+)?(out|questionable|doubtful|inactive|active)\b",sentence,re.I)
            if assertion and assertion.group(1).upper()!=status:
                problems.append('status_mismatch')
            game=e.get('game',{})
            if (game.get('lineup_locked') or game.get('state') in ('in','post')) and re.search(r'\b(start him|start '+subject+r'|bench him|bench '+subject+r'|if .{0,40}sits|might sit|could sit|will sit)\b',sentence,re.I):
                problems.append('locked_player_advice')
            if game.get('completed') and re.search(r'\b(will|could|might|if)\b',lower) and re.search(r'\b(sits|inactive|misses|suit up)\b',lower):
                problems.append('finished_game_availability_forecast')
            scored=re.search(subject+r'\s+(?:scored|posted|finished with)\s+(\d+(?:\.\d+)?)\s+(?:fantasy\s+)?points',sentence,re.I)
            if scored:
                allowed={Decimal(str(p)) for p in [e.get('points'),*[r.get('points') for r in e.get('previous_weeks',[])]] if p is not None}
                if Decimal(scored.group(1)) not in allowed: problems.append('player_points_mismatch')
            if len(mentioned)==1:
                metric_fields={'targets':('receivingTargets','targets'),'carries':('rushingAttempts','carries'),
                               'receptions':('receivingReceptions','receptions'),
                               'passing yards':('passingYards','passing_yards'),
                               'rushing yards':('rushingYards','rushing_yards'),
                               'receiving yards':('receivingYards','receiving_yards'),
                               'offensive snaps':(None,'offense_snaps')}
                explicit_week=re.search(r'\bweek\s+(\d+)\b',lower)
                stated_week=int(explicit_week.group(1)) if explicit_week else None
                for metric,(espn_key,usage_key) in metric_fields.items():
                    claims=re.findall(r'(\d+(?:\.\d+)?)\s+'+metric+r'\b',lower)
                    if not claims: continue
                    values=[]
                    if stated_week in (None,context.get('week')) and espn_key:
                        values.append(e.get('stats',{}).get(espn_key))
                    values.extend(row.get(usage_key) for row in e.get('usage',{}).get('weeks',[])
                                  if stated_week is None or row['week']==stated_week)
                    allowed={Decimal(str(v)) for v in values if v is not None}
                    if any(Decimal(claim) not in allowed for claim in claims): problems.append('player_usage_mismatch')
                for roster in context.get('rosters',[]):
                    team=roster['team']
                    if team==e.get('fantasy_team'): continue
                    if re.search(re.escape(team)+r"(?:'s)?\s+(?:QB|RB|WR|TE|player|starter)\s+"+subject,sentence,re.I):
                        problems.append('ownership_mismatch')
        if not re.search(r'\b(clinched|eliminated)\b',report,re.I):
            for claim in re.finditer(r'\b(clinched|eliminated)\b',lower):
                prefix=lower[:claim.start()]
                negated=re.search(r"\b(?:no one|nobody|none|not|never|hasn't|haven't|isn't|aren't)\s+(?:(?:has|have|is|are|been|yet|already)\s+)*$",prefix)
                if not negated: problems.append('unsupported_playoff_claim')
    # Match before sentence splitting: the period in "Alex M." is not a sentence end.
    subjects=_trade_subjects(context)
    next_subject=r'\band\s+(?:'+'|'.join(subjects.values())+r')\s+(?:received|receives|gets|got|acquired|acquires|sent|sends|gave up|gives up|traded away)\b'
    for team,subject in subjects.items():
        for verb,key in (('received|receives|gets|got|acquired|acquires','received'),('sent|sends|gave up|gives up|traded away','sent')):
            for assertion in re.finditer(subject+r'\s+(?:'+verb+r')\s+([^.!?\n]+)',clean,re.I):
                recipients=re.split(r'\b(?:in exchange for|for|from|while|whereas|but|who|whose|which)\b|;|\band\s+(?:sent|sends|gave|gives)\b|'+next_subject,
                                    assertion.group(1),maxsplit=1,flags=re.I)[0]
                allowed={p for other in context.get('trade_sides',[]) if other['team']==team for p in other[key]}
                for name in names:
                    if name.casefold() in recipients.casefold() and name not in allowed:
                        problems.append('trade_direction_mismatch')
    return sorted(set(problems))


def fallback_highlights(context):
    lines=[]
    for item in context.get('highlights',[])[:2]:
        phase='Finished' if item['final'] else 'Currently'
        lines.append(f"{item['player']} ({item['team']}): {phase.lower()} at {item['points']:.2f} points, {item['vs_projection']:+.2f} versus the supplied projection.")
    if not lines:
        for p in context.get('players',[]):
            trend=p.get('trend',{})
            if trend.get('sample_games',0)>0:
                lines.append(f"{p['name']}: {trend['average_points']:.2f} points per game across {trend['sample_games']} verified recent games. That sample alone does not establish future performance.")
            if len(lines)==2: break
    return '\n\n'.join(lines)
