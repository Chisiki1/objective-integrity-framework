"""Small derived search index. Original lessons and evidence remain authoritative."""
import re


def ensure_index(store):
    with store.lock:
        version = store.record_get('controller_schema', 'skill_search')
        present = store.db.execute("SELECT count(*) FROM sqlite_master WHERE name IN ('skill_search','skill_search_insert','skill_search_update','skill_search_delete','skill_use_version')").fetchone()[0]
        if version == {'version': 2} and present == 5:
            return
        # Python 3.12's bundled SQLite supports this tokenizer on both supported
        # runtimes. Unlike word-only search it also finds Japanese procedures.
        fields = " || ' ' || ".join(
            "coalesce(json_extract({body},'$." + key + "'),'')"
            for key in ('title', 'applies_when', 'procedure', 'next_trigger', 'tools'))
        def values(body, rowid):
            # Legacy folder lessons without explicit sharing remain owner-only.
            # Store effective visibility in the derived index before any LIMIT.
            scope = (f"CASE WHEN json_extract({body},'$.scope') LIKE 'folder:%' "
                     f"AND trim(coalesce(json_extract({body},'$.sharing_reason'),''))='' "
                     f"THEN 'task:'||json_extract({body},'$.owner_task_id') "
                     f"ELSE json_extract({body},'$.scope') END")
            return (f"{rowid},json_extract({body},'$.id'),{scope},"
                    f"json_extract({body},'$.owner_task_id'),json_extract({body},'$.status'),"
                    + fields.format(body=body))
        store.db.executescript(f'''
            BEGIN IMMEDIATE;
            CREATE VIRTUAL TABLE IF NOT EXISTS skill_search USING fts5(
                id UNINDEXED, scope UNINDEXED, owner UNINDEXED, status UNINDEXED,
                terms, tokenize='trigram');
            DROP TRIGGER IF EXISTS skill_search_insert;
            DROP TRIGGER IF EXISTS skill_search_update;
            DROP TRIGGER IF EXISTS skill_search_delete;
            CREATE TRIGGER IF NOT EXISTS skill_search_insert AFTER INSERT ON records
                WHEN NEW.kind='practical_skill' BEGIN
                INSERT INTO skill_search(rowid,id,scope,owner,status,terms) VALUES({values('NEW.body', 'NEW.rowid')});
            END;
            CREATE TRIGGER IF NOT EXISTS skill_search_update AFTER UPDATE OF body ON records
                WHEN NEW.kind='practical_skill' BEGIN
                DELETE FROM skill_search WHERE rowid=OLD.rowid;
                INSERT INTO skill_search(rowid,id,scope,owner,status,terms) VALUES({values('NEW.body', 'NEW.rowid')});
            END;
            CREATE TRIGGER IF NOT EXISTS skill_search_delete AFTER DELETE ON records
                WHEN OLD.kind='practical_skill' BEGIN
                DELETE FROM skill_search WHERE rowid=OLD.rowid;
            END;
            DELETE FROM skill_search;
            INSERT INTO skill_search(rowid,id,scope,owner,status,terms)
                SELECT {values('body', 'rowid')} FROM records WHERE kind='practical_skill';
            CREATE INDEX IF NOT EXISTS skill_use_version ON records(
                json_extract(body,'$.skill_id'),json_extract(body,'$.skill_revision'))
                WHERE kind='practical_skill_use';
            INSERT OR REPLACE INTO records VALUES('controller_schema','skill_search','{{"version":2}}');
            COMMIT;
        ''')


def search(store, task_id, scope, query='', tools=(), limit=32):
    # Task text and tool names are data, never FTS syntax. The trigram
    # tokenizer cannot match runs shorter than three characters, so a
    # two-character Japanese run is kept aside for the derived column below.
    terms = list(tools)
    short = []
    for word in re.findall(r'[a-zA-Z_][a-zA-Z_0-9-]{2,}|[\u3040-\u30ff\u4e00-\u9fff]{2,}', query[:3000]):
        if word.isascii():
            terms.append(word.casefold())
        elif len(word) == 2:
            short.append(word)
        else:
            terms.extend(word[i:i + 3] for i in range(len(word) - 2))
    terms = list(dict.fromkeys(terms))[:48]
    short = list(dict.fromkeys(short))[:16]
    with store.lock:
        identities = []
        if terms:
            match = ' OR '.join('"' + t.replace('"', '""') + '"' for t in terms)
            identities = [r[0] for r in store.db.execute('''SELECT id FROM skill_search
                WHERE skill_search MATCH ? AND status!='retired'
                AND (scope IN (?, 'general') OR owner=?)
                ORDER BY rank LIMIT ?''', (match, scope, task_id, limit))]
        # The derived index already stores each lesson's searchable text in its
        # `terms` column, so a short run is found there by a bounded substring
        # lookup. No per-record JSON parsing happens in this query.
        if short:
            clause = ' OR '.join("terms LIKE ? ESCAPE '!'" for _ in short)
            keys = tuple('%' + s.replace('!', '!!').replace('%', '!%').replace('_', '!_') + '%'
                         for s in short)
            identities.extend(r[0] for r in store.db.execute(f'''SELECT id FROM skill_search
                WHERE ({clause}) AND status!='retired'
                AND (scope IN (?, 'general') OR owner=?)
                LIMIT ?''', keys + (scope, task_id, limit)))
        # Own recent lessons remain reachable before the next action is known.
        # Unrelated global lessons never displace relevant older search hits.
        for field, value in ([('scope', scope), ('owner_task_id', task_id)] if query or tools
                             else [('scope', scope), ('scope', 'general'), ('owner_task_id', task_id)]):
            identities.extend(r[0] for r in store.db.execute(f'''SELECT id FROM records
                WHERE kind='practical_skill' AND json_extract(body,'$.{field}')=?
                AND json_extract(body,'$.status')!='retired'
                AND (json_extract(body,'$.owner_task_id')=? OR json_extract(body,'$.scope')='general'
                     OR (json_extract(body,'$.scope')=? AND trim(coalesce(json_extract(body,'$.sharing_reason'),''))!=''))
                ORDER BY json_extract(body,'$.updated_at') DESC LIMIT 8''', (value,task_id,scope)))
        # Only these bounded candidates decode full records; thousands of older
        # lessons, original tool results and revision histories are not loaded.
        return [store.record_get('practical_skill', identity)
                for identity in list(dict.fromkeys(identities))[:limit]]


def refresh_outcomes(store, skill_id):
    skill = store.record_get('practical_skill', skill_id)
    if not skill:
        return
    counts = dict(helpful=0, no_change=0, harmful=0, inconclusive=0)
    with store.lock:
        for row in store.db.execute('''SELECT json_extract(body,'$.assessment.judgment'),count(*)
            FROM records WHERE kind='practical_skill_use'
            AND json_extract(body,'$.skill_id')=? AND json_extract(body,'$.skill_revision')=?
            AND json_extract(body,'$.status')='result_observed'
            GROUP BY json_extract(body,'$.assessment.judgment')''', (skill_id, skill['revision'])):
            if row[0] in counts:
                counts[row[0]] = row[1]
        if skill.get('use_outcomes') != counts:
            store.record('practical_skill', skill_id, dict(skill, use_outcomes=counts))
