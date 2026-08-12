from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

from app.core.config import settings


engine = create_engine(
    settings.sqlalchemy_database_uri,
    pool_pre_ping=True,
    pool_size=5,
    max_overflow=10,
    pool_recycle=1800,
    pool_timeout=30,
    connect_args={"connect_timeout": 3},
    echo_pool=False,
)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def db_ping() -> bool:
    try:
        with engine.connect() as connection:
            connection.execute(text("SELECT 1"))
        return True
    except Exception:
        return False


def get_connection_pool_status() -> dict:
    """Get current connection pool status for monitoring."""
    pool = engine.pool
    return {
        "pool_size": pool.size(),
        "checked_out": pool.checkedout(),
        "available": pool.size() - pool.checkedout(),
        "total_connections": pool.size() + pool.checkedout(),
        "pool_type": type(pool).__name__,
    }
