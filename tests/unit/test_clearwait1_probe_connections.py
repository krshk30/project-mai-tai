"""The rollback probe needs independent reader and writer transactions."""

import threading
from concurrent.futures import ThreadPoolExecutor

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.orm.exc import StaleDataError
from sqlalchemy.pool import StaticPool

from project_mai_tai.db.base import Base
from project_mai_tai.db.models import Strategy


def probe_factory(tmp_path):
    engine = create_engine(f"sqlite+pysqlite:///{tmp_path / 'rollback-probe.sqlite'}",
                           connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, expire_on_commit=False)


def test_static_pool_reader_rollback_can_remove_another_sessions_uncommitted_row():
    engine = create_engine("sqlite://", poolclass=StaticPool,
                           connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    try:
        with factory() as writer:
            row = Strategy(code="controlled", name="before", execution_mode="live")
            writer.add(row)
            writer.flush()
            connection = writer.connection().connection.driver_connection
            with factory() as reader:
                assert reader.connection().connection.driver_connection is connection
                assert reader.scalar(select(Strategy.id)) == row.id
            row.name = "after"
            with pytest.raises(StaleDataError, match="0 were matched"):
                writer.flush()
    finally:
        engine.dispose()


def test_file_pool_parallel_readers_have_distinct_connections_and_do_not_rollback_writer(tmp_path):
    factory = probe_factory(tmp_path)
    barrier = threading.Barrier(2)

    def read():
        with factory() as reader:
            connection = reader.connection().connection.driver_connection
            assert reader.scalar(select(Strategy.id)) is None
            barrier.wait(timeout=2)
            return connection

    try:
        with factory() as writer:
            row = Strategy(code="controlled", name="before", execution_mode="live")
            writer.add(row)
            writer.flush()
            connection = writer.connection().connection.driver_connection
            with ThreadPoolExecutor(max_workers=2) as pool:
                jobs = [pool.submit(read) for _ in range(2)]
                readers = [job.result(timeout=3) for job in jobs]
            assert readers[0] is not readers[1]
            assert all(reader is not connection for reader in readers)
            row.name = "after"
            writer.flush()
            writer.commit()
        with factory() as reader:
            assert reader.scalar(select(Strategy.name)) == "after"
    finally:
        factory.kw["bind"].dispose()
