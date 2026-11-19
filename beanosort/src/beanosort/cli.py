import argparse
import getpass
import json
import os
from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy import select
from sqlalchemy.orm import Session

from .auth import hash_password
from .capture import CAPTURE_DEFAULT
from .config import Settings
from .db import Database
from .models import Audit, Container, Location, Station, User, Version, World, identity
from .service import Workflow


def migrate(url):
    root = Path(__file__).resolve().parents[2]
    config = Config(str(root / "alembic.ini"))
    config.attributes["database_url"] = url
    command.upgrade(config, "head")


def initialize(db, username, password):
    db.enable_wal()
    with Session(db.engine) as s, s.begin():
        world = s.get(World, 1)
        if world and world.environment != db.environment:
            raise ValueError("Refusing to change database environment")
        if not world:
            s.add(World(id=1, environment=db.environment))
        user = s.scalar(select(User).filter_by(username=username))
        if user is None:
            user = User(id=identity("user"), username=username, password_hash=hash_password(password), role="admin")
            s.add(user)
        user_id = user.id
    with db.write() as s:
        for name, kind, methods in (("Receiving desk", "manual", ["manual", "simulated"]),
                                    ("Packaging desk", "packaging", ["manual", "simulated"]),
                                    ("Training sorter", "simulator", ["simulated"]),
                                    ("Manual run record", "sorter_record", ["manual"])):
            if not s.scalar(select(Station.id).filter_by(name=name)):
                s.add(Station(id=identity("station"), name=name, kind=kind,
                              capabilities={"execution_methods": methods, "hardware_control": False}))
    workflow = Workflow(db)
    for name in ("Floor", "LoadingBay"):
        with db.read() as s:
            exists = s.scalar(select(Location.id).filter_by(name_key=name.casefold()))
            removed = s.scalar(select(Audit.id).where(Audit.action == "location_deleted", Audit.data["before"]["name_key"].as_string() == name.casefold()).limit(1))
        if not exists and not removed:
            workflow.create_location(user_id, "initial-location-" + name, name)
    for kind, name, data in (
        ("recipe", "Selection fixture / manual record", {"schema": "beanosort-recipe/v1", "purpose": "coordination only",
          "synthetic_rule": "every fifth fixture particle selected; all synthetic decisions issue actions",
          "hardware_configuration": None, "classifier": "unknown", "calibration": "unknown"}),
        ("capture_model", "Independent capture", CAPTURE_DEFAULT),
        ("reconciliation_policy", "Exact balance with explicit composition approval", {"tolerance_mg": 0,
         "allow_estimated_composition": True, "note": "Development default, not a universal process tolerance; configure a new version before preparing a job"}),
    ):
        with db.read() as s:
            exists = s.scalar(select(Version.id).filter_by(kind=kind, name=name))
        if not exists:
            workflow.create_version(user_id, kind, name, data)
    from .simulation import SimulationConfiguration
    SimulationConfiguration(db).initialize(user_id)
    return user_id


def seed_demo(db, user_id):
    if db.environment != "simulation":
        raise ValueError("Demo stock can only be seeded in an isolated simulation database")
    workflow = Workflow(db)
    bins = {}
    for label in ("Demo raw", "Demo Selected", "Demo Pass-through", "Ready source", "Ready Selected", "Ready Pass-through"):
        with db.read() as s:
            existing = s.scalar(select(Container.id).filter_by(label=label))
        bins[label] = existing or workflow.create_container(user_id, label=label, location="Training shelf", capacity="25", tare_mass="0.5")
    with db.read() as s:
        receiving = s.scalar(select(Station.id).filter_by(name="Receiving desk"))
        station = s.scalar(select(Station.id).filter_by(name="Training sorter"))
        # The repeatable v1 demo deliberately keeps its historical 97% recipe.
        versions = {v.kind: v.id for v in s.scalars(select(Version).where(Version.version == 1, Version.name != "Simulation capture",
                    (Version.kind != "recipe") | (Version.name == "Selection fixture / manual record")))}
    evidence = {"kind": "simulation", "method": "entered training scale value", "tare_basis": "net after tare", "precision_mg": 1000}
    first = None
    for label in ("Demo raw", "Ready source"):
        portion_id = workflow.receive(user_id, f"seed-receipt-v1-{label}", {
            "container_id": bins[label], "station_id": receiving, "net_mass": "20", "ready": True,
            "supplier": "Synthetic supplier", "origin": "Training fixture", "bean_type": "Unspecified pulse",
            "supplier_lot": "SYNTHETIC-001", "received_date": "2026-11-10", "notes": "No physical material represented", "evidence": evidence})
        if first is None:
            first = portion_id
    op_id = workflow.prepare(user_id, "seed-prepare-v1", {
        "portion_id": first, "planned_mass": "20", "station_id": station,
        "selected_id": bins["Demo Selected"], "pass_through_id": bins["Demo Pass-through"], "ready": True,
        "recipe_id": versions["recipe"], "capture_model_id": versions["capture_model"], "policy_id": versions["reconciliation_policy"]})
    with db.read() as s:
        from .models import Operation
        completed = s.get(Operation, op_id).status == "completed"
    if not completed:
        run_id = workflow.start(user_id, op_id, seed="demo-2026", count=1000)
        while workflow.simulate_next(user_id, run_id):
            pass
        workflow.transition(user_id, op_id, "finishing")
        workflow.transition(user_id, op_id, "awaiting_reconciliation", drained=True)
        workflow.reconcile(user_id, "seed-reconcile-v1", op_id, {
            "selected": "4", "pass_through": "16", "remainder": "0", "retained": "0", "sample": "0", "loss": "0",
            "evidence": evidence, "reason": "Synthetic entered weights; independent of predicted counts"})
        workflow.release(user_id, op_id, accept_estimates=True, reason="Training-only release of estimated composition")
    return op_id


def seed_material_demo(db, user_id):
    """Repeatable manual genealogy on isolated training stock only."""
    if db.environment != "simulation":
        raise ValueError("Material demo requires an isolated simulation database")
    from .handling import MaterialHandling
    from .models import HandlingPlan, Operation, Output, ProductLot
    sort = seed_demo(db, user_id)
    workflow, handler = Workflow(db), MaterialHandling(db)
    with db.read() as s:
        source = s.scalar(select(Output.portion_id).filter_by(operation_id=sort, stream="pass_through"))
        station = s.scalar(select(Station.id).filter_by(name="Receiving desk"))
        policy = s.scalar(select(Version.id).filter_by(kind="reconciliation_policy", version=1))
        spec = s.scalar(select(Version.id).filter_by(kind="packaging_specification", name="Training 1 kg pack", version=1))
    if not spec:
        spec = workflow.create_version(user_id, "packaging_specification", "Training 1 kg pack", {"sku": "SYNTHETIC-BEAN", "nominal_mg": 1000000})
    bins = {}
    for name in ("Demo withdrawal", "Demo blend ingredient", "Demo blend", "Demo product crate"):
        with db.read() as s:
            existing = s.scalar(select(Container.id).filter_by(label=name))
        bins[name] = existing or workflow.create_container(user_id, label=name, location="Training material bench", capacity="25", tare_mass="0.5")
    evidence = {"kind": "simulation", "method": "entered training value", "tare_basis": "net after tare", "precision_mg": 1000}
    second = workflow.receive(user_id, "material-demo-second-receipt-v1", {
        "container_id": bins["Demo blend ingredient"], "station_id": station, "net_mass": "3", "ready": True,
        "supplier": "Second synthetic supplier", "bean_type": "Unspecified pulse", "evidence": evidence})

    def handle(kind, portions, destination, execution, **extra):
        op = handler.prepare(user_id, "material-demo-prepare-" + kind + "-v1", kind, {
            "portion_ids": portions, "container_id": bins[destination], "station_id": station, "policy_id": policy, "ready": True, **extra})
        with db.read() as s:
            state = s.get(Operation, op).status
            recorded = s.scalar(select(Output.portion_id).filter_by(operation_id=op))
        if state == "prepared":
            handler.start(user_id, op)
        if not recorded:
            handler.execute(user_id, "material-demo-execute-" + kind + "-v1", op, {
                "physical_confirmation": True, "representative": True, "reason": "Synthetic manual workflow; no physical material",
                "evidence": evidence, "loss": "0", "remainder": "0", **execution})
        workflow.release(user_id, op, accept_estimates=True, reason="Training-only release; composition may be unknown")
        with db.read() as s:
            stream = {"split": "withdrawn", "blend": "blend", "packaging": "product"}[kind]
            return s.scalar(select(Output.portion_id).filter_by(operation_id=op, stream=stream))

    withdrawn = handle("split", [source], "Demo withdrawal", {"output_mass": "5", "remainder": "11"}, withdrawn_mass="5")
    blended = handle("blend", [withdrawn, second], "Demo blend", {"output_mass": "8", "actual_inputs": {withdrawn: "5", second: "3"}})
    product = handle("packaging", [blended], "Demo product crate", {"pack_masses": ["0.999", "1.001", "1", "1", "1", "1"], "remainder": "2"}, specification_id=spec)
    with db.read() as s:
        return s.scalar(select(ProductLot.id).filter_by(portion_id=product))


def main():
    parser = argparse.ArgumentParser(description="BeanoSort local workflow administration; no hardware access")
    sub = parser.add_subparsers(dest="command", required=True)
    init = sub.add_parser("init", help="Apply migrations and create an initial administrator/configuration")
    init.add_argument("--username", default="admin")
    sub.add_parser("migrate")
    summaries = sub.add_parser("rebuild-summaries", help="Rebuild derived statistics without changing stock or source evidence")
    summaries.add_argument("--run-id")
    summaries.add_argument("--username", default="admin")
    demo = sub.add_parser("demo")
    chain = sub.add_parser("demo-chain", help="Seed a complete synthetic receipt → sort → split → blend → package genealogy")
    chain.add_argument("--username", default="admin")
    demo.add_argument("--username", default="admin")
    user = sub.add_parser("user")
    user.add_argument("username")
    user.add_argument("--role", choices=["operator", "supervisor", "admin"], default="operator")
    version = sub.add_parser("version", help="Append a configuration version from a JSON file")
    version.add_argument("kind", choices=["recipe", "capture_model", "reconciliation_policy", "product_specification", "packaging_specification"])
    version.add_argument("name")
    version.add_argument("file", type=Path)
    version.add_argument("--username", default="admin")
    ingest = sub.add_parser("ingest", help="Import a reviewed compact-particle batch, no machine connection")
    ingest.add_argument("run_id")
    ingest.add_argument("batch_key")
    ingest.add_argument("file", type=Path)
    ingest.add_argument("--username", default="admin")
    export = sub.add_parser("export-registry", help="Read an archived completed Registry run into a durable spool")
    export.add_argument("database", type=Path)
    export.add_argument("source_run_id")
    export.add_argument("output", type=Path)
    export.add_argument("--statistics", type=Path)
    spool = sub.add_parser("import-spool", help="Attach and resume/replay a verified offline spool")
    spool.add_argument("run_id")
    spool.add_argument("directory", type=Path)
    spool.add_argument("--username", default="admin")
    reference = sub.add_parser("reference-import", help="Snapshot a read-only statistics population and create its first lightness recipe")
    reference.add_argument("directory", type=Path)
    reference.add_argument("--storage", type=Path, default=Path("var/reference-datasets"))
    reference.add_argument("--name", default="Green coffee reference")
    reference.add_argument("--source-url", default="")
    reference.add_argument("--username", default="admin")
    hopper = sub.add_parser("hopper-recipe", help="Create the all-Pass-through hopper-emptying reference recipe; no hardware access")
    hopper.add_argument("--dataset-id")
    hopper.add_argument("--username", default="admin")
    args = parser.parse_args()
    if args.command == "export-registry":
        from .adapter import export_registry
        result = export_registry(args.database, args.source_run_id, args.output, statistics=args.statistics)
        print(json.dumps({k: result[k] for k in ("fingerprint", "environment", "particle_count", "counts")}, indent=2))
        return
    settings = Settings.from_env()
    db = Database(settings.database_url, settings.environment)
    if args.command in {"init", "migrate"}:
        migrate(settings.database_url)
        db.enable_wal()
        if args.command == "init":
            password = os.environ.get("BEANOSORT_ADMIN_PASSWORD") or getpass.getpass("Initial admin password: ")
            initialize(db, args.username, password)
        print("Database initialized/migrated; no hardware services started.")
    elif args.command == "user":
        password = getpass.getpass("New user password: ")
        with db.write() as s:
            s.add(User(id=identity("user"), username=args.username, password_hash=hash_password(password), role=args.role))
        print("User created")
    else:
        with db.read() as s:
            user_id = s.scalar(select(User.id).filter_by(username=args.username))
            if user_id is None:
                parser.error("Initialize the requested user first")
        if args.command == "demo":
            print("Synthetic demo job:", seed_demo(db, user_id))
        elif args.command == "rebuild-summaries":
            from .auth import require_role
            from .models import Run
            from .run_summaries import rebuild
            with db.read() as s:
                require_role(s.get(User, user_id), "admin", "supervisor")
                ids = [args.run_id] if args.run_id else list(s.scalars(select(Run.id).order_by(Run.created_at)))
            for run_id in ids:
                with db.write() as s:
                    run = s.get(Run, run_id)
                    if run is None: parser.error("Unknown run")
                    result = rebuild(s, run, db.environment)
                    count = result.particle_count
                print(json.dumps({"run_id":run_id,"summarized_particles":count}),flush=True)
        elif args.command == "demo-chain":
            print("Synthetic product lot:", seed_material_demo(db, user_id))
        elif args.command == "version":
            print(Workflow(db).create_version(user_id, args.kind, args.name, json.loads(args.file.read_text())))
        elif args.command == "ingest":
            print("New particles:", Workflow(db).ingest(user_id, args.run_id, args.batch_key, json.loads(args.file.read_text())))
        elif args.command == "import-spool":
            from .adapter import import_spool
            print(json.dumps(import_spool(Workflow(db), user_id, args.run_id, args.directory)))
        elif args.command == "reference-import":
            from .references import DEFAULT_RECIPE, create_recipe, import_bundle
            dataset_id = import_bundle(db, user_id, args.directory, args.storage, name=args.name, source_url=args.source_url)
            recipe_id = create_recipe(db, user_id, "reference-default-" + dataset_id, DEFAULT_RECIPE, dataset_id)
            print(json.dumps({"dataset_id": dataset_id, "recipe_id": recipe_id}))
        elif args.command == "hopper-recipe":
            from .models import ReferenceDataset
            from .references import create_recipe
            from .sorting_modes import HOPPER_RECIPE_NAME
            with db.read() as s:
                dataset_id = args.dataset_id or s.scalar(select(ReferenceDataset.id).order_by(ReferenceDataset.created_at.desc()).limit(1))
            if not dataset_id:
                parser.error("Import a reference dataset before creating its hopper-emptying simulation recipe")
            print(create_recipe(db, user_id, "hopper-emptying-"+dataset_id, HOPPER_RECIPE_NAME,
                                dataset_id, feature="area_mm2", relation="pass_all"))


if __name__ == "__main__":
    main()
