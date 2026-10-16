from .base import *

DMS_BASE_URL = env("DMS_BASE_URL", default="http://127.0.0.1:8000")

DEBUG = True

ALLOWED_HOSTS = [
    "127.0.0.1",
    "localhost",
]
