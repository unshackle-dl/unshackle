import logging
import os
import random
from datetime import datetime, timedelta

log = logging.getLogger("NF-ESN")


def chrome_esn_generator():
    ESN_GEN = "".join(random.choice("0123456789ABCDEF") for _ in range(30))
    esn_file = ".esn"

    def gen_file():
        with open(esn_file, "w") as file:
            file.write(f"NFCDIE-03-{ESN_GEN}")

    if not os.path.isfile(esn_file):
        log.warning("Generating a new Chrome ESN")
        gen_file()

    file_datetime = datetime.fromtimestamp(os.path.getmtime(esn_file))
    time_diff = datetime.now() - file_datetime
    if time_diff > timedelta(hours=6):
        log.warning("Old ESN detected, Generating a new Chrome ESN")
        gen_file()

    with open(esn_file, "r") as f:
        esn = f.read()

    return esn
