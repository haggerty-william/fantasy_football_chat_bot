"""Consistent team/manager labels for human-readable league reports."""


def _manager_names(team):
    def clean(value):
        return ' '.join(value.split()) if isinstance(value, str) else ''

    result = []
    for owner in getattr(team, 'owners', []) or []:
        if not isinstance(owner, dict):
            continue
        name = ' '.join(part for part in (clean(owner.get('firstName')), clean(owner.get('lastName'))) if part)
        name = name or clean(owner.get('displayName'))
        if name and name.casefold() not in {n.casefold() for n in result}:
            result.append(name)
    return result


def team_label(team, teams):
    """Append compact manager names, disambiguated across the entire league."""
    name = getattr(team, 'team_name', '')
    managers = _manager_names(team)
    if not managers:
        return name
    names = {n.casefold(): n for candidate in [*teams, team] for n in _manager_names(candidate)}

    def label(manager):
        parts = manager.split()
        matching = [n for n in names.values() if n.split()[0].casefold() == parts[0].casefold()]
        if len(matching) == 1 or len(parts) == 1:
            return parts[0]
        initial = parts[-1][0]
        if sum(len(n.split()) > 1 and n.split()[-1][0].casefold() == initial.casefold()
               for n in matching) == 1:
            return f'{parts[0]} {initial}.'
        return manager

    return f"{name} ({', '.join(label(manager) for manager in managers)})"
