"""Disposable, bounded display projections. Saved records remain authoritative."""


def preview(value, *, chars=3000, items=30, depth=6):
    if isinstance(value, str):
        return value if len(value) <= chars else value[:chars] + '\n…'
    if depth <= 0 and isinstance(value, (dict, list, tuple)):
        return '[詳細は原記録に保存されています]'
    if isinstance(value, dict):
        result = {k: preview(v, chars=chars, items=items, depth=depth-1)
                  for k, v in list(value.items())[:items]}
        if len(value) > items:
            result['_more_fields'] = len(value)-items
        return result
    if isinstance(value, (list, tuple)):
        result = [preview(v, chars=chars, items=items, depth=depth-1) for v in value[:items]]
        if len(value) > items:
            result.append({'_more_records': len(value)-items})
        return result
    return value


def event_view(event):
    result = {k: v for k, v in event.items() if k != 'detail'}
    # User instructions and completed answers retain their text in the chat.
    chars = 100000 if event['stage'] in ('source', 'task') else 3000
    result['detail'] = preview(event.get('detail'), chars=chars)
    result['_record_url'] = f"/api/tasks/{event['task_id']}/records/events/{event['seq']}"
    return result


def conversation_view(snapshot, after=0):
    """Input has already passed credential redaction; never trim secrets first."""
    result = dict(snapshot)
    result['events'] = [event_view(e) for e in snapshot.get('events', []) if e['seq'] > after]
    task = dict(snapshot['task'])
    task['state'] = preview(task.get('state', {}))
    task['source_history'] = [dict(preview(source, chars=4000), _index=source.get('_index', index)) for index,source in enumerate(task.get('source_history', []))]
    if task.get('final'):
        task['final'] = preview(task['final'], chars=12000)
    result['task'] = task
    operations = []
    for row in snapshot.get('operations', []):
        actual = row.get('result') or {}
        operations.append({
            'operation': {k: row['operation'].get(k) for k in ('id', 'kind', 'purpose')},
            'tool_name': row.get('tool_name'), 'status': row.get('status'),
            'started_at': row.get('started_at'), 'source_hash': row.get('source_hash'),
            'result': {k: actual.get(k) for k in ('status', 'effect', 'artifacts')},
            '_record_url': f"/api/tasks/{task['id']}/records/operations/{row['operation']['id']}",
        })
    result['operations'] = operations
    knowledge = snapshot.get('knowledge') or {}
    result['knowledge'] = {key: [preview(r) for r in rows] if isinstance(rows, list) else preview(rows)
                           for key, rows in knowledge.items()}
    result['readiness'] = preview(snapshot.get('readiness', {}))
    result['_record_url'] = f"/api/tasks/{task['id']}/records"
    result['display_projection'] = True
    return result
