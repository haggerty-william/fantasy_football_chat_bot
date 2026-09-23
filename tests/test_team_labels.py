from types import SimpleNamespace as Obj

from gamedaybot.chat.team_labels import team_label


def team(name, *managers):
    return Obj(team_name=name, owners=[dict(zip(('firstName', 'lastName'), manager.split(' ', 1)))
                                       for manager in managers])


def test_labels_disambiguate_shared_first_names_and_initial_collisions():
    teams = [team('Oak', 'Tanner Example'), team('Maple', 'Alex Morgan'),
             team('Birch', 'Alex Carter'), team('Pine', 'Alex Jordan')]
    assert [team_label(t, teams) for t in teams] == [
        'Oak (Tanner)', 'Maple (Alex M.)', 'Birch (Alex C.)', 'Pine (Alex J.)']
    teams.append(team('Cedar', 'Alex Jones'))
    assert team_label(teams[3], teams) == 'Pine (Alex Jordan)'
    assert team_label(teams[4], teams) == 'Cedar (Alex Jones)'


def test_multiple_managers_names_are_clean_deduplicated_and_no_owner_stays_plain():
    managed = team('Oak', ' Tanner  Example ', 'Tanner Example', 'Alex Morgan')
    managed.owners.extend([None, {'displayName': 'Commissioner', 'id': 'private-id'}, {'email': 'private@example.com'}])
    unknown = Obj(team_name='Maple')
    assert team_label(managed, [managed, unknown]) == 'Oak (Tanner, Alex, Commissioner)'
    assert team_label(unknown, [managed, unknown]) == 'Maple'
