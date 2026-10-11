import os
import sys

CURRENT_DIR = os.path.dirname(os.path.abspath(__file__))
if CURRENT_DIR not in sys.path:
    sys.path.insert(0, CURRENT_DIR)

os.environ.setdefault("OUTPUT_DIR", "/tmp/generation-output")

from mangum import Mangum
from main import app

mangum_handler = Mangum(app, lifespan="off")


def lambda_handler(event, context):
    return mangum_handler(event, context)


handler = lambda_handler
