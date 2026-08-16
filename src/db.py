"""PostgreSQL bağlantı katmanı — sqlite3.Row benzeri dict-erişimli satırlar döndürür,
böylece geri kalan kod (api.py) minimum değişiklikle çalışmaya devam eder."""
import os
import urllib.parse as up
from decimal import Decimal

import pg8000.dbapi as pg
from dotenv import load_dotenv

load_dotenv()


def _pyval(v):
    """NUMERIC kolonları pg8000'de Decimal olarak gelir — SQLite'taki float davranışını
    korumak ve rule_engine'de tip karışıklığı olmaması için float'a çeviriyoruz."""
    return float(v) if isinstance(v, Decimal) else v


class Row(dict):
    """dict + hem d['col'] hem d.col erişimi (sqlite3.Row alışkanlığını korumak için)."""

    def __getattr__(self, item):
        try:
            return self[item]
        except KeyError as e:
            raise AttributeError(item) from e


class Connection:
    def __init__(self):
        url = up.urlparse(os.environ["DATABASE_URL"])
        self._conn = pg.connect(
            user=url.username, password=url.password, host=url.hostname,
            port=url.port or 5432, database=url.path.lstrip("/"), ssl_context=True,
        )
        # pg8000 varsayılan olarak her sorguyu bir transaction'a açar ve elle commit
        # ister — okuma-amaçlı (GET) endpoint'ler hiç commit çağırmadığı için bağlantı
        # "idle in transaction" olarak asılı kalıyordu; zamanla biriken bu bağlantılar
        # ALTER TABLE gibi DDL işlemlerini kilitleyebiliyordu. autocommit ile her
        # sorgu kendi transaction'ını hemen kapatıyor (yazma endpoint'lerindeki elle
        # commit() çağrıları artık no-op ama zararsız, dokunmaya gerek yok).
        self._conn.autocommit = True

    def execute(self, sql: str, params: tuple = ()):
        # sqlite tarzı '?' placeholder'ları, psycopg/pg8000 tarzı '%s'e çevrilir
        pg_sql = sql.replace("?", "%s")
        cur = self._conn.cursor()
        cur.execute(pg_sql, params)
        return _Result(cur)

    def close(self):
        self._conn.close()


class _Result:
    def __init__(self, cur):
        self._cur = cur
        self._cols = [d[0] for d in (cur.description or [])]

    def _wrap(self, row):
        if row is None:
            return None
        return Row(zip(self._cols, (_pyval(v) for v in row)))

    def fetchone(self):
        return self._wrap(self._cur.fetchone())

    def fetchall(self):
        return [self._wrap(r) for r in self._cur.fetchall()]


def db() -> Connection:
    return Connection()
