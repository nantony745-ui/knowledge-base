from contextlib import contextmanager
from functools import lru_cache

from sqlalchemy import URL, create_engine, event
from sqlalchemy.orm import Session

from app.settings import load_settings


@lru_cache
def engine():
    s = load_settings()
    s.require('MYSQL_PASSWORD')
    url = URL.create('mysql+pymysql', username=s.MYSQL_USER, password=s.MYSQL_PASSWORD.get_secret_value(),
                     host=s.MYSQL_HOST, port=s.MYSQL_PORT, database=s.MYSQL_DATABASE)
    result = create_engine(url, pool_pre_ping=True, pool_recycle=1800, connect_args={'charset': 'utf8mb4'})
    @event.listens_for(result, 'connect')
    def utc_connection(connection, record):
        with connection.cursor() as cursor:
            cursor.execute("SET time_zone = '+00:00'")
    return result


@contextmanager
def session():
    with Session(engine(), expire_on_commit=False) as db:
        try:
            yield db
        except BaseException:
            db.rollback()
            raise
