"""Run only approved additive 0023, from exact hashed application migration files."""
import argparse
import hashlib
import json
from pathlib import Path, PurePosixPath
import tarfile

BASE = '20261005_0022'
TARGET = '20261008_0023'


def extract(job, attempt):
    release = json.loads((job / 'release.json').read_bytes())
    archive = job / 'approved-migrations.tar'
    if hashlib.sha256(archive.read_bytes()).hexdigest() != release['artifacts']['approved-migrations.tar']:
        raise RuntimeError('migration archive hash differs')
    output = attempt / 'migration-source'
    with tarfile.open(archive) as stream:
        members = stream.getmembers()
        if len(members) > 256 or sum(row.size for row in members) > 4_000_000:
            raise RuntimeError('migration archive bound exceeded')
        for row in members:
            path = PurePosixPath(row.name)
            if path.is_absolute() or '..' in path.parts or str(path) != row.name.rstrip('/'):
                raise RuntimeError('unsafe migration path')
            name = row.name.rstrip('/')
            if name != 'alembic.ini' and not name.startswith('sql/migrations/') and name not in ('sql', 'sql/migrations'):
                raise RuntimeError('foreign migration path')
            if not row.isfile() and not row.isdir():
                raise RuntimeError('migration links refused')
        output.mkdir(mode=0o700, exist_ok=False)
        for row in members:
            if row.isfile():
                path = output / row.name
                path.parent.mkdir(parents=True, exist_ok=True)
                with path.open('xb') as handle:
                    handle.write(stream.extractfile(row).read())
                path.chmod(0o600)
    for name in ('alembic.ini', 'sql/migrations/env.py', 'sql/migrations/versions/20261008_0023_entry_classification.py'):
        if hashlib.sha256((output / name).read_bytes()).hexdigest() != release['source_hashes'][name]:
            raise RuntimeError('migration source hash differs: ' + name)
    return output


def upgrade(source, database_url):
    import os
    from alembic import command
    from alembic.config import Config
    from sqlalchemy import create_engine, text

    engine = create_engine(database_url)
    def revision():
        with engine.connect() as connection:
            return connection.execute(text('SELECT version_num FROM alembic_version')).scalars().all()
    try:
        before = revision()
        if before not in ([BASE], [TARGET]):
            raise RuntimeError('unexpected migration base revision')
        if before == [BASE]:
            # env.py reads get_settings; expose only the DB setting, never log it.
            os.environ['MAI_TAI_DATABASE_URL'] = database_url
            from project_mai_tai.settings import get_settings
            get_settings.cache_clear()
            config = Config(str(source / 'alembic.ini'))
            config.set_main_option('script_location', str(source / 'sql/migrations'))
            command.upgrade(config, TARGET)
        after = revision()
        if after != [TARGET]:
            raise RuntimeError('migration 0023 not applied')
        return dict(before=before, after=after, upgrade_called=before == [BASE], target=TARGET)
    finally:
        engine.dispose()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--job', type=Path, required=True)
    parser.add_argument('--attempt', type=Path, required=True)
    args = parser.parse_args()
    from project_mai_tai.settings import Settings
    source = extract(args.job, args.attempt)
    receipt = upgrade(source, Settings(_env_file='/etc/project-mai-tai/project-mai-tai.env').database_url)
    with (args.attempt / 'migration0023.json').open('x') as handle:
        json.dump(receipt, handle, sort_keys=True)
    print(json.dumps(receipt, sort_keys=True))


if __name__ == '__main__':
    main()
