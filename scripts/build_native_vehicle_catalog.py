#!/usr/bin/env python3
"""Build the browser's experimental PS5 vehicle catalog from local evidence.

Usage:
  python scripts/build_native_vehicle_catalog.py VEHICLES.sqlite CAR_LIBRARY.json
"""

import argparse
import json
import sqlite3
from collections import Counter, defaultdict
from contextlib import closing
from pathlib import Path

LANDSTALKER_XL = 0xCE0B9F22
RHINO_TANK = 0x2EA68690
CONFIG_MARKER = 0x41C9FAC3
DARK_NEUTRAL_PAINT = 0xFF020202
MAX_PARTS = 512
GARAGE_CLASSES = {
    "Bikes", "Coupes", "Muscle Cars", "Off-Road Vehicles", "Pickups",
    "Sedans", "Sports", "Supercars", "SUVs", "Vans",
}
OTHER_STORAGE = {"Aircraft", "Trailers", "Trains", "Watercraft"}
OUTPUT = Path(__file__).resolve().parents[1] / "static/gta6-cars/native/native-catalog.mjs"


def uint32(value):
    return type(value) is int and 0 < value <= 0xFFFFFFFF


def read_definitions(database):
    descriptors = defaultdict(set)
    configurations = defaultdict(list)
    variations = set()
    rows = database.execute("""
        SELECT kind, model_hash, variant_hash, model_type, appearance_json
        FROM native_definition
        ORDER BY digest
    """)
    for kind, model, variant, inventory_type, appearance in rows:
        data = json.loads(appearance)
        if kind == "descriptor" and uint32(inventory_type) and data.get("73b59518") == inventory_type:
            descriptors[variant].add(inventory_type)
        elif kind == "variation":
            variations.add((model, variant))
        elif kind == "configuration":
            configurations[model].append((variant, data))
    return descriptors, configurations, variations


def select_preset(model, definitions, descriptors, variations):
    candidates = {}
    for variant, data in definitions:
        config, raw_parts = data.get("name"), data.get("e89b8540")
        if not (uint32(variant) and uint32(config) and isinstance(raw_parts, list)):
            continue
        if any(type(part) is not int or not 0 <= part <= 0xFFFFFFFF for part in raw_parts):
            continue
        # Neither value appears in the 45,437 serialized part words in the PS5
        # corpus. The marker can appear near the end of the native list.
        parts = tuple(part for part in raw_parts if part not in (0, CONFIG_MARKER))
        if not 0 < len(parts) <= MAX_PARTS:
            continue
        types = descriptors[variant]
        if len(types) != 1:
            continue
        key = (variant, config)
        candidate = (next(iter(types)), parts, data.get("5bdc13e3"))
        if key in candidates and candidates[key] != candidate:
            raise ValueError(f"Conflicting native configuration {model:08x}/{variant:08x}/{config:08x}")
        candidates[key] = candidate
    if not candidates:
        return None

    # Prefer a witnessed variation with more indexed presets. The neutral paint
    # was used in the Landstalker trial. Neither signal names an official default.
    by_variant = Counter(variant for variant, _ in candidates)
    variant, config = min(candidates, key=lambda key: (
        (model, key[0]) not in variations,
        -by_variant[key[0]],
        candidates[key][2] != DARK_NEUTRAL_PAINT,
        key,
    ))
    inventory_type, parts, _ = candidates[variant, config]
    default_evidence = "only-indexed-preset" if len(candidates) == 1 else "deterministic-fallback"
    return variant, config, inventory_type, parts, default_evidence


def class_for(model, name, library):
    category = library["srCategory"].get(str(model))
    if category:
        return category, "HTOS model category"
    # The model hash is JOAAT32("bus") and the HTOS service entry has that exact slug.
    if model == 0xD577C962 and name == "Bus" and any(
        label == name and public_class == "Service" and slug == "bus"
        for label, _, public_class, slug in library["vehicleList"]
    ):
        return "Transport Vehicles", "HTOS model token and service label"
    return "Unclassified", None


def coverage(database, library):
    descriptors, configurations, variations = read_definitions(database)
    donor_models = {row["model"] & 0xFFFFFFFF for row in library["cars"]}
    rows = database.execute("""
        SELECT model_hash, label, label_evidence
        FROM model
        WHERE model_hash != ? AND (model_hash = ? OR model_hash NOT IN (SELECT DISTINCT model_hash FROM body))
        ORDER BY label COLLATE NOCASE, model_hash
    """, (LANDSTALKER_XL, RHINO_TANK)).fetchall()
    labels = Counter(name for _, name, _ in rows)
    vehicles = []
    for model, name, label_evidence in rows:
        definitions = configurations[model]
        selected = select_preset(model, definitions, descriptors, variations)
        variant, config, inventory_type, parts, default_evidence = selected or (None, None, None, [], None)
        category, category_evidence = class_for(model, name, library)
        if not definitions:
            reason = "No indexed native configuration."
        elif selected is None:
            reason = "No complete native preset with a unique inventory descriptor."
        elif model == RHINO_TANK:
            reason = "An archived save links a Rhino Tank to a different garage."
        elif category in OTHER_STORAGE:
            reason = "This class may need a different type of storage."
        elif category == "Unclassified":
            reason = "This model has no documented garage class."
        elif category not in GARAGE_CLASSES:
            reason = "No archived garage link was found for this model in the sampled saves."
        else:
            reason = None
        if labels[name] > 1:
            token = library["srInternal"].get(str(model)) or f"0x{model:08X}"
            name = f"{name} ({token})"
        vehicles.append({
            "name": name,
            "model": model,
            "variant": variant,
            "config": config,
            "inventoryType": inventory_type,
            "parts": parts,
            "category": category,
            "defaultEvidence": default_evidence,
            "eligible": reason is None,
            "blockReason": reason,
            "garageEligible": reason is None,
            "garageTrial": reason is not None and selected is not None,
            "labelEvidence": label_evidence,
            "categoryEvidence": category_evidence,
            "donorAvailable": model in donor_models,
            "archivedGarageControl": model == RHINO_TANK,
        })
    return vehicles


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("database", type=Path, help="Local PS5 vehicle research SQLite index")
    parser.add_argument("class_catalog", type=Path, help="Local HTOS car_library.json")
    parser.add_argument("--output", type=Path, default=OUTPUT)
    parser.add_argument("--check", action="store_true", help="Verify that the generated module is current")
    args = parser.parse_args()
    library = json.loads(args.class_catalog.read_bytes())
    database_uri = args.database.resolve(strict=True).as_uri() + "?mode=ro"
    with closing(sqlite3.connect(database_uri, uri=True)) as database:
        vehicles = coverage(database, library)
    counts = {
        "unseenModels": sum(not vehicle["archivedGarageControl"] for vehicle in vehicles),
        "eligibleModels": sum(vehicle["eligible"] for vehicle in vehicles),
        "garageTrialModels": sum(vehicle["garageTrial"] for vehicle in vehicles),
        "eligibleWithoutDonor": sum(vehicle["eligible"] and not vehicle["donorAvailable"] for vehicle in vehicles),
        "completePresets": sum(vehicle["config"] is not None for vehicle in vehicles),
        "onlyIndexedPresets": sum(vehicle["defaultEvidence"] == "only-indexed-preset" for vehicle in vehicles),
    }
    options = ",\n".join("  " + json.dumps(v, ensure_ascii=True, separators=(",", ":")) for v in vehicles)
    output = (
        "// Generated by scripts/build_native_vehicle_catalog.py from the local PS5 vehicle index.\n"
        "// Each experiment uses an indexed preset. A fallback makes no claim about the stock appearance.\n"
        f"export const experimentalCoverage = Object.freeze({json.dumps(counts)});\n\n"
        "export const experimentalVehicles = Object.freeze([\n"
        f"{options}\n"
        "].map(vehicle => Object.freeze({ ...vehicle, parts: Object.freeze(vehicle.parts) })));\n"
    )
    if args.check:
        if not args.output.is_file() or args.output.read_text(encoding="utf-8") != output:
            parser.exit(1, f"Stale native vehicle catalog: {args.output}\n")
    else:
        args.output.write_text(output, encoding="utf-8", newline="\n")
    print(json.dumps(counts))


if __name__ == "__main__":
    main()
