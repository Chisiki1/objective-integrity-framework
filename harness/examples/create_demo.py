"""Create a labelled example workspace with scripted decisions and real file tools.

No provider request, credentials, invented execution result or production data.
Use only a new data directory. Start OIF against it after this script exits.
"""
import argparse
import asyncio
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))
from policy_harness.executor import Executor
from policy_harness.knowledge import Knowledge
from policy_harness.practical_engine import PracticalEngine
from policy_harness.practical_policy import PracticalPolicy
from policy_harness.store import Store
from policy_harness.organization import Organization


class ScriptedProvider:
    def __init__(self, translations=None):
        self.replies = []
        self.translations = translations or {}

    def translate(self, value):
        if isinstance(value, str):
            return self.translations.get(value, value)
        if isinstance(value, list):
            return [self.translate(item) for item in value]
        if isinstance(value, dict):
            return {key: self.translate(item) for key, item in value.items()}
        return value

    async def generate(self, role, phase, payload, schema):
        if not self.replies:
            raise AssertionError('Example requested an unexpected decision: ' + phase)
        return schema.model_validate(self.translate(self.replies.pop(0))), {'fixture': True}

    async def health(self):
        return {'ready': True, 'fixture': True}


class NoResearch:
    async def collect(self, *args, **kwargs):
        raise AssertionError('This example does not perform external research')


def write(path, text, message):
    return {'action': 'tools', 'message': message, 'tools': [
        {'name': 'file_write', 'purpose': 'Save the requested text and verify its bytes',
         'arguments': {'path': path, 'text': text}}]}


def complete(path, message):
    return {'action': 'complete', 'message': message, 'artifacts': [path],
            'acceptance': [{'criterion': 0, 'evidence': 'The saved file was read back by the file tool.'}]}


async def main(directory, language='en'):
    directory = directory.resolve()
    if directory.exists():
        raise ValueError('Use an absent example data directory; nothing was replaced.')
    store = Store(directory)
    translations = json.loads((Path(__file__).with_name('ja.json')).read_text(encoding='utf-8')) if language == 'ja' else {}
    provider = ScriptedProvider(translations)
    tr = provider.translate
    engine = PracticalEngine(store, PracticalPolicy(ROOT / 'policy/complete-policy-v3.json'),
        Executor(directory), provider, NoResearch(), Knowledge(store))
    org = Organization(store)
    folder = org.create_folder(tr('Example workspace'))
    report = '''# A focused release, ready to share

## The goal
Make the next release easy to understand and easy to try.

## What changed
- A dedicated workspace keeps instructions and results together.
- Artifacts can be previewed before they are downloaded.
- Useful lessons can be applied to later work.

## The next step
Share a small, reproducible example and invite specific feedback.

---
Illustrative content created by the scripted OIF example.
'''
    first = store.create_task(tr('Draft a concise launch brief'), tr(['Save the requested Markdown brief']),
                             options={'folder_id': folder['id']})
    done = complete('launch-brief.md', 'The launch brief is ready. It explains the goal, the three changes and the next step. Open the Markdown preview to read it.')
    done['learning'] = [dict(action='create', title='Verify text where it is written',
        reason='The file tool returned the saved bytes and text facts.', operation_indices=[0],
        applies_when='Writing literal UTF-8 text',
        procedure='Write the exact content, then use the file tool readback to check the saved text.',
        limits='This checks text content, not executable behavior.',
        next_trigger='The next text output that needs exact content', share_scope='general',
        sharing_reason='A general verification procedure; no individual content or paths are shared.')]
    provider.replies = [write('launch-brief.md', report, 'Writing a short brief with the goal, changes and next step.'), done]
    await engine.start_task(first['id'])
    assert store.get_task(first['id'])['status'] == 'completed'
    assert len(store.records('practical_skill')) == 1
    second = store.create_task(tr('Prepare the welcome message'), tr(['Save the welcome text']), options={'folder_id':folder['id']})
    action = write('welcome.txt', 'Welcome to a workspace that remembers the goal.', 'Applying the shared text-verification lesson to this new task.')
    action['skill_uses'] = [{'skill':0, 'tool_index':0, 'adaptation':'Use the saved-text readback for this output.'}]
    done = complete('welcome.txt', 'The welcome message is saved and checked. The shared lesson was used in this new chat and its outcome recorded.')
    done['learning_assessments'] = [{'operation_index':0, 'judgment':'helpful', 'reason':'Saved text was checked in the same file operation.'}]
    provider.replies = [action, done]
    await engine.start_task(second['id'])
    assert store.get_task(second['id'])['status'] == 'completed'
    uses = store.task_records('practical_skill_use', second['id'])
    assert len(uses) == 1 and uses[0]['assessment']['judgment'] == 'helpful'
    assert not store.task_records('practical_learning_rejection', second['id'])
    third = store.create_task(tr('Keep the next milestone clear'), tr(['Save the checklist']), options={'folder_id':folder['id']})
    provider.replies = [write('next-step.md', '# Next milestone\n\n- Review the brief.\n- Try the welcome message.\n- Record specific feedback.\n', 'Turning the next milestone into a short checklist.'),
        complete('next-step.md','The next milestone is written as three concrete actions.')]
    await engine.start_task(third['id'])
    assert store.get_task(third['id'])['status'] == 'completed'
    org.update(first['id'], {'pinned':True}, org.metadata(first['id'])['revision'])
    print(json.dumps({'example':True,'scripted_decisions':True,'real_file_operations':True,
        'tasks':[first['id'],second['id'],third['id']], 'shared_lessons':len(store.records('practical_skill')),
        'cross_task_uses_assessed':len(uses)}))
    store.close()


if __name__ == '__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--data-directory',type=Path,required=True)
    parser.add_argument('--language',choices=['en','ja'],default='en')
    args=parser.parse_args()
    asyncio.run(main(args.data_directory,args.language))
