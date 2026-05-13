from datetime import datetime

from anysql import __version__
from anysql.scheduler import next_daily_run
from anysql.storage.repositories import decrypt_secret, encrypt_secret, stable_hash


def test_version_is_bumped_to_team_architecture_release():
    assert __version__ == "0.6.3"


def test_secret_round_trip_preserves_product_password():
    password = "DJN"
    assert decrypt_secret(encrypt_secret(password)) == password


def test_stable_hash_ignores_dict_key_order():
    left = {"table": "A", "columns": [{"name": "C1", "type": "VARCHAR2"}]}
    right = {"columns": [{"type": "VARCHAR2", "name": "C1"}], "table": "A"}
    assert stable_hash(left) == stable_hash(right)


def test_scheduler_waits_until_next_3am_instead_of_running_on_startup():
    assert next_daily_run(datetime(2026, 5, 12, 2, 30)) == datetime(2026, 5, 12, 3, 0)
    assert next_daily_run(datetime(2026, 5, 12, 3, 0)) == datetime(2026, 5, 13, 3, 0)
