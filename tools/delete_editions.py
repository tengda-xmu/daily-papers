"""Apply a validated archive operation in the serialized publication workflow."""
import json
import os
import re
import uuid
from pathlib import Path

from src.editions import delete_editions
from tools.recommendation_data import reconcile


def main():
    request_id = str(uuid.UUID(os.environ['DELETE_REQUEST_ID']))
    revision = os.environ['DELETE_REVISION']
    ids = json.loads(os.environ['DELETE_EDITION_IDS'])
    if not re.fullmatch(r'[a-f0-9]{64}', revision) or not isinstance(ids, list) or not 1 <= len(ids) <= 1000 or not all(isinstance(i, str) for i in ids):
        raise ValueError('Invalid deletion request')
    data = Path(__file__).resolve().parents[1] / 'data'
    reconcile(data)
    delete_editions(data, ids, revision, request_id)


if __name__ == '__main__':
    main()
