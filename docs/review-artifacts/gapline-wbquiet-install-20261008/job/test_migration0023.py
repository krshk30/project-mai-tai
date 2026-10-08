import io
import json
import subprocess
import tarfile

import pytest
from sqlalchemy import create_engine, inspect, text

import migration0023 as migration


def test_actual_reviewed_git_archive_extracts_with_hash_bound_sources(tmp_path):
    job, attempt = tmp_path / 'job', tmp_path / 'attempt'
    job.mkdir()
    attempt.mkdir()
    raw = subprocess.check_output(['git', 'archive', '--format=tar', 'fa278ecd', 'alembic.ini', 'sql/migrations'])
    import hashlib
    names = ['alembic.ini', 'sql/migrations/env.py', 'sql/migrations/versions/20261008_0023_entry_classification.py']
    source_hashes = {name: hashlib.sha256(subprocess.check_output(['git', 'show', 'fa278ecd:' + name])).hexdigest() for name in names}
    (job / 'approved-migrations.tar').write_bytes(raw)
    (job / 'release.json').write_text(json.dumps(dict(artifacts={'approved-migrations.tar': hashlib.sha256(raw).hexdigest()}, source_hashes=source_hashes)))
    source = migration.extract(job, attempt)
    assert 'entry_classification' in (source / names[-1]).read_text()
    assert 'nullable=True' in (source / names[-1]).read_text()


def test_actual_alembic_additive_nullable_json_and_exact_target(tmp_path, monkeypatch):
    source = tmp_path / 'source'
    versions = source / 'sql/migrations/versions'
    versions.mkdir(parents=True)
    (source / 'alembic.ini').write_text('[alembic]\n')
    (source / 'sql/migrations/env.py').write_text(
        'import os\nfrom alembic import context\nfrom sqlalchemy import create_engine\n'
        'engine=create_engine(os.environ["MAI_TAI_DATABASE_URL"])\n'
        'with engine.connect() as connection:\n'
        '    context.configure(connection=connection)\n'
        '    with context.begin_transaction(): context.run_migrations()\n')
    (versions / 'base.py').write_text('revision="20261005_0022"\ndown_revision=None\ndef upgrade(): pass\n')
    # Recorded reviewed H migration, no modelled trading behavior.
    (versions / '0023.py').write_text(
        'import sqlalchemy as sa\nfrom alembic import op\nrevision="20261008_0023"\n'
        'down_revision="20261005_0022"\ndef upgrade():\n'
        '    op.add_column("oms_managed_positions", sa.Column("entry_classification", sa.JSON(), nullable=True))\n')
    url = 'sqlite:///' + str(tmp_path / 'isolated.db')
    engine = create_engine(url)
    with engine.begin() as connection:
        connection.execute(text('CREATE TABLE alembic_version (version_num VARCHAR(32))'))
        connection.execute(text("INSERT INTO alembic_version VALUES ('20261005_0022')"))
        connection.execute(text('CREATE TABLE oms_managed_positions (id VARCHAR(32) PRIMARY KEY)'))
        connection.execute(text("INSERT INTO oms_managed_positions VALUES ('old-row')"))
    monkeypatch.setenv('MAI_TAI_DATABASE_URL', url)
    result = migration.upgrade(source, url)
    assert result == dict(before=[migration.BASE], after=[migration.TARGET], upgrade_called=True, target=migration.TARGET)
    columns = {row['name']: row for row in inspect(engine).get_columns('oms_managed_positions')}
    assert set(columns) == {'id', 'entry_classification'} and columns['entry_classification']['nullable'] is True
    with engine.connect() as connection:
        assert connection.execute(text('SELECT entry_classification FROM oms_managed_positions')).scalar() is None
    assert migration.upgrade(source, url)['upgrade_called'] is False
    engine.dispose()


def test_different_revision_refuses_without_upgrade(tmp_path):
    url = 'sqlite:///' + str(tmp_path / 'wrong.db')
    engine = create_engine(url)
    with engine.begin() as connection:
        connection.execute(text('CREATE TABLE alembic_version (version_num VARCHAR(32))'))
        connection.execute(text("INSERT INTO alembic_version VALUES ('unreviewed')"))
    with pytest.raises(RuntimeError, match='unexpected migration base'):
        migration.upgrade(tmp_path, url)
    engine.dispose()


@pytest.mark.parametrize('name', ['../escape', '/escape', 'src/source.py'])
def test_migration_archive_cannot_write_foreign_or_escape_paths(tmp_path, name):
    job, attempt = tmp_path / 'job', tmp_path / 'attempt'
    job.mkdir()
    attempt.mkdir()
    archive = job / 'approved-migrations.tar'
    with tarfile.open(archive, 'w') as stream:
        member = tarfile.TarInfo(name)
        member.size = 1
        stream.addfile(member, io.BytesIO(b'x'))
    import hashlib
    (job / 'release.json').write_text(json.dumps(dict(artifacts={archive.name: hashlib.sha256(archive.read_bytes()).hexdigest()})))
    with pytest.raises(RuntimeError, match='migration path'):
        migration.extract(job, attempt)
    assert not (attempt / 'migration-source').exists()
