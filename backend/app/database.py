"""数据库连接与初始化。

SQLite 用在多进程（API + worker）场景下有两个必须做对的地方：

1. **WAL 模式**。它让读不阻塞写、写不阻塞读。单机百人规模完全够用。
   WAL 是**持久属性**，设一次永久生效，不必每连接设。
   注意：WAL 依赖共享内存，**data/ 目录不能放 NFS 或任何网络盘**，
   否则会损坏数据库。

2. **busy_timeout**。这是**每连接**属性，必须每个连接都设。
   不设的话，worker 写结果的那一瞬间，任何并发的 API 写操作都会立刻
   抛 `database is locked`，用户看到的是随机报错。
   这里通过 `connect_args={"timeout": 30}` 交给 sqlite3 驱动设置，
   而不是靠"记得执行 PRAGMA"。
"""

from pathlib import Path

from sqlalchemy import event
from sqlmodel import Session, SQLModel, create_engine

from .config import settings

DATA_DIR: Path = settings.data_dir
UPLOAD_DIR: Path = settings.upload_dir
FRAMES_DIR: Path = settings.frames_dir
BACKUP_DIR: Path = settings.backup_dir
DATABASE_URL = f"sqlite:///{DATA_DIR / 'app.db'}"

for directory in (DATA_DIR, UPLOAD_DIR, FRAMES_DIR, BACKUP_DIR):
    directory.mkdir(parents=True, exist_ok=True)

engine = create_engine(
    DATABASE_URL,
    # timeout=30 是 sqlite3 的 busy_timeout（秒），不是连接超时。
    # check_same_thread=False：FastAPI 的线程池会跨线程复用连接。
    connect_args={"check_same_thread": False, "timeout": 30},
)


@event.listens_for(engine, "connect")
def _set_sqlite_pragmas(dbapi_connection, _connection_record) -> None:
    """每个新连接都要设的 PRAGMA。"""
    cursor = dbapi_connection.cursor()
    try:
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA synchronous=NORMAL")  # WAL 下 NORMAL 已足够安全
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.execute("PRAGMA busy_timeout=30000")
    finally:
        cursor.close()


def init_db() -> None:
    SQLModel.metadata.create_all(engine)


def get_session():
    with Session(engine) as session:
        yield session
