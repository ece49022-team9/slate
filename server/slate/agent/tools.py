#for now test tool... will have browser tools later
from datetime import datetime


def get_current_time() -> str:
    return datetime.now().astimezone().strftime("%Y-%m-%d %H:%M:%S %Z")