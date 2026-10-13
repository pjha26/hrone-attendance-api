import os
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from pymongo import MongoClient

from app import main


@pytest.fixture(scope="session")
def api():
    uri = os.getenv("TEST_MONGO_URI")
    if not uri:
        pytest.skip("Set TEST_MONGO_URI for real MongoDB integration tests")
    connection = MongoClient(uri, tz_aware=True, serverSelectionTimeoutMS=3000)
    connection.admin.command("ping")
    database = connection["hrone_test_" + uuid4().hex]
    old_client, old_db = main.client, main.db
    main.client, main.db = connection, database
    try:
        with TestClient(main.app) as http:
            try:
                yield http
            finally:
                connection.drop_database(database.name)
    finally:
        main.client, main.db = old_client, old_db


@pytest.fixture
def database(api):
    main.db.employees.delete_many({})
    main.db.attendance_logs.delete_many({})
    return main.db
