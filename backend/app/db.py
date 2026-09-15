from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker


def get_engine(dsn: str):
    return create_engine(dsn, pool_pre_ping=True)


def session_factory(engine):
    return sessionmaker(bind=engine, expire_on_commit=False)
