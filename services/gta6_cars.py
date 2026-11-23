import fcntl
import json
import os
import shutil
import subprocess
import sys
import uuid
from pathlib import Path


APP_ROOT = Path(__file__).resolve().parent.parent
CAR_LIBRARY_PATH = APP_ROOT / "static" / "gta6-cars" / "car_library.json"
MERGE_SCRIPT_PATH = APP_ROOT / "scripts" / "merge_gta6_cars.py"
CAR_WORK_ROOT = Path("workspace") / "gta6-cars"
MAX_CONTRIBUTION_BYTES = 2 * 1024 * 1024
MAX_CARS_PER_CONTRIBUTION = 32
MAX_ENTRY_BYTES = 64 * 1024


class ContributionError(ValueError):
    pass


def _raw_integer(value, label, allow_bool=False):
    if not isinstance(value, str) or len(value) > 128:
        raise ContributionError(f"Invalid {label}.")
    try:
        parsed = json.loads(value)
    except (json.JSONDecodeError, RecursionError) as error:
        raise ContributionError(f"Invalid {label}.") from error
    if type(parsed) is not int and not (allow_bool and type(parsed) is bool):
        raise ContributionError(f"Invalid {label}.")


def _clean_car(car):
    if not isinstance(car, dict):
        raise ContributionError("Each car must be an object.")
    model = car.get("model")
    model_id = car.get("modelId")
    street = car.get("street")
    name = car.get("name")
    car_class = car.get("class")
    entry = car.get("entry")
    row = car.get("row")
    source = car.get("source")
    if type(model) is not int or not 0 <= model <= 0xFFFFFFFF:
        raise ContributionError("Invalid car model.")
    if type(model_id) is not int or not 0 <= model_id <= 0xFFFFFFFF:
        raise ContributionError("Invalid car model ID.")
    if type(street) is not bool:
        raise ContributionError("Invalid car location.")
    if (
        not isinstance(name, str)
        or not name.strip()
        or len(name) > 200
        or any(ord(character) < 32 for character in name)
        or "<" in name
        or ">" in name
    ):
        raise ContributionError("Invalid car name.")
    if (
        not isinstance(car_class, str)
        or len(car_class) > 80
        or any(ord(character) < 32 for character in car_class)
        or "<" in car_class
        or ">" in car_class
    ):
        raise ContributionError("Invalid car class.")
    if not isinstance(entry, str) or not entry or len(entry.encode()) > MAX_ENTRY_BYTES:
        raise ContributionError("Invalid car entry.")
    try:
        entry_data = json.loads(entry)
    except (json.JSONDecodeError, RecursionError) as error:
        raise ContributionError("Invalid car entry.") from error
    if not isinstance(entry_data, dict) or "$0x1b60404d" not in entry_data or "$0x08ccb621" not in entry_data:
        raise ContributionError("Incomplete car entry.")
    if not isinstance(row, dict) or not 1 <= len(row) <= 64:
        raise ContributionError("Invalid car row.")
    for key, value in row.items():
        if not isinstance(key, str) or not key.startswith("$0x") or len(key) > 64:
            raise ContributionError("Invalid car row field.")
        _raw_integer(value, "car row value", allow_bool=True)
    if not isinstance(source, dict):
        raise ContributionError("Invalid car source.")
    _raw_integer(source.get("id2"), "car source")
    _raw_integer(source.get("id3"), "car source")
    parts = car.get("parts", 0)
    if type(parts) is not int or not 0 <= parts <= 4095:
        raise ContributionError("Invalid car part count.")
    if name.startswith("Unidentified vehicle #") or not car_class.strip():
        car_class = "Unknown"
    return {
        "model": model,
        "modelId": model_id,
        "street": street,
        "name": name,
        "class": car_class,
        "parts": parts,
        "entry": entry,
        "row": dict(row),
        "source": {"id2": source["id2"], "id3": source["id3"]},
    }


def clean_contribution(data):
    if not isinstance(data, dict) or not isinstance(data.get("cars"), list):
        raise ContributionError("A car list is required.")
    cars = data["cars"]
    if not 1 <= len(cars) <= MAX_CARS_PER_CONTRIBUTION:
        raise ContributionError(f"Send between 1 and {MAX_CARS_PER_CONTRIBUTION} cars.")
    return {"version": 1, "tool": "gta6-car-giver", "cars": [_clean_car(car) for car in cars]}


def _atomic_write(path, data):
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        with temporary.open("xb") as output:
            output.write(data)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _car_keys(document):
    return {
        f"{car['model'] & 0xFFFFFFFF}{'s' if car['street'] else 'g'}"
        for car in document["cars"]
    }


def _merge(contribution_path):
    CAR_WORK_ROOT.mkdir(parents=True, exist_ok=True)
    lock_path = CAR_WORK_ROOT / "library.lock"
    with lock_path.open("a+b") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        original = CAR_LIBRARY_PATH.read_bytes()
        before = json.loads(original)
        work = CAR_WORK_ROOT / f"merge-{uuid.uuid4().hex}"
        work.mkdir()
        try:
            candidate = work / "car_library.json"
            candidate.write_bytes(original)
            subprocess.run(
                [sys.executable, str(MERGE_SCRIPT_PATH), str(candidate), str(contribution_path)],
                check=True,
                capture_output=True,
                text=True,
                timeout=30,
            )
            after = json.loads(candidate.read_text(encoding="utf-8"))
            if not isinstance(after, dict) or not isinstance(after.get("cars"), list):
                raise RuntimeError("The merged car library is invalid.")
            before_keys = _car_keys(before)
            after_keys = _car_keys(after)
            if not before_keys.issubset(after_keys) or len(after_keys - before_keys) > MAX_CARS_PER_CONTRIBUTION:
                raise RuntimeError("The merged car library failed its integrity check.")
            _atomic_write(CAR_LIBRARY_PATH.with_suffix(".json.bak"), original)
            _atomic_write(CAR_LIBRARY_PATH, candidate.read_bytes())
            return len(after_keys - before_keys)
        finally:
            shutil.rmtree(work, ignore_errors=True)


def accept_contribution(data):
    document = clean_contribution(data)
    contributions = CAR_WORK_ROOT / "contributions"
    contributions.mkdir(parents=True, exist_ok=True)
    contribution_id = uuid.uuid4().hex
    contribution_path = contributions / f"{contribution_id}.json"
    _atomic_write(contribution_path, json.dumps(document, separators=(",", ":"), ensure_ascii=False).encode())
    try:
        added = _merge(contribution_path)
    except Exception:
        contribution_path.unlink(missing_ok=True)
        raise
    return contribution_id, added
