from __future__ import annotations

import pytest
from sqlalchemy.orm import sessionmaker

from app.db import init_db, make_engine


@pytest.fixture()
def engine(tmp_path):
    engine = make_engine(f"sqlite:///{(tmp_path / 'test.db').as_posix()}")
    init_db(engine)
    yield engine
    engine.dispose()


@pytest.fixture()
def session_factory(engine):
    return sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


@pytest.fixture()
def session(session_factory):
    with session_factory() as session:
        yield session
