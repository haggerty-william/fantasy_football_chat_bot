"""Conservative deterministic checks on commentary; not a semantic truth oracle."""
import json
import re
from copy import deepcopy
from decimal import Decimal, InvalidOperation


def _numbers(text):
    values=set()
    for token in re.findall(r'(?<![A-Za-z])[-+]?\d+(?:\.\d+)?',str(text)):
        try: values.add(abs(Decimal(token)))
        except InvalidOperation: pass
    return values


def _entries(context):
    entries={e['name']:deepcopy(e) for e in context.get('players',[])}
    for roster in context.get('rosters',[]):
        for row in roster['players']:
            pid,name,position,slot,points,projection,status,nfl_team=row
            entries.setdefault(name,{'name':name,'fantasy_team':roster['team'],'points':points,
                                     'projected_points':projection,'current_status':status,'slot':slot,
                                     'game':context.get('nfl_games',{}).get(nfl_team,{})})
    # Player facts in newly selected fantasy tools are as authoritative as the
    # initial snapshot. Keep weeks attached so an old score cannot validate a
    # claim about the current week (or vice versa). NFL scoreboards/summaries
    # deliberately do not enter this fantasy-points/ownership index.
    def week_facts(entry, week, row):
        if type(week) is not int or not 1 <= week <= 18:
            return
        target=entry.setdefault('_week_evidence',{}).setdefault(week,{})
        if 'points' in row: target['points']=row['points']
        if isinstance(row.get('fantasy_team'),str): target['fantasy_team']=row['fantasy_team']
        if 'slot' in row: target['slot']=row['slot']
        elif 'lineup_slot' in row: target['slot']=row['lineup_slot']
        stats=row.get('stats',row.get('breakdown'))
        if isinstance(stats,dict): target['stats']=dict(stats)

    for entry in entries.values():
        week_facts(entry,context.get('week'),entry)
        for row in entry.get('previous_weeks',[]):
            week_facts(entry,row.get('week'),row)

    def merge(row, week=None, current=False, owner=None):
        if not isinstance(row,dict) or not isinstance(row.get('name'),str): return
        name=row['name']
        entry=entries.setdefault(name,{'name':name})
        for key in ('id','fantasy_team'):
            if key in row: entry[key]=row[key]
        if owner is not None and 'fantasy_team' not in entry: entry['fantasy_team']=owner
        if current and not context.get('historical'):
            for key in ('current_status','game','slot'):
                if key in row: entry[key]=deepcopy(row[key])
        scoped_week=row.get('week',week)
        week_facts(entry,scoped_week,{**row,**({'fantasy_team':owner} if owner is not None else {})})
        for detail in row.get('weekly_stats',[]):
            if isinstance(detail,dict): week_facts(entry,detail.get('week'),detail)
        if scoped_week==context.get('week'):
            for key in ('points','stats'):
                if key in row: entry[key]=deepcopy(row[key])
    for evidence in context.get('tool_evidence',[]):
        result=evidence.get('result',{})
        if not isinstance(result,dict) or 'error' in result: continue
        name=evidence.get('tool')
        if name in ('get_player_season_details','get_free_agent_pool','search_players'):
            for row in result.get('players',[]): merge(row,current=True)
        elif name=='get_team_profile':
            for row in result.get('roster',[]): merge(row,current=True,owner=result.get('team'))
        elif name=='get_week_box_scores':
            for team in result.get('teams',[]):
                for row in team.get('players',[]): merge(row,week=result.get('week'),owner=team.get('team'))
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


def _team_names(context):
    names = {t['name'] for t in context.get('fantasy_teams', [])}
    names.update(r['team'] for r in context.get('rosters', []))
    names.update(t['team'] for t in context.get('league_history', {}).get('teams', []))
    return {name.replace('\u2019', "'") for name in names}


def _nfl_names(context):
    names=set()
    for evidence in context.get('tool_evidence',[]):
        result=evidence.get('result',{})
        if evidence.get('tool')=='get_nfl_scoreboard': games=result.get('games',[])
        elif evidence.get('tool')=='get_nfl_game_summary': games=[result.get('game',{})]
        else: continue
        for game in games:
            for team in game.get('teams',[]):
                names.update(team[key] for key in ('nfl_team','name') if isinstance(team.get(key),str) and team[key])
    return names


def _nfl_sentence(sentence, nfl_names, fantasy_names):
    has=lambda names:any(re.search(r'(?<!\w)'+re.escape(name)+r'(?!\w)',sentence,re.I) for name in names)
    return has(nfl_names) and not has(fantasy_names)


def _nfl_fantasy_score_issues(text, context):
    """Do not let NFL-score numbers validate direct fantasy score assertions."""
    nfl_names=_nfl_names(context)
    if not nfl_names: return []
    names=_team_names(context)
    facts={name:{} for name in names}
    def add(name,week,score):
        normalized=name.replace('\u2019', "'") if isinstance(name,str) else None
        if normalized in facts and type(week) is int and isinstance(score,(int,float)) and not isinstance(score,bool):
            facts[normalized].setdefault(week,set()).add(Decimal(str(score)))
    for matchup in context.get('matchups',[]):
        for side in matchup.get('sides',[]): add(side.get('team'),matchup.get('week'),side.get('score'))
    for team in context.get('league_history',{}).get('teams',[]):
        for row in team.get('performance',{}).get('recent_games',[]): add(team.get('team'),row.get('week'),row.get('points'))
    for evidence in context.get('tool_evidence',[]):
        if evidence.get('tool') not in ('get_week_matchups','get_fantasy_schedule'): continue
        for matchup in evidence.get('result',{}).get('matchups',[]):
            weeks=matchup.get('scoring_weeks',[])
            if len(weeks)==1:
                for side in matchup.get('teams',[]): add(side.get('team'),weeks[0],side.get('score'))
    issues=[]
    for sentence in _sentences(text,names|nfl_names):
        if (_nfl_sentence(sentence,nfl_names,names) and
                re.search(r'\b(?:scored|posted|finished with)\s+\d+(?:\.\d+)?\s+fantasy points\b',sentence,re.I)):
            issues.append('nfl_score_is_not_fantasy_points')
        weeks={int(w) for w in re.findall(r'\bweek\s+(\d+)\b',sentence,re.I)}
        if len(weeks)>1: continue
        for name,values in facts.items():
            claim=re.search(r'(?<!\w)'+re.escape(name)+r'\s+(?:scored|posted|finished with)\s+(\d+(?:\.\d+)?)\s+(?:fantasy\s+)?points\b',sentence,re.I)
            if not claim: continue
            allowed=values.get(next(iter(weeks)),set()) if weeks else set().union(*values.values()) if values else set()
            if allowed and Decimal(claim.group(1)) not in allowed: issues.append('fantasy_team_points_mismatch')
    return issues


def _sentences(text, team_names):
    """Punctuation belonging to a supplied team name is not a sentence end."""
    pattern = (r'(?<!\w)(?:' + '|'.join(re.escape(n) for n in sorted(team_names, key=len, reverse=True))
               + r')(?!\w)') if team_names else None
    spans = [m.span() for m in re.finditer(pattern, text, re.I)] if pattern else []
    start = 0
    for boundary in re.finditer(r'(?<=[.!?])\s+|\n+', text):
        if '\n' not in boundary.group() and any(a <= boundary.start() - 1 < b for a, b in spans):
            continue
        yield text[start:boundary.start()]
        start = boundary.end()
    yield text[start:]


def _unsupported_playoff_claim(sentence, report, context):
    """Current seeds and a method-note are not confirmed playoff outcomes."""
    pattern = (r'\b(?P<clinch>clinched|guaranteed (?:a )?playoff (?:spot|place|berth))\b|'
               r'\b(?P<elimination>eliminated|outside (?:the )?mathematical bounds|'
               r'out of playoff contention|(?:cannot|can\'t) make (?:the )?playoffs|'
               r'no (?:mathematical )?(?:chance|path) (?:of|to) (?:making |reaching )?(?:the )?playoffs)\b')
    names = _team_names(context)
    report = report.replace('\u2019', "'")
    previous_claim_end = 0
    for match in re.finditer(pattern, sentence, re.I):
        # Keep an intervening comma aside with its subject. A previous playoff
        # assertion starts a new clause, so its team cannot validate this one.
        prefix = re.split(r';|\b(?:but|while|whereas)\b',
                          sentence[previous_claim_end:match.start()], flags=re.I)[-1]
        previous_claim_end = match.end()
        # A comma may separate independent clauses, rather than an aside or
        # a list of joint subjects. Do not inherit another clause's "might".
        for comma in reversed(list(re.finditer(',', prefix))):
            before, after = prefix[:comma.start()], prefix[comma.end():]
            if (re.search(r'\b(?:has|have|is|are|was|were|could|might|would|may|will|won|lost)\b', before, re.I)
                    and any(re.search(r'(?<!\w)' + re.escape(n) + r'(?!\w)', after, re.I) for n in names)):
                prefix = after
                break
        if re.search(r"\b(?:no one|nobody|none|not|never|hasn't|haven't|isn't|aren't)\s+"
                     r'(?:(?:has|have|is|are|been|yet|already|mathematically)\s+)*$', prefix, re.I):
            continue
        if re.search(r'\b(?:if|could|might|would|may)\b', prefix, re.I):
            continue
        status = 'Clinched' if match.group('clinch') else 'Eliminated'
        subjects = [n for n in names if re.search(r'(?<!\w)' + re.escape(n) + r'(?!\w)', prefix, re.I)]
        # Only the report generator's explicit per-team record-bound status is
        # authoritative. Another team's status cannot validate this assertion.
        if not subjects or any(not re.search(
                r'^' + re.escape(name) + r': #[0-9]+ \| ' + status + r' by record bound\s*$',
                report, re.I | re.M) for name in subjects):
            return True
    return False


def check_commentary(text, report, context):
    identity_issues = ['unresolved_identity_reference'] if re.search(r'\[(?:team|manager):[^\]\n]*\]', text) else []
    if not context: return identity_issues
    from gamedaybot.espn.team_performance_checks import check_team_performance
    from gamedaybot.espn.transaction_checks import check_transaction_claims
    normalized=text.replace('\u2019', "'").replace('**','')
    # NFL clubs can influence opponents' scoring. The fantasy-management
    # defense-control guard applies only outside explicitly named NFL context.
    nfl_names,fantasy_names=_nfl_names(context),_team_names(context)
    fantasy_text='\n'.join(sentence for sentence in _sentences(normalized,fantasy_names|nfl_names)
                           if not _nfl_sentence(sentence,nfl_names,fantasy_names)) if nfl_names else text
    problems=identity_issues + check_team_performance(fantasy_text, context)
    problems.extend(_nfl_fantasy_score_issues(normalized,context))
    problems.extend(check_transaction_claims(text, context))
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
    sentences=_sentences(clean, _team_names(context))
    for sentence in sentences:
        lower=sentence.casefold()
        explicit_weeks={int(w) for w in re.findall(r'\bweek\s+(\d+)\b',lower)}
        stated_week=next(iter(explicit_weeks)) if len(explicit_weeks)==1 else None
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
            if (context.get('matchups') and stated_week in (None,context.get('week')) and e.get('slot') in ('BE','BN','IR')
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
                observed=e.get('_week_evidence',{})
                values=([observed.get(stated_week,{}).get('points')] if stated_week is not None else
                        [e.get('points'),*[row.get('points') for row in observed.values()]])
                allowed={Decimal(str(p)) for p in values if p is not None}
                if Decimal(scored.group(1)) not in allowed: problems.append('player_points_mismatch')
            if len(mentioned)==1:
                metric_fields={'targets':('receivingTargets','targets'),'carries':('rushingAttempts','carries'),
                               'receptions':('receivingReceptions','receptions'),
                               'passing yards':('passingYards','passing_yards'),
                               'rushing yards':('rushingYards','rushing_yards'),
                               'receiving yards':('receivingYards','receiving_yards'),
                               'offensive snaps':(None,'offense_snaps')}
                for metric,(espn_key,usage_key) in metric_fields.items():
                    claims=re.findall(r'(\d+(?:\.\d+)?)\s+'+metric+r'\b',lower)
                    if not claims: continue
                    values=[]
                    if stated_week in (None,context.get('week')) and espn_key:
                        values.append(e.get('stats',{}).get(espn_key))
                    if espn_key:
                        values.extend(row.get('stats',{}).get(espn_key)
                                      for w,row in e.get('_week_evidence',{}).items()
                                      if stated_week is None or w==stated_week)
                    values.extend(row.get(usage_key) for row in e.get('usage',{}).get('weeks',[])
                                  if stated_week is None or row['week']==stated_week)
                    allowed={Decimal(str(v)) for v in values if v is not None}
                    if any(Decimal(claim) not in allowed for claim in claims): problems.append('player_usage_mismatch')
                for roster in context.get('rosters',[]):
                    team=roster['team']
                    owner=(e.get('_week_evidence',{}).get(stated_week,{}).get('fantasy_team')
                           if stated_week is not None and stated_week!=context.get('week') else e.get('fantasy_team'))
                    if owner is None or team==owner: continue
                    if re.search(re.escape(team)+r"(?:'s)?\s+(?:QB|RB|WR|TE|player|starter)\s+"+subject,sentence,re.I):
                        problems.append('ownership_mismatch')
        if _unsupported_playoff_claim(sentence, report, context):
            problems.append('unsupported_playoff_claim')
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
