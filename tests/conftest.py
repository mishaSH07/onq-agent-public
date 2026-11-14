"""Fictional data for offline collection and publishing tests."""

import pytest

from onq.store import Store


def add_courses(store):
    for number in range(1, 5):
        store.save_course({
            'id': f'{number:032x}',
            'org_id': str(number),
            'name': f'Example Course {number}',
            'semester': 'Fall 2026',
            'project': f'https://chatgpt.com/g/g-p-example-{number}/project',
            'enabled': True,
            'schedule': None,
            'next_run': None,
            'local_status': 'Not checked',
            'cloud_status': 'Not verified',
            'last_check': None,
            'new_count': 0,
        })


@pytest.fixture
def course_store(tmp_path):
    store = Store(tmp_path / 'state', tmp_path / 'library')
    add_courses(store)
    return store
