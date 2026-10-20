"""migrate(engine): fresh-create, idempotence, the shared adopt guard."""

import pytest
import sqlalchemy as sa

import asas_sync
from asas_sync.migrate import VERSION_TABLE


def test_fresh_create_and_idempotent(engine):
    asas_sync.migrate(engine)
    asas_sync.migrate(engine)
    inspector = sa.inspect(engine)
    for table in ("asas_sync_cursor", "asas_sync_seen", VERSION_TABLE):
        assert inspector.has_table(table)


def test_an_unrelated_table_of_the_same_name_is_refused(engine):
    with engine.begin() as conn:
        conn.execute(sa.text("CREATE TABLE asas_sync_cursor (id INTEGER PRIMARY KEY)"))
        conn.execute(sa.text("CREATE TABLE asas_sync_seen (id INTEGER PRIMARY KEY)"))
    with pytest.raises(RuntimeError, match="cannot adopt"):
        asas_sync.migrate(engine)
