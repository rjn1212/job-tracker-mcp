import pytest

from job_tracker_mcp.database import Database


@pytest.fixture
def db(tmp_path):
    """A Database backed by a throwaway file. Never touches data/jobs.db.

    A real file rather than :memory: so that WAL mode and the pragmas are
    exercised as configured in production.
    """
    database = Database(db_path=tmp_path / "test.db")
    yield database
    database.close()
