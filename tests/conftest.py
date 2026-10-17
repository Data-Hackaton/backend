import os
import tempfile

# окружение до импорта backend: engine БД создаётся при импорте
_tmp = tempfile.mkdtemp()
# TEST_DATABASE_URL — прогон на Postgres (пустая база): SQL, который sqlite прощает, там падает
os.environ["DATABASE_URL"] = os.environ.get("TEST_DATABASE_URL") or f"sqlite:///{_tmp}/test.db"
os.environ["ADMIN_KEY"] = "test-admin"
os.environ["META_DIR"] = os.path.join(os.path.dirname(__file__), "meta")
os.environ["VALKEY_URL"] = "redis://127.0.0.1:1/0"  # недоступен: прогресс просто не пишется
